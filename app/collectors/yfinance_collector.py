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
from app.db import mongo_store


def _is_blocked_ticker(ticker: str) -> bool:
    """Pre-collection guard for an EXPLICITLY NAMED ticker.

    Deliberately not the runtime-augmented FALSE_TICKERS. Those auto-bans come
    from extraction verification ("yfinance returned nothing, likely delisted")
    and from company_registry's `rejected` rows — and a transient vendor outage
    on 2026-05-07 mass-banned SPY, QQQ, IWM, SMH and 67 other real ETFs that
    way, permanently refusing every explicit price fetch for them.

    Nor is it the STATIC slang list any more (2026-08-08). That argument was
    right about the runtime list and stopped one step short: a caller that
    names a ticker outright is not extracting it from prose, so "does this look
    like a word" is the wrong question to ask about it. 124 of the slang list's
    324 entries are currently listed US instruments — AppLovin, ServiceNow,
    Allstate, Gartner, ON Semiconductor, Welltower — and every one of them was
    being refused here while other vendors backfilled the recent tip, so the
    damage was invisible to every freshness check and showed up only as depth:
    48 bars against a 4,818-bar median.

    `app/collectors/explicit_fetch_guard.py` holds the measurement and the
    split. What survives is the 200 entries that are not listed anywhere, which
    is what keeps `data_report`'s skip-not-outage classification working.
    """
    from app.collectors.explicit_fetch_guard import is_blocked_for_explicit_fetch

    if is_blocked_for_explicit_fetch(ticker):
        logger.warning(
            "[yfinance] BLOCKED ticker '%s' — slang/acronym with no listing", ticker,
        )
        return True
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
            # WARNING, not INFO — see the note on the except below. An empty
            # frame is the single most common failure and it used to be
            # invisible.
            logger.warning(
                "[yfinance] %s: FETCH_EMPTY — history(period=%s) returned %s",
                ticker, period, "None" if df is None else "0 rows",
            )
            return None

        # Volume belongs in this subset. yfinance's mid-session forming bar has
        # real OHLC but a NaN Volume (the session hasn't closed, so there's no
        # final volume yet) — that row survived an OHLC-only dropna, then blew
        # up PriceHistorySchema's `Volume: Series[int]` coercion and took all
        # 124 other good rows down with it. Reproduced 2026-08-01: a single
        # NaN Volume with valid OHLC on the newest row causes
        # `PriceHistorySchema.validate` to reject the ENTIRE 125-row frame.
        cols = [c for c in ("Open", "High", "Low", "Close", "Volume") if c in df.columns]
        if cols:
            before = len(df)
            df = df.dropna(subset=cols)
            if len(df) < before:
                logger.debug(
                    "[yfinance] %s: dropped %d incomplete bar(s) at fetch",
                    ticker, before - len(df),
                )
        if df.empty:
            logger.warning(
                "[yfinance] %s: FETCH_ALL_INCOMPLETE — every bar had a null in %s",
                ticker, "/".join(cols),
            )
            return None
        return df
    except Exception as e:
        # WARNING with the exception TYPE, and never a bare message.
        #
        # This was `logger.info(...)` with only str(e). Everything a fetch can
        # fail with — HTTP 429, socket timeout, JSON decode, a curl_cffi
        # error — collapsed into one INFO line that reached no error counter
        # and no telemetry table. So when 15 of 18 price failures landed in the
        # 09:xx ET bucket over 07-27..07-31, `execution_errors` held no record
        # of ANY of them: not a rate limit, not a timeout, nothing. The cause
        # was unknowable from the outside, which is why this is the first fix.
        #
        # The tag prefixes exist so the three paths can be counted apart in
        # SQL: FETCH_EMPTY / FETCH_ALL_INCOMPLETE / FETCH_EXCEPTION.
        logger.warning(
            "[yfinance] %s: FETCH_EXCEPTION — %s: %s",
            ticker, type(e).__name__, e,
        )
        return None


async def collect_price_history(ticker: str, period: str = "6mo") -> int:
    """
    Fetch OHLCV history and upsert into price_history table.
    Returns number of rows inserted.
    """
    if _is_blocked_ticker(ticker):
        logger.warning("[yfinance] %s: COLLECT_BLOCKED — on the ticker blocklist", ticker)
        return 0
    df = await fetch_ohlcv_dataframe(ticker, period)
    if df is None:
        # A failed/empty fetch still leaves whatever prices we already have,
        # and those may be newer than the derived technicals — so repair them
        # rather than returning early. yfinance returns NaN often enough
        # (rate limits, after hours) that skipping here would leave the
        # freshness of the whole table at the mercy of the vendor.
        #
        # This branch had NO log of its own, which made the most common way to
        # return 0 the least instrumented one: the caller's derived
        # "yfinance_price returned no data" event was the only trace, and it
        # cannot say why. The fetcher now tags its three failure modes; this
        # line closes the loop so a reader can join the two.
        logger.warning(
            "[yfinance] %s: COLLECT_NO_FRAME — fetch returned nothing "
            "(see the FETCH_* line above for the reason); refreshing "
            "technicals from stored prices only",
            ticker,
        )
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
    # Volume is in this subset for the same reason it had to be added to
    # fetch_ohlcv_dataframe's dropna: yfinance's forming bar can carry valid
    # OHLC and a NaN Volume (no final volume until the session settles), and
    # `Volume: Series[int]` coercion rejects the whole frame on that one NaN.
    # Measured 2026-08-01: a single NaN-Volume row with otherwise-valid OHLC
    # discarded all 125 rows via PriceHistorySchema.validate. This is the most
    # likely cause of the 09:xx ET opening-bell failures (15 of 18 over
    # 07-27..07-31): the market-open cycle runs while today's bar is still
    # forming, and that bar's Volume is exactly the field yfinance has not
    # settled yet.
    #
    # Note the incremental-fetch trap: the NaN is in the NEWEST bar, so a
    # narrower `period` does not avoid it. Salvage is what fixes this, not a
    # smaller window.
    _cols = ["Open", "High", "Low", "Close", "Volume"]
    _before = len(df)
    df = df.dropna(subset=[c for c in _cols if c in df.columns])
    _dropped = _before - len(df)
    if _dropped:
        logger.warning(
            "[yfinance] %s: dropped %d incomplete bar(s) of %d (NaN OHLC/Volume — "
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
    except (pandera.errors.SchemaError, pandera.errors.SchemaErrors) as e:
        # SchemaErrors (plural) is pandera's lazy/multi-failure variant and is
        # not a subclass of SchemaError — a coercion failure on a column like
        # Volume raises SchemaErrors, which this except clause did not catch
        # until now. An uncaught SchemaErrors here doesn't return 0, it
        # crashes the whole collect_all() call for the ticker, unhandled.
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
            mongo_store.upsert_doc('fundamentals', {'ticker': ticker.upper(), 'snapshot_date': today}, {'ticker': ticker.upper(), 'snapshot_date': today, 'source': 'finnhub', 'market_cap': mkt_cap, 'pe_ratio': pe, 'forward_pe': None, 'peg_ratio': None, 'price_to_book': metric.get("bookValuePerShareAnnual"), 'price_to_sales': metric.get("psTTM"), 'ev_to_ebitda': None, 'profit_margin': metric.get("netProfitMarginTTM"), 'roe': metric.get("roeTTM"), 'roa': metric.get("roaTTM"), 'revenue': None, 'revenue_growth': None, 'net_income': None, 'debt_to_equity': metric.get("debtEquityTTM") / 100.0
                     if metric.get("debtEquityTTM") is not None else None, 'current_ratio': metric.get("currentRatioAnnual"), 'beta': beta, 'week_52_high': metric.get("52WeekHigh"), 'week_52_low': metric.get("52WeekLow"), 'short_float_pct': None}, insert_only=True)
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
