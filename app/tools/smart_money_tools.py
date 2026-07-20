"""
Smart-money agent tools — congressional disclosures and 13F fund filings as
research leads.

These read the materialized output of app/analytics/returns_engine.py, which is
the SAME source the dashboard reads. That is deliberate: an agent being told a
different number than the UI displays is a debugging nightmare and erodes trust
in both.

Every performance figure is real alpha vs SPY. Where we cannot compute one, the
tool says so explicitly rather than emitting a placeholder — an agent that reads
"0.0% return" will treat it as a measurement, not a gap.
"""

import logging

from lazycat.tool_registry import registry
from app.db.connection import get_db

logger = logging.getLogger(__name__)

# Disclosure is inherently stale: congress files up to 45 days after the trade,
# 13Fs land ~45 days after quarter end. Any lead older than this is history, not
# a signal, so we tell the agent the age and let it discount accordingly.
STALE_AFTER_DAYS = 120


def _fmt_pct(value: float | None) -> str:
    return "n/a" if value is None else f"{value:+.1f}%"


def _fmt_usd(value: float | None) -> str:
    return "n/a" if value is None else f"${value:,.0f}"


@registry.register(
    name="get_smart_money_signal",
    description=(
        "Get smart-money activity for a specific stock: which members of Congress "
        "and which hedge funds bought or sold it, when it became public, and how "
        "well those actors have historically performed (real alpha vs SPY). Use "
        "this to check whether informed money is accumulating or exiting a ticker."
    ),
    parameters={
        "type": "object",
        "properties": {
            "ticker": {
                "type": "string",
                "description": "The stock ticker symbol (e.g., NVDA)",
            },
            "days": {
                "type": "integer",
                "description": "How far back to look for disclosures. Default 365.",
            },
        },
        "required": ["ticker"],
    },
    tier=0,
    source="smart_money",
)
async def get_smart_money_signal(ticker: str, days: int = 365) -> str:
    ticker = (ticker or "").upper().strip()
    if not ticker:
        return "No ticker provided."

    with get_db() as db:
        rows = db.execute(
            """
            SELECT s.actor_type, s.actor_name, s.direction, s.event_date,
                   s.size_est_usd, s.size_confidence, s.alpha_1y,
                   p.avg_alpha, p.win_rate, p.scored_count, p.rankable
            FROM smart_money_trade_scores s
            LEFT JOIN smart_money_performance p
              ON p.actor_type = s.actor_type
             AND p.actor_id   = s.actor_id
             AND p.horizon    = '1y'
            WHERE s.ticker = %s
              AND s.event_date >= CURRENT_DATE - MAKE_INTERVAL(days => %s)
            ORDER BY s.event_date DESC
            LIMIT 40
            """,
            (ticker, days),
        ).fetchall()

        summary = db.execute(
            """
            SELECT
                COUNT(DISTINCT actor_id) FILTER (WHERE direction = 'buy'
                    AND actor_type = 'congress')                       AS congress_buyers,
                COUNT(DISTINCT actor_id) FILTER (WHERE direction = 'sell'
                    AND actor_type = 'congress')                       AS congress_sellers,
                COUNT(DISTINCT actor_id) FILTER (WHERE direction = 'buy'
                    AND actor_type = 'fund')                           AS fund_buyers,
                COUNT(DISTINCT actor_id) FILTER (WHERE direction = 'sell'
                    AND actor_type = 'fund')                           AS fund_sellers,
                MAX(event_date)                                        AS latest,
                CURRENT_DATE - MAX(event_date)                         AS days_since
            FROM smart_money_trade_scores
            WHERE ticker = %s
              AND event_date >= CURRENT_DATE - MAKE_INTERVAL(days => %s)
            """,
            (ticker, days),
        ).fetchone()

    if not rows:
        return (
            f"No congressional or 13F smart-money activity recorded for {ticker} "
            f"in the last {days} days. Absence of a signal is not a bearish "
            f"signal — it may simply mean no tracked actor disclosed a trade."
        )

    c_buy, c_sell, f_buy, f_sell, latest, days_since = summary
    days_since = int(days_since) if days_since is not None else None

    lines = [f"## Smart Money: {ticker} (last {days}d)", ""]
    lines.append(
        f"**Congress:** {c_buy or 0} distinct buyers, {c_sell or 0} sellers"
    )
    lines.append(f"**Hedge funds (13F):** {f_buy or 0} buyers, {f_sell or 0} sellers")

    if days_since is not None:
        freshness = "FRESH" if days_since <= STALE_AFTER_DAYS else "STALE"
        lines.append(
            f"**Most recent disclosure:** {latest} ({days_since}d ago — {freshness})"
        )
        if freshness == "STALE":
            lines.append(
                "> Treat as historical context, not an actionable signal: "
                "the position may have changed since it was disclosed."
            )

    lines.append("")
    lines.append("### Individual disclosures")
    lines.append("| Actor | Type | Action | Public on | Size | Their 1y alpha | Sample |")
    lines.append("|---|---|---|---|---|---|---|")

    for (
        actor_type, actor_name, direction, event_date, size_est,
        size_conf, _alpha_1y, actor_alpha, _win, scored, rankable,
    ) in rows[:20]:
        # An actor's track record is only worth quoting if it cleared the
        # minimum sample size; otherwise we show the count and no number.
        if actor_alpha is not None and rankable:
            track = _fmt_pct(actor_alpha)
        else:
            track = "insufficient history"

        size = _fmt_usd(size_est)
        if size_conf == "bound":
            size += " (min)"
        elif size_conf == "range":
            size += " (est)"

        lines.append(
            f"| {actor_name} | {actor_type} | {direction.upper()} | {event_date} "
            f"| {size} | {track} | n={scored or 0} |"
        )

    return "\n".join(lines)


@registry.register(
    name="get_smart_money_leads",
    description=(
        "Find stocks that multiple members of Congress and/or multiple hedge funds "
        "have recently bought — ranked research leads based on consensus buying by "
        "informed money. Use this to discover candidate tickers worth researching "
        "when you have no specific ticker in mind."
    ),
    parameters={
        "type": "object",
        "properties": {
            "days": {
                "type": "integer",
                "description": "Lookback window for disclosures. Default 180.",
            },
            "min_buyers": {
                "type": "integer",
                "description": "Minimum distinct buyers required. Default 3.",
            },
            "source": {
                "type": "string",
                "enum": ["congress", "fund", "both"],
                "description": "Which cohort to mine. Default 'both'.",
            },
            "limit": {
                "type": "integer",
                "description": "Max leads to return. Default 15.",
            },
        },
        "required": [],
    },
    tier=0,
    source="smart_money",
)
async def get_smart_money_leads(
    days: int = 180,
    min_buyers: int = 3,
    source: str = "both",
    limit: int = 15,
) -> str:
    source = source if source in ("congress", "fund", "both") else "both"
    limit = max(1, min(int(limit or 15), 50))

    type_clause = "" if source == "both" else "AND s.actor_type = %(actor_type)s"

    # Rank by conviction-weighted buying, but weight each actor by whether they
    # have a real track record — consensus among proven performers is a stronger
    # lead than consensus among actors we cannot score.
    sql = f"""
        SELECT
            s.ticker,
            COUNT(DISTINCT s.actor_id) FILTER (WHERE s.direction = 'buy')  AS buyers,
            COUNT(DISTINCT s.actor_id) FILTER (WHERE s.direction = 'sell') AS sellers,
            COUNT(DISTINCT s.actor_id) FILTER (
                WHERE s.direction = 'buy' AND p.rankable AND p.avg_alpha > 0
            ) AS proven_buyers,
            SUM(s.size_est_usd) FILTER (WHERE s.direction = 'buy')         AS buy_value,
            MAX(s.event_date)                                              AS latest,
            STRING_AGG(DISTINCT s.actor_type, '+')                         AS cohorts
        FROM smart_money_trade_scores s
        LEFT JOIN smart_money_performance p
          ON p.actor_type = s.actor_type
         AND p.actor_id   = s.actor_id
         AND p.horizon    = '1y'
        WHERE s.event_date >= CURRENT_DATE - MAKE_INTERVAL(days => %(days)s)
          {type_clause}
        GROUP BY s.ticker
        HAVING COUNT(DISTINCT s.actor_id) FILTER (WHERE s.direction = 'buy') >= %(min_buyers)s
        ORDER BY proven_buyers DESC, buyers DESC, buy_value DESC NULLS LAST
        LIMIT %(limit)s
    """

    params = {
        "days": days,
        "min_buyers": min_buyers,
        "limit": limit,
        "actor_type": source,
    }

    with get_db() as db:
        rows = db.execute(sql, params).fetchall()

    if not rows:
        return (
            f"No tickers met the threshold of {min_buyers}+ distinct buyers in the "
            f"last {days} days for source '{source}'. Try lowering min_buyers or "
            f"widening the window."
        )

    lines = [
        f"## Smart Money Leads — {source}, last {days}d, {min_buyers}+ buyers",
        "",
        "Ranked by number of buyers with a POSITIVE proven track record "
        "(real 1y alpha vs SPY, minimum sample size), then by total buyers.",
        "",
        "| Ticker | Buyers | Proven buyers | Sellers | Buy value | Cohorts | Latest |",
        "|---|---|---|---|---|---|---|",
    ]

    for ticker, buyers, sellers, proven, buy_value, latest, cohorts in rows:
        lines.append(
            f"| {ticker} | {buyers} | {proven or 0} | {sellers or 0} "
            f"| {_fmt_usd(buy_value)} | {cohorts} | {latest} |"
        )

    lines.append("")
    lines.append(
        "> These are leads, not recommendations. Disclosure lags the actual trade "
        "by up to 45 days, and consensus buying reflects past conviction."
    )
    return "\n".join(lines)


@registry.register(
    name="get_smart_money_leaderboard",
    description=(
        "Get the best-performing members of Congress or hedge funds ranked by real "
        "risk-adjusted performance (alpha vs SPY), with sample sizes. Use this to "
        "judge how much weight to give a particular actor's trades."
    ),
    parameters={
        "type": "object",
        "properties": {
            "actor_type": {
                "type": "string",
                "enum": ["congress", "fund"],
                "description": "Which cohort to rank. Default 'congress'.",
            },
            "horizon": {
                "type": "string",
                "enum": ["1m", "3m", "6m", "1y"],
                "description": "Forward return window. Default '1y'.",
            },
            "limit": {"type": "integer", "description": "Max rows. Default 15."},
        },
        "required": [],
    },
    tier=0,
    source="smart_money",
)
async def get_smart_money_leaderboard(
    actor_type: str = "congress", horizon: str = "1y", limit: int = 15
) -> str:
    actor_type = actor_type if actor_type in ("congress", "fund") else "congress"
    horizon = horizon if horizon in ("1m", "3m", "6m", "1y") else "1y"
    limit = max(1, min(int(limit or 15), 50))

    with get_db() as db:
        rows = db.execute(
            """
            SELECT actor_name, avg_alpha, avg_return, win_rate,
                   scored_count, coverage_pct
            FROM smart_money_performance
            WHERE actor_type = %s AND horizon = %s
              AND rankable IS TRUE AND avg_alpha IS NOT NULL
            ORDER BY avg_alpha DESC
            LIMIT %s
            """,
            (actor_type, horizon, limit),
        ).fetchall()

    if not rows:
        return (
            f"No ranked performance data for {actor_type} at {horizon}. "
            f"Returns may not have been computed yet."
        )

    lines = [
        f"## Smart Money Leaderboard — {actor_type}, {horizon} alpha vs SPY",
        "",
        "| Actor | Alpha | Raw return | Win rate | Scored trades | Coverage |",
        "|---|---|---|---|---|---|",
    ]
    for name, alpha, raw, win, scored, cov in rows:
        win_s = "n/a" if win is None else f"{win:.0f}%"
        cov_s = "n/a" if cov is None else f"{cov:.0f}%"
        lines.append(
            f"| {name} | {_fmt_pct(alpha)} | {_fmt_pct(raw)} | {win_s} "
            f"| {scored} | {cov_s} |"
        )

    lines.append("")
    lines.append(
        "> Alpha is excess return vs SPY over the same window, measured from the "
        "date each trade became public. Only actors meeting a minimum sample size "
        "are ranked."
    )
    return "\n".join(lines)
