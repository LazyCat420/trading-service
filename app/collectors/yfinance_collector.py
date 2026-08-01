"""
yfinance Collector — Fetches OHLCV, fundamentals, financials, balance sheet.

Pure data collector. No LLM calls. No processing.
Writes to: price_history, fundamentals, financial_history, balance_sheet
"""

import logging

logger = logging.getLogger(__name__)


import datetime
import asyncio
import yfinance as yf
import requests
from requests.adapters import HTTPAdapter

class TimeoutHTTPAdapter(HTTPAdapter):
    def __init__(self, timeout=15.0, *args, **kwargs):
        self.timeout = timeout
        super().__init__(*args, **kwargs)

    def send(self, request, **kwargs):
        kwargs["timeout"] = kwargs.get("timeout") or self.timeout
        return super().send(request, **kwargs)

def get_timeout_session(timeout=15.0) -> requests.Session:
    session = requests.Session()
    adapter = TimeoutHTTPAdapter(timeout=timeout)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session

_yf_session = get_timeout_session(15.0)
from app.db.connection import get_db


def _is_blocked_ticker(ticker: str) -> bool:
    """Pre-collection guard: reject tickers in the FALSE_TICKERS blocklist."""
    try:
        from app.processors.ticker_extractor import FALSE_TICKERS
        if ticker.upper() in FALSE_TICKERS:
            logger.warning("[yfinance] BLOCKED ticker '%s' — in FALSE_TICKERS", ticker)
            return True
    except ImportError:
        pass
    return False


async def fetch_ohlcv_dataframe(ticker: str, period: str = "6mo"):
    """Fetch OHLCV history as a DataFrame without writing to DB.

    Incomplete bars are dropped HERE, at the shared fetcher, so every consumer
    gets a frame whose LAST ROW is a real session.

    yfinance returns the in-progress session with NaN OHLC and a non-null
    Volume. `collect_price_history` learned to salvage around that (7e8932a),
    but `market_data.build_market_snapshot` — a different consumer of this same
    function — does `df.iloc[-1]` and takes the NaN row, so
    `float(latest["Close"]) or None` collapsed to None. Measured in
    cycle-v3-1785128960: 7 of 8 tickers stored `analysis_price = 0.00` while
    price_history held perfectly good closes. Those snapshots feed the
    Freshness Gate's next-cycle delta, so a zero baseline silently corrupts the
    comparison rather than failing loudly.

    Fixing the fetcher rather than each caller: any future consumer of this
    frame would otherwise inherit the same trap.
    """
    stock = yf.Ticker(ticker)
    try:
        df = await asyncio.to_thread(stock.history, period=period, auto_adjust=True)
        if df is None or df.empty:
            logger.info(f"[yfinance] No price data for {ticker}")
            return None

        ohlc = [c for c in ("Open", "High", "Low", "Close") if c in df.columns]
        if ohlc:
            before = len(df)
            df = df.dropna(subset=ohlc)
            if len(df) < before:
                logger.debug(
                    "[yfinance] %s: dropped %d incomplete bar(s) at fetch",
                    ticker, before - len(df),
                )
        if df.empty:
            logger.info(f"[yfinance] No complete bars for {ticker}")
            return None
        return df
    except Exception as e:
        logger.info(f"[yfinance] Error fetching price history for {ticker}: {e}")
        return None


async def collect_price_history(ticker: str, period: str = "6mo") -> int:
    """
    Fetch OHLCV history and upsert into price_history table.
    Returns number of rows inserted.
    """
    if _is_blocked_ticker(ticker):
        return 0
    df = await fetch_ohlcv_dataframe(ticker, period)
    if df is None:
        # A failed/empty fetch still leaves whatever prices we already have,
        # and those may be newer than the derived technicals — so repair them
        # rather than returning early. yfinance returns NaN often enough
        # (rate limits, after hours) that skipping here would leave the
        # freshness of the whole table at the mercy of the vendor.
        await _refresh_technicals(ticker)
        return 0

    from app.validation.schema import PriceHistorySchema
    import pandera.errors

    # Drop incomplete bars BEFORE validating. yfinance routinely returns the
    # most recent session with NaN OHLC and a non-null Volume — an in-progress
    # or not-yet-settled bar. PriceHistorySchema's columns are non-nullable, so
    # that single row used to reject the whole frame: measured 2026-07-26, all
    # 12 tickers in the cycle failed with "non-nullable series 'Open' contains
    # null values" on exactly one bad row out of 125, discarding 124 good ones
    # and leaving every agent on cached prices. Salvaging the complete rows is
    # strictly better than keeping none of them — a partial frame is still an
    # upsert, and the dropped bar arrives complete on the next collection.
    #
    # Note the incremental-fetch trap: the NaN is in the NEWEST bar, so a
    # narrower `period` does not avoid it. Salvage is what fixes this, not a
    # smaller window.
    _ohlc = ["Open", "High", "Low", "Close"]
    _before = len(df)
    df = df.dropna(subset=[c for c in _ohlc if c in df.columns])
    _dropped = _before - len(df)
    if _dropped:
        logger.warning(
            "[yfinance] %s: dropped %d incomplete bar(s) of %d (NaN OHLC — "
            "usually the in-progress session); keeping %d",
            ticker, _dropped, _before, len(df),
        )
    if df.empty:
        logger.error(
            "[yfinance] %s: every bar was incomplete — no usable price rows", ticker
        )
        await _refresh_technicals(ticker)
        return 0

    # Same salvage for internally inconsistent bars (High < Open etc.). The
    # schema's OHLC checks are frame-level, so one bad bar used to reject the
    # whole frame — and unlike the NaN case, a bad bar mid-history stays inside
    # the fetch window, so the failure REPEATS every collection until the bar
    # ages out. Measured in cycle-v3-1785504601: RBLX's 2026-07-18 gap-down bar
    # (Open 49.46 > High 40.0, straight from Yahoo) blocked all yfinance writes
    # for 10 sessions; the desk then analysed RBLX at the 07-17 close, 24% off.
    # We drop the bar rather than clamping it: we cannot know which field is
    # wrong, and the other vendor usually has the session.
    from app.validation.schema import ohlc_consistency_mask
    _mask = ohlc_consistency_mask(df)
    if not _mask.all():
        _bad = df[~_mask]
        logger.warning(
            "[yfinance] %s: dropped %d internally inconsistent bar(s) (%s); "
            "keeping %d",
            ticker,
            len(_bad),
            ", ".join(str(d.date()) for d in _bad.index[:5]),
            int(_mask.sum()),
        )
        df = df[_mask]
    if df.empty:
        logger.error(
            "[yfinance] %s: every bar was inconsistent — no usable price rows",
            ticker,
        )
        await _refresh_technicals(ticker)
        return 0

    try:
        df = PriceHistorySchema.validate(df)
    except pandera.errors.SchemaError as e:
        logger.error(f"[yfinance] Validation failed for {ticker}: {e}")
        await _refresh_technicals(ticker)
        return 0

    rows = []
    for date, row in df.iterrows():
        rows.append(
            [
                ticker,
                date.date(),
                float(row["Open"]),
                float(row["High"]),
                float(row["Low"]),
                float(row["Close"]),
                int(row["Volume"]),
            ]
        )

    if rows:

        def _insert():
            with get_db() as db:
                db.executemany(
                    """
                    INSERT INTO price_history (ticker, date, open, high, low, close, volume, source)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, 'yfinance')
                    ON CONFLICT (ticker, date, source) DO NOTHING
                """,
                    rows,
                )

        await asyncio.to_thread(_insert)

    count = len(rows)

    logger.info(f"[yfinance] {ticker}: {count} price rows written")
    await _refresh_technicals(ticker)
    return count


async def _refresh_technicals(ticker: str) -> None:
    """Recompute the derived indicator rows for `ticker`.

    Technicals are a pure function of `price_history`, so they are refreshed
    HERE — at the single point prices are collected — rather than left to
    whenever an agent happens to call `get_technical_indicators`. Nothing
    scheduled that, which is why only 5 of 503 tickers were fresher than 3 days
    while `price_history` was current for all of them, and why the quant
    analyst was handed a **1963-12-26** RSI for CVX as its "VERIFIED TECHNICAL
    BASELINE".

    Deliberately hooked into `collect_price_history` rather than
    `collect_all()`: the V3 precollect path (`app/v3/data_report.py`) calls
    `collect_price_history` directly, so a hook one level up would never fire
    during a cycle — the path that matters most.

    Called on the failure paths too. A NaN/rate-limited fetch still leaves the
    prices we already had, which may be newer than the technicals; skipping
    then would put the table's freshness at the mercy of the vendor.

    Fail-open: stale technicals are bad, but a failure here must never cost us
    the price rows we just collected.
    """
    try:
        from app.processors.technical_processor import compute_technicals

        await asyncio.to_thread(compute_technicals, ticker)
    except Exception as e:
        logger.warning(
            "[yfinance] %s: technicals refresh failed (non-fatal): %s", ticker, e
        )


async def fetch_fundamentals_dict(ticker: str) -> dict | None:
    """Fetch fundamentals dictionary without writing to DB."""
    stock = yf.Ticker(ticker)
    try:
        info = await asyncio.to_thread(lambda: stock.info)
        if not info or "symbol" not in info:
            logger.info(f"[yfinance] No fundamentals for {ticker}")
            return None
        return info
    except Exception as e:
        logger.info(f"[yfinance] Error fetching fundamentals for {ticker}: {e}")
        return None


async def collect_fundamentals_finnhub(ticker: str) -> bool:
    """
    Fetch fundamentals snapshot from Finnhub as a tertiary fallback.
    Returns True if data was written.
    """
    import os
    from app.config.config import settings
    
    api_key = settings.FINNHUB_API_KEY or os.environ.get("FINNHUB_API_KEY", "")
    if not api_key:
        logger.info(f"[finnhub] API key not set, skipping fundamentals fallback for {ticker}")
        return False

    try:
        import finnhub
        client = finnhub.Client(api_key=api_key)
        
        # Run Finnhub client API calls in threads since they are blocking I/O calls
        profile = await asyncio.to_thread(client.company_profile2, symbol=ticker)
        financials = await asyncio.to_thread(client.company_basic_financials, ticker, 'all')
        
        if not profile and not financials:
            logger.info(f"[finnhub] No profile/financials data returned for {ticker}")
            return False
            
        metric = financials.get("metric", {}) if financials else {}
        
        today = datetime.date.today()
        # Market Cap from profile is in Millions, convert to dollars
        mkt_cap_m = profile.get("marketCapitalization")
        mkt_cap = float(mkt_cap_m) * 1_000_000 if mkt_cap_m else None
        
        pe = metric.get("peNormalizedTTM") or metric.get("peExclExtraTTM")
        beta = metric.get("beta") or profile.get("beta")
        
        # Map fields to fundamentals table structure
        with get_db() as db:
            db.execute(
                """
                INSERT INTO fundamentals (
                    ticker, snapshot_date, source, market_cap, pe_ratio, forward_pe, peg_ratio,
                    price_to_book, price_to_sales, ev_to_ebitda, profit_margin,
                    roe, roa, revenue, revenue_growth, net_income,
                    debt_to_equity, current_ratio, beta,
                    week_52_high, week_52_low, short_float_pct
                ) VALUES (%s, %s, 'finnhub', %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (ticker, snapshot_date) DO NOTHING
                """,
                [
                    ticker.upper(),
                    today,
                    mkt_cap,
                    pe,
                    None,
                    None,
                    metric.get("bookValuePerShareAnnual"), # pb proxy
                    metric.get("psTTM"),
                    None,
                    metric.get("netProfitMarginTTM"),
                    metric.get("roeTTM"),
                    metric.get("roaTTM"),
                    None,
                    None,
                    None,
                    # percent-style like yfinance — column convention is ratio
                    (metric.get("debtEquityTTM") / 100.0
                     if metric.get("debtEquityTTM") is not None else None),
                    metric.get("currentRatioAnnual"),
                    beta,
                    metric.get("52WeekHigh"),
                    metric.get("52WeekLow"),
                    None,
                ],
            )
        logger.info(f"[finnhub] Successfully stored fundamentals fallback for {ticker}")
        return True
    except Exception as e:
        logger.info(f"[finnhub] Error fetching fundamentals fallback for {ticker}: {e}")
        return False


async def collect_fundamentals(ticker: str) -> bool:
    """
    Fetch fundamentals snapshot and upsert into fundamentals table.
    Returns True if data was written.
    """
    # 1. Try yfinance
    info = await fetch_fundamentals_dict(ticker)
    if info and info.get("marketCap"):
        today = datetime.date.today()

        # Unit seams in yfinance .info (checked against yfinance 1.2.0):
        # debtToEquity is a PERCENT (AAPL 79.5) — the column convention is
        # RATIO, so divide. dividendYield is a PERCENT since the 0.2.x line
        # (AAPL 0.32 = 0.32%) — column convention is FRACTION, so divide.
        # Every other percent-like field already arrives as a fraction.
        de = info.get("debtToEquity")
        de = de / 100.0 if de is not None else None
        dy = info.get("dividendYield")
        dy = dy / 100.0 if dy is not None else None
        earnings_ts = info.get("earningsTimestamp")
        earnings_date = (
            datetime.date.fromtimestamp(earnings_ts) if earnings_ts else None
        )

        fields = {
            "market_cap": info.get("marketCap"),
            "pe_ratio": info.get("trailingPE"),
            "forward_pe": info.get("forwardPE"),
            "peg_ratio": info.get("pegRatio") or info.get("trailingPegRatio"),
            "price_to_book": info.get("priceToBook"),
            "price_to_sales": info.get("priceToSalesTrailing12Months"),
            "ev_to_ebitda": info.get("enterpriseToEbitda"),
            "profit_margin": info.get("profitMargins"),
            "roe": info.get("returnOnEquity"),
            "roa": info.get("returnOnAssets"),
            "revenue": info.get("totalRevenue"),
            "revenue_growth": info.get("revenueGrowth"),
            "net_income": info.get("netIncomeToCommon"),
            "debt_to_equity": de,
            "current_ratio": info.get("currentRatio"),
            "beta": info.get("beta"),
            "week_52_high": info.get("fiftyTwoWeekHigh"),
            "week_52_low": info.get("fiftyTwoWeekLow"),
            "short_float_pct": info.get("shortPercentOfFloat"),
            # extension (2026-07-27)
            "dividend_yield": dy,
            "dividend_ttm": info.get("trailingAnnualDividendRate"),
            "payout_ratio": info.get("payoutRatio"),
            "ev_to_sales": info.get("enterpriseToRevenue"),
            "quick_ratio": info.get("quickRatio"),
            "eps_ttm": info.get("trailingEps"),
            # NOTE: yfinance's earningsGrowth is the latest QUARTER's growth,
            # NOT finviz's "EPS this Y" fiscal-year estimate (XOM: +60% vs
            # -43% — different concepts). eps_growth_this_y is finviz-only.
            "eps_growth_qoq": info.get("earningsQuarterlyGrowth"),
            "sales_growth_qoq": info.get("revenueGrowth"),
            "gross_margin": info.get("grossMargins"),
            "oper_margin": info.get("operatingMargins"),
            "insider_own_pct": info.get("heldPercentInsiders"),
            "inst_own_pct": info.get("heldPercentInstitutions"),
            "shares_outstanding": info.get("sharesOutstanding"),
            "shares_float": info.get("floatShares"),
            "short_ratio": info.get("shortRatio"),
            "short_interest": info.get("sharesShort"),
            "recom_score": info.get("recommendationMean"),
            "target_price": info.get("targetMeanPrice"),
            "earnings_date": earnings_date,
        }
        cols = list(fields.keys())
        updates = ", ".join(
            f"{c} = COALESCE(EXCLUDED.{c}, fundamentals.{c})" for c in cols
        )
        with get_db() as db:
            db.execute(
                f"""
                INSERT INTO fundamentals (ticker, snapshot_date, source, {', '.join(cols)})
                VALUES (%s, %s, 'yfinance', {', '.join(['%s'] * len(cols))})
                ON CONFLICT (ticker, snapshot_date) DO UPDATE SET {updates}
                """,
                [ticker, today] + [fields[c] for c in cols],
            )
        logger.info(
            f"[yfinance] {ticker}: fundamentals written (mkt_cap={info.get('marketCap')})"
        )
        return True

    logger.info(f"[yfinance] Failed to collect fundamentals for {ticker}, trying fallbacks...")

    # 2. Try FMP Fallback
    try:
        from app.collectors.fmp_collector import collect_fundamentals as collect_fmp
        fmp_ok = await collect_fmp(ticker)
        if fmp_ok:
            logger.info(f"[fundamentals] Fallback to FMP succeeded for {ticker}")
            return True
    except Exception as e:
        logger.warning(f"[fundamentals] Fallback to FMP failed for {ticker}: {e}")

    # 3. Try Finnhub Fallback
    try:
        finnhub_ok = await collect_fundamentals_finnhub(ticker)
        if finnhub_ok:
            logger.info(f"[fundamentals] Fallback to Finnhub succeeded for {ticker}")
            return True
    except Exception as e:
        logger.warning(f"[fundamentals] Fallback to Finnhub failed for {ticker}: {e}")

    return False


async def collect_financials(ticker: str) -> int:
    """
    Fetch income statement (quarterly + annual) and upsert into financial_history.
    Returns number of rows inserted.
    """
    stock = yf.Ticker(ticker)
    count = 0
    try:
        sources = await asyncio.to_thread(
            lambda: [
                ("quarterly", stock.quarterly_income_stmt),
                ("annual", stock.income_stmt),
            ]
        )
    except Exception as e:
        logger.info(f"[yfinance] Error fetching financials for {ticker}: {e}")
        return 0

    rows = []
    for period_type, financials in sources:
        if financials is None or financials.empty:
            continue

        for col in financials.columns:
            period_end = col.date() if hasattr(col, "date") else col
            data = financials[col]
            rows.append(
                [
                    ticker,
                    period_type,
                    period_end,
                    _safe_float(data, "Total Revenue"),
                    _safe_float(data, "Gross Profit"),
                    _safe_float(data, "Operating Income"),
                    _safe_float(data, "Net Income"),
                    _safe_float(data, "Basic EPS"),
                    None,  # FCF from cash flow statement, not income stmt
                ]
            )

    if rows:

        def _insert():
            with get_db() as db:
                db.executemany(
                    """
                    INSERT INTO financial_history (
                        ticker, period_type, period_end,
                        revenue, gross_profit, operating_income,
                        net_income, eps, free_cash_flow
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (ticker, period_type, period_end) DO UPDATE SET
                    revenue = EXCLUDED.revenue,
                    gross_profit = EXCLUDED.gross_profit,
                    operating_income = EXCLUDED.operating_income,
                    net_income = EXCLUDED.net_income,
                    eps = EXCLUDED.eps,
                    free_cash_flow = EXCLUDED.free_cash_flow
                """,
                    rows,
                )

        await asyncio.to_thread(_insert)

    count = len(rows)

    logger.info(f"[yfinance] {ticker}: {count} financial history rows written")
    return count


async def collect_balance_sheet(ticker: str) -> int:
    """
    Fetch balance sheet and upsert into balance_sheet table.
    Returns number of rows inserted.
    """
    stock = yf.Ticker(ticker)
    try:
        bs = await asyncio.to_thread(lambda: stock.balance_sheet)
        if bs is None or bs.empty:
            logger.info(f"[yfinance] No balance sheet for {ticker}")
            return 0
    except Exception as e:
        logger.info(f"[yfinance] Error fetching balance sheet for {ticker}: {e}")
        return 0

    count = 0

    rows = []
    for col in bs.columns:
        period_end = col.date() if hasattr(col, "date") else col
        data = bs[col]

        rows.append(
            [
                ticker,
                period_end,
                _safe_float(data, "Total Assets"),
                _safe_float(data, "Total Liabilities Net Minority Interest"),
                _safe_float(data, "Stockholders Equity"),
                _safe_float(data, "Cash And Cash Equivalents"),
                _safe_float(data, "Total Debt"),
                _safe_float(data, "Working Capital"),
            ]
        )

    if rows:

        def _insert():
            with get_db() as db:
                db.executemany(
                    """
                    INSERT INTO balance_sheet (
                        ticker, period_end, total_assets, total_liabilities,
                        total_equity, cash, total_debt, working_capital
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (ticker, period_end) DO UPDATE SET
                        total_assets = EXCLUDED.total_assets,
                        total_liabilities = EXCLUDED.total_liabilities,
                        total_equity = EXCLUDED.total_equity,
                        cash = EXCLUDED.cash,
                        total_debt = EXCLUDED.total_debt,
                        working_capital = EXCLUDED.working_capital
                """,
                    rows,
                )

        await asyncio.to_thread(_insert)

    count = len(rows)

    logger.info(f"[yfinance] {ticker}: {count} balance sheet rows written")
    return count


async def collect_all(ticker: str) -> dict:
    """Run all yfinance collectors for a single ticker."""
    if _is_blocked_ticker(ticker):
        return {"ticker": ticker, "price_rows": 0, "fundamentals": False, "financial_rows": 0, "balance_rows": 0}
    prices = await collect_price_history(ticker)
    fundies = await collect_fundamentals(ticker)
    financials = await collect_financials(ticker)
    balance = await collect_balance_sheet(ticker)

    return {
        "ticker": ticker,
        "price_rows": prices,
        "fundamentals": fundies,
        "financial_rows": financials,
        "balance_rows": balance,
    }


async def collect_news(ticker: str) -> int:
    """
    Fetch ticker-specific news from yfinance and scrape full article bodies.
    Proxy to the robust news_collector.py to ensure full body extraction and quality gating.
    """
    try:
        from app.collectors.news_collector import collect_yfinance_news
        logger.info(f"[yfinance] Proxying collect_news({ticker}) to robust news_collector...")
        return await collect_yfinance_news(ticker)
    except Exception as e:
        logger.error(f"[yfinance] Proxy call error for {ticker}: {e}")
        return 0


def _safe_float(series, key: str) -> float | None:
    """Safely extract a float from a pandas Series, handling missing keys."""
    try:
        val = series.get(key)
        if val is not None and str(val) != "nan":
            return float(val)
    except (KeyError, TypeError, ValueError):
        pass
    return None
