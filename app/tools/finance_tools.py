from pydantic import BaseModel, Field
from app.tools.registry import registry
from app.db.connection import get_db
from app.utils.text_utils import format_db_section, fmt_usd


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

    # Still acquire yfinance semaphore just to be safe as it's the primary target
    async with rate_limiter.acquire("yfinance"):
        await fetch_price_history(ticker)
        await fetch_fundamentals(ticker)
        await fetch_financials(ticker)
        await fetch_balance_sheet(ticker)

    with get_db() as db:
        sections = []

        # Fundamentals
        rows = db.execute(
            """
            SELECT snapshot_date, market_cap, pe_ratio, forward_pe, peg_ratio,
                   price_to_book, profit_margin, roe, revenue, revenue_growth,
                   debt_to_equity, beta, week_52_high, week_52_low, short_float_pct
            FROM fundamentals WHERE ticker = %s ORDER BY snapshot_date DESC LIMIT 1
        """,
            [ticker],
        ).fetchall()
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
                    "ProfitMargin",
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
        q_rows = db.execute(
            """
            SELECT period_end, revenue, gross_profit, operating_income, net_income, eps, free_cash_flow
            FROM financial_history 
            WHERE ticker = %s AND period_type = 'quarterly' 
            ORDER BY period_end DESC LIMIT 4
        """,
            [ticker],
        ).fetchall()
        if q_rows:
            q_lines = ["\n## Recent Quarterly Financials"]
            for row in q_rows:
                rev = fmt_usd(row[1]) if row[1] else "N/A"
                ni = fmt_usd(row[4]) if row[4] else "N/A"
                eps = f"EPS=${row[5]:.2f}" if row[5] else ""
                q_lines.append(f"  {row[0]}: Rev={rev}, Net Income={ni}, {eps}")
            sections.append("\n".join(q_lines))

        # Annual Financials
        a_rows = db.execute(
            """
            SELECT period_end, revenue, gross_profit, operating_income, net_income, eps, free_cash_flow
            FROM financial_history 
            WHERE ticker = %s AND period_type = 'annual' 
            ORDER BY period_end DESC LIMIT 4
        """,
            [ticker],
        ).fetchall()
        if a_rows:
            a_lines = ["\n## Recent Annual Financials"]
            for row in a_rows:
                rev = fmt_usd(row[1]) if row[1] else "N/A"
                ni = fmt_usd(row[4]) if row[4] else "N/A"
                eps = f"EPS=${row[5]:.2f}" if row[5] else ""
                a_lines.append(f"  {row[0]}: Rev={rev}, Net Income={ni}, {eps}")
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

    async with rate_limiter.acquire("finnhub"):
        await collect_finnhub_news(ticker)

    with get_db() as db:
        rows = db.execute(
            """
            SELECT title, publisher, published_at, COALESCE(llm_summary, summary)
            FROM news_articles WHERE ticker = %s ORDER BY published_at DESC LIMIT 15
        """,
            [ticker],
        ).fetchall()

    if not rows:
        return "No recent news found."

    return format_db_section(
        "Recent News", rows, ["Title", "Publisher", "Date", "Summary"]
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
