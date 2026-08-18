"""
Smart-money agent tools — congressional disclosures and 13F fund filings as research leads.

Pure MongoDB implementation for smart_money_trade_scores and smart_money_performance collections.
"""

import logging
from datetime import datetime, timedelta, timezone

from lazycat.tool_registry import registry
from app.db import mongo_store

logger = logging.getLogger(__name__)

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

    since = (datetime.now(timezone.utc) - timedelta(days=days)).date().isoformat()

    try:
        scores = mongo_store.find_docs(
            "smart_money_trade_scores",
            {
                "ticker": ticker,
                "event_date": {"$gte": since},
            },
            sort=[("event_date", -1)],
            limit=40,
        )

        perf_docs = mongo_store.find_docs(
            "smart_money_performance",
            {"horizon": "1y"},
        )
        perf_map = {(p.get("actor_type"), p.get("actor_id")): p for p in perf_docs}

        if not scores:
            return (
                f"No congressional or 13F smart-money activity recorded for {ticker} "
                f"in the last {days} days. Absence of a signal is not a bearish "
                f"signal — it may simply mean no tracked actor disclosed a trade."
            )

        congress_buyers = set()
        congress_sellers = set()
        fund_buyers = set()
        fund_sellers = set()
        latest_date = None

        rows = []
        for s in scores:
            a_type = s.get("actor_type")
            a_id = s.get("actor_id")
            direction = (s.get("direction") or "").lower()
            ev_date = str(s.get("event_date") or "")
            if not latest_date or ev_date > latest_date:
                latest_date = ev_date

            if a_type == "congress":
                if direction == "buy":
                    congress_buyers.add(a_id)
                elif direction == "sell":
                    congress_sellers.add(a_id)
            elif a_type == "fund":
                if direction == "buy":
                    fund_buyers.add(a_id)
                elif direction == "sell":
                    fund_sellers.add(a_id)

            p = perf_map.get((a_type, a_id), {})
            rows.append((
                a_type,
                s.get("actor_name", ""),
                direction,
                ev_date,
                s.get("size_est_usd"),
                s.get("size_confidence"),
                s.get("alpha_1y"),
                p.get("avg_alpha"),
                p.get("win_rate"),
                p.get("scored_count", 0),
                p.get("rankable", False),
            ))

        today_iso = datetime.now(timezone.utc).date().isoformat()
        days_since = None
        if latest_date:
            try:
                d_latest = datetime.fromisoformat(latest_date).date()
                d_today = datetime.fromisoformat(today_iso).date()
                days_since = (d_today - d_latest).days
            except Exception:
                pass

        lines = [f"## Smart Money: {ticker} (last {days}d)", ""]
        lines.append(f"**Congress:** {len(congress_buyers)} distinct buyers, {len(congress_sellers)} sellers")
        lines.append(f"**Hedge funds (13F):** {len(fund_buyers)} buyers, {len(fund_sellers)} sellers")

        if days_since is not None:
            freshness = "FRESH" if days_since <= STALE_AFTER_DAYS else "STALE"
            lines.append(f"**Most recent disclosure:** {latest_date} ({days_since}d ago — {freshness})")
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

    except Exception as e:
        logger.warning("[smart_money] get_smart_money_signal failed: %s", e)
        return f"Smart money lookup for {ticker} temporarily unavailable."


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
    since = (datetime.now(timezone.utc) - timedelta(days=days)).date().isoformat()

    try:
        query = {"event_date": {"$gte": since}}
        if source != "both":
            query["actor_type"] = source

        scores = mongo_store.find_docs("smart_money_trade_scores", query)
        perf_docs = mongo_store.find_docs("smart_money_performance", {"horizon": "1y"})
        perf_map = {(p.get("actor_type"), p.get("actor_id")): p for p in perf_docs}

        by_ticker: dict[str, dict] = {}
        for s in scores:
            t = s.get("ticker")
            if not t:
                continue
            if t not in by_ticker:
                by_ticker[t] = {
                    "buyers": set(),
                    "sellers": set(),
                    "proven_buyers": set(),
                    "buy_value": 0.0,
                    "latest": "",
                    "cohorts": set(),
                }
            entry = by_ticker[t]
            a_id = s.get("actor_id")
            a_type = s.get("actor_type")
            dir_str = (s.get("direction") or "").lower()
            ev_date = str(s.get("event_date") or "")

            if ev_date > entry["latest"]:
                entry["latest"] = ev_date
            if a_type:
                entry["cohorts"].add(a_type)

            if dir_str == "buy":
                entry["buyers"].add(a_id)
                size = float(s.get("size_est_usd") or 0.0)
                entry["buy_value"] += size

                p = perf_map.get((a_type, a_id), {})
                if p.get("rankable") and float(p.get("avg_alpha") or 0.0) > 0:
                    entry["proven_buyers"].add(a_id)
            elif dir_str == "sell":
                entry["sellers"].add(a_id)

        leads = []
        for t, data in by_ticker.items():
            n_buyers = len(data["buyers"])
            if n_buyers >= min_buyers:
                leads.append((
                    t,
                    n_buyers,
                    len(data["sellers"]),
                    len(data["proven_buyers"]),
                    data["buy_value"],
                    data["latest"],
                    "+".join(sorted(data["cohorts"])),
                ))

        leads.sort(key=lambda x: (x[3], x[1], x[4]), reverse=True)
        leads = leads[:limit]

        if not leads:
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

        for ticker, buyers, sellers, proven, buy_value, latest, cohorts in leads:
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

    except Exception as e:
        logger.warning("[smart_money] get_smart_money_leads failed: %s", e)
        return "Smart money leads temporarily unavailable."


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

    try:
        docs = mongo_store.find_docs(
            "smart_money_performance",
            {
                "actor_type": actor_type,
                "horizon": horizon,
                "rankable": True,
                "avg_alpha": {"$ne": None},
            },
            sort=[("avg_alpha", -1)],
            limit=limit,
        )

        if not docs:
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
        for d in docs:
            name = d.get("actor_name", "")
            alpha = d.get("avg_alpha")
            raw = d.get("avg_return")
            win = d.get("win_rate")
            scored = d.get("scored_count", 0)
            cov = d.get("coverage_pct")

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

    except Exception as e:
        logger.warning("[smart_money] get_smart_money_leaderboard failed: %s", e)
        return f"Leaderboard for {actor_type} temporarily unavailable."
