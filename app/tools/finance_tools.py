import logging

from pydantic import BaseModel, Field
from app.tools.registry import registry
from app.utils.text_utils import format_db_section, fmt_usd
from app.db import mongo_query
from datetime import datetime, timezone

logger = logging.getLogger(__name__)


class TickerInput(BaseModel):
    ticker: str = Field(description="The stock ticker symbol (e.g. AAPL)")


@registry.register(
    name="get_market_data",
    description="Get recent price history, fundamentals, financials, and balance sheet for a stock from multiple reliable sources.",
    parameters={
        "type": "object",
        "properties": {
            "ticker": {
                "type": "string",
                "description": "The stock ticker symbol (e.g., AAPL)",
            }
        },
        "required": ["ticker"],
    },
    tier=0,
    source="data_rotator",
    input_model=TickerInput,
)
async def get_market_data(ticker: str) -> str:
    from app.collectors.data_rotator import (
        fetch_price_history,
        fetch_fundamentals,
        fetch_financials,
        fetch_balance_sheet,
    )
    from app.services.api_rate_limiter import rate_limiter

    # READ-THROUGH, not fetch-then-read. These four sequential network calls
    # ran on EVERY invocation and made this a 20.9s-median tool against a 30s
    # bridge deadline (p95 36.0s, 24% of calls aborted). The cycle's precollect
    # has normally just written price + fundamentals for this ticker.
    from app.collectors.data_rotator import _is_missing_recent_session
    from app.db import mongo_store as _ms
    from app.tools.read_through import refresh_within_budget

    # Freshness for a DAILY BAR is "is a session missing", not "how many hours
    # old". A wall-clock threshold calls Monday's close stale by Tuesday
    # pre-market and re-fetches a bar that does not exist yet — measured 30.1h
    # "stale" for AAPL while its 08-24 bar was the newest session in existence.
    # `_is_missing_recent_session` counts real sessions its market PEERS hold,
    # so it is right across holidays and foreign exchanges, and it already
    # guards the collector's own fallback chain.
    _have_rows = bool(_ms.find_docs("price_history", {"ticker": ticker}, limit=1))
    if not _have_rows or _is_missing_recent_session(ticker):
        async def _refresh_market():
            async with rate_limiter.acquire("yfinance"):
                await fetch_price_history(ticker)
                await fetch_fundamentals(ticker)
                await fetch_financials(ticker)
                await fetch_balance_sheet(ticker)

        await refresh_within_budget(f"market_data:{ticker}", _refresh_market)

    sections = []

    from app.db import mongo_store, mongo_query

    # P/E BASIS — which window, and did the vendors agree?
    #
    # The row below is whichever source wrote LAST. That decided the desk's
    # P/E, and on 2026-08-20 it handed COF a 12.2 while finviz showed 79: two
    # correct numbers over two different trailing-twelve-month windows (the
    # Discover charge quarter had rolled off one and not the other). 16 of the
    # 501 tickers carrying two sources disagree by >=2x, and which vendor is
    # right FLIPS by ticker, so this states the window instead of picking a
    # favourite. Advisory text only — it changes no field the desk trades on.
    try:
        from app.quant import pe_basis
        _pe_rows = mongo_store.find_docs(
            'fundamentals', {'ticker': ticker},
            sort=[('snapshot_date', -1)], limit=25)
        # No price is read here on purpose: `price_history` is keyed by
        # vendor and the sources disagree ~20%, so resolve_pe falls back to
        # market cap over TTM net income rather than importing that error.
        _resolved = pe_basis.resolve_pe(ticker, rows=_pe_rows)
        if _resolved.get('value') is not None:
            sections.append("P/E basis: " + pe_basis.describe(_resolved))
    except Exception as _pe_err:  # noqa: BLE001
        # Never cost the report its fundamentals over an advisory line.
        logger.debug("[finance_tools] %s: P/E basis unavailable: %s", ticker, _pe_err)

    # Fundamentals
    rows = mongo_query.find_rows('fundamentals', {'ticker': ticker}, ['snapshot_date', 'market_cap', 'pe_ratio', 'forward_pe', 'peg_ratio', 'price_to_book', 'profit_margin', 'roe', 'revenue', 'revenue_growth', 'debt_to_equity', 'beta', 'week_52_high', 'week_52_low', 'short_float_pct'], sort=[('snapshot_date', -1)], limit=1)
    sections.append(
        format_db_section(
            "Fundamentals",
            rows,
            [
                "Date",
                "MarketCap",
                "PE",
                "ForwardPE",
                "PEG",
                "P/B",
                "NetMargin",
                "ROE",
                "Revenue",
                "RevenueGrowth",
                "D/E",
                "Beta",
                "52wHigh",
                "52wLow",
                "ShortFloat%",
            ],
        )
    )

    # Quarterly Financials
    q_docs = mongo_store.find_docs(
        "financial_history",
        {"ticker": ticker, "period_type": "quarterly"},
        sort=[("period_end", -1)],
        limit=4,
    )
    if q_docs:
        q_lines = ["\n## Recent Quarterly Financials"]
        for d in q_docs:
            rev = fmt_usd(d.get("revenue")) if d.get("revenue") else "N/A"
            ni = fmt_usd(d.get("net_income")) if d.get("net_income") else "N/A"
            eps = f"EPS=${float(d.get('eps')):.2f}" if d.get("eps") is not None else ""
            q_lines.append(f"  {d.get('period_end')}: Rev={rev}, Net Income={ni}, {eps}")
        sections.append("\n".join(q_lines))

    # Annual Financials
    a_docs = mongo_store.find_docs(
        "financial_history",
        {"ticker": ticker, "period_type": "annual"},
        sort=[("period_end", -1)],
        limit=4,
    )
    if a_docs:
        a_lines = ["\n## Recent Annual Financials"]
        for d in a_docs:
            rev = fmt_usd(d.get("revenue")) if d.get("revenue") else "N/A"
            ni = fmt_usd(d.get("net_income")) if d.get("net_income") else "N/A"
            eps = f"EPS=${float(d.get('eps')):.2f}" if d.get("eps") is not None else ""
            a_lines.append(f"  {d.get('period_end')}: Rev={rev}, Net Income={ni}, {eps}")
        sections.append("\n".join(a_lines))

    return "\n".join(sections)


@registry.register(
    name="get_finnhub_news",
    description="Get the latest news articles for a stock from Finnhub.",
    parameters={
        "type": "object",
        "properties": {
            "ticker": {"type": "string", "description": "The stock ticker symbol"}
        },
        "required": ["ticker"],
    },
    tier=0,
    source="finnhub",
    input_model=TickerInput,
)
async def get_finnhub_news(ticker: str) -> str:
    from app.collectors.news_collector import collect_finnhub_news
    from app.services.api_rate_limiter import rate_limiter
    from datetime import datetime, timezone, timedelta
    from app.db import mongo_store

    from app.tools.read_through import refresh_within_budget, store_can_answer

    def _fetch(days: int):
        since = datetime.now(timezone.utc) - timedelta(days=days)
        docs = mongo_store.find_docs(
            "news_articles",
            {"ticker": ticker, "published_at": {"$gte": since}},
            sort=[("published_at", -1)],
            limit=15,
        )
        return [
            (
                d.get("id"),
                d.get("title"),
                d.get("publisher"),
                d.get("published_at"),
                d.get("summary"),
            )
            for d in docs
        ]

    # READ FIRST. During a cycle the precollect phase has already run this
    # exact collector for this exact ticker, so the network trip below is
    # usually redundant — and it was costing 36s at p50 / 65s at p95 against a
    # 60s bridge deadline (16% of calls failed outright).
    def _order_for_reading(rows: list) -> list:
        """Headline-named articles first, then recency.

        `published_at DESC` alone made the newest passing mention the lead
        item: NVDA's top article was an Asian market wrap, AAPL's an essay on
        central banking (measured 2026-08-25 — only 19.7% of stored articles
        name their ticker in the headline). Within each band recency still
        rules, so nothing gets buried — off-topic rows just stop outranking
        on-topic ones.
        """
        try:
            from app.services.watch_desk import _title_names_ticker

            return sorted(
                rows,
                key=lambda r: (not _title_names_ticker(ticker, r[1] or ""),
                               -(r[3].timestamp() if r[3] else 0)),
            )
        except Exception:
            return rows

    window_days = 14
    rows = _fetch(window_days)

    NEWS_MAX_AGE_H = 6.0
    _newest = max((r[3] for r in rows if r[3]), default=None)
    if not store_can_answer(_newest, NEWS_MAX_AGE_H, have_rows=len(rows) >= 3):
        async def _collect():
            async with rate_limiter.acquire("finnhub"):
                await collect_finnhub_news(ticker)

        await refresh_within_budget(f"finnhub_news:{ticker}", _collect)
        rows = _fetch(window_days)

    if len(rows) < 3:
        window_days = 90
        rows = _fetch(window_days)

    if not rows:
        return (
            f"No news published for {ticker} in the last {window_days} days. "
            "Treat this as an ABSENCE of coverage, not as neutral sentiment — "
            "do not infer a catalyst either way."
        )

    freshest = max(r[3] for r in rows if r[3]) if any(r[3] for r in rows) else None
    staleness_note = ""
    if freshest is not None:
        from datetime import datetime, timezone
        _now = datetime.now(timezone.utc)
        _f = freshest if freshest.tzinfo else freshest.replace(tzinfo=timezone.utc)
        age_days = (_now - _f).days
        if age_days >= 3:
            staleness_note = (
                f"\n\n⚠️ COLD NEWS: the most recent article for {ticker} is "
                f"{age_days} days old (window searched: {window_days}d). "
                f"There is no fresh catalyst here.\n"
            )

    # Grounded facts instead of raw scrape text where available. The COALESCE
    # that used to sit above is gone (it was NOT empty for 100% of rows —
    # 640 survive), so this is raw `summary` — ~2.3k chars of scrape (often leading
    # with nav chrome) per article, 15 articles per call. Facts compress that
    # ~5-8x and every quote is offset-verified against the source. Fail-open:
    # articles without facts fall back to (truncated) raw text.
    facts_by_id: dict = {}
    render_facts_line = None
    try:
        from app.services.news_extraction import ensure_facts, render_facts_line

        facts_by_id = await ensure_facts(
            [(r[0], ticker, r[1], r[4]) for r in rows]
        )
    except Exception as e:
        logger.warning("[news] grounded extraction unavailable: %s", e)

    display_rows = []
    rows = _order_for_reading(rows)
    for r in rows:
        article_id, title, publisher, published_at, body = r
        if article_id in facts_by_id and render_facts_line is not None:
            body = render_facts_line(facts_by_id[article_id])
        elif body and len(body) > 400:
            # Raw fallback: cap the dump — the old path pasted up to the full
            # scrape into one table cell.
            body = body[:400] + "…"
        display_rows.append((title, publisher, published_at, body))

    return staleness_note + format_db_section(
        "Recent News", display_rows, ["Title", "Publisher", "Date", "Key Facts"]
    )


@registry.register(
    name="get_technical_indicators",
    description="Get computed technical indicators (RSI, MACD, SMA, Bollinger Bands).",
    parameters={
        "type": "object",
        "properties": {
            "ticker": {"type": "string", "description": "The stock ticker symbol"}
        },
        "required": ["ticker"],
    },
    tier=0,
    source="computed",
    input_model=TickerInput,
)
async def get_technical_indicators(ticker: str) -> str:
    from app.processors.technical_processor import get_signals

    # Assumes price history already populated by yfinance tool OR we trigger it if missing!
    # Wait, technical processor automatically computes it from DB price_history.
    from app.processors.technical_processor import compute_technicals

    try:
        compute_technicals(ticker)
    except Exception:
        pass

    signals = get_signals(ticker)
    return signals if signals else "No technical signals available."


@registry.register(
    name="get_institutional_holdings",
    description="Get institutional hedge fund ownership data for a stock. Shows which top hedge funds hold it, position sizes, new positions, quarterly momentum, and whether top-performing funds are invested.",
    parameters={
        "type": "object",
        "properties": {
            "ticker": {
                "type": "string",
                "description": "The stock ticker symbol (e.g., AAPL)",
            }
        },
        "required": ["ticker"],
    },
    tier=0,
    source="sec_13f",
    input_model=TickerInput,
)
async def get_institutional_holdings(ticker: str) -> str:
    """Query SEC 13F institutional holdings data for a ticker.

    Returns a markdown summary of which top hedge funds hold this stock,
    how their positions have changed, and whether top-performing funds
    have conviction in it.
    """
    from app.collectors.fund_scanner import get_institutional_signal, get_fund_momentum

    signal = get_institutional_signal(ticker)
    momentum = get_fund_momentum(ticker)

    if signal["fund_count"] == 0:
        return f"No tracked institutional hedge fund holds {ticker} in their latest 13F filing."

    lines = [f"## Institutional Holdings: {ticker}"]
    lines.append(f"**{signal['fund_count']} tracked hedge fund(s)** hold this stock.")
    lines.append(
        f"Total institutional value: ${signal['total_institutional_value']:,.0f}"
    )

    if signal["has_top_performer"]:
        lines.append(
            f"⭐ **Top-performing fund(s):** {', '.join(signal['top_performer_names'])}"
        )

    lines.append(f"Institutional momentum: **{signal['momentum']}**")

    if signal["has_new_position"]:
        lines.append("🆕 At least one fund opened a **new position** this quarter.")

    # Top holders table
    if signal["holders"]:
        lines.append("\n| Fund | Shares | Value | New? | Chg% |")
        lines.append("|------|--------|-------|------|------|")
        for h in signal["holders"][:7]:
            val_fmt = f"${h['value_usd']:,.0f}" if h["value_usd"] else "$0"
            new_flag = "🆕" if h["is_new"] else ""
            chg = f"{h['pct_change']:+.1f}%" if h["pct_change"] else "N/A"
            lines.append(
                f"| {h['fund']} | {h['shares']:,} | {val_fmt} | {new_flag} | {chg} |"
            )

    # Quarterly momentum
    if momentum["direction"] != "NO_HISTORY":
        lines.append(f"\n**Quarterly Momentum ({momentum['latest_quarter']} vs {momentum['previous_quarter']}):** {momentum['direction']}")
        if momentum["new_buyers"]:
            lines.append(f"  New buyers: {', '.join(momentum['new_buyers'][:5])}")
        if momentum["exiters"]:
            lines.append(f"  Exited: {', '.join(momentum['exiters'][:5])}")
        if momentum["net_share_change"]:
            lines.append(f"  Net share change: {momentum['net_share_change']:+,}")

    return "\n".join(lines)


@registry.register(
    name="get_ticker_summary",
    description="Get a one-call fundamentals snapshot for a ticker: name, sector/industry, market cap, P/E and forward P/E, 52-week range, average volume, dividend yield, analyst price target, and next earnings date.",
    parameters={
        "type": "object",
        "properties": {
            "ticker": {"type": "string", "description": "The stock ticker symbol (e.g., AAPL)"}
        },
        "required": ["ticker"],
    },
    tier=0,
    source="yfinance",
    input_model=TickerInput,
)
async def get_ticker_summary(ticker: str) -> str:
    """Consolidated fundamentals summary (ported from tradingchart-service data_proxy /api/summary)."""
    import datetime as _dt

    import yfinance as yf

    from app.services.api_rate_limiter import rate_limiter

    ticker = ticker.upper().strip()
    async with rate_limiter.acquire("yfinance"):
        info = yf.Ticker(ticker).info or {}

    earnings_date = None
    ts = info.get("earningsTimestamp")
    if ts:
        try:
            earnings_date = _dt.datetime.fromtimestamp(ts).strftime("%Y-%m-%d")
        except Exception:
            earnings_date = None

    def _num(v, money=False):
        if v is None:
            return "N/A"
        try:
            return fmt_usd(v) if money else f"{v:,.2f}" if isinstance(v, float) else f"{v:,}"
        except Exception:
            return str(v)

    lines = [
        f"## {info.get('longName') or info.get('shortName') or ticker} ({ticker})",
        f"Sector: {info.get('sector', 'N/A')} | Industry: {info.get('industry', 'N/A')}",
        f"Market cap: {_num(info.get('marketCap'), money=True)}",
        f"P/E: {_num(info.get('trailingPE'))} | Forward P/E: {_num(info.get('forwardPE'))}",
        f"52-week range: {_num(info.get('fiftyTwoWeekLow'))} – {_num(info.get('fiftyTwoWeekHigh'))}",
        f"Average volume: {_num(info.get('averageVolume'))}",
        f"Dividend yield: {_num(info.get('dividendYield'))}",
        f"Analyst mean target: {_num(info.get('targetMeanPrice'))}",
        f"Next earnings date: {earnings_date or 'N/A'}",
    ]
    return "\n".join(lines)
