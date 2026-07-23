"""
Research scheduling tools — let agents snipe research at the right time.

All creation goes through the Research Governor (app/services/research_governor),
which enforces hard caps, per-ticker dedupe/cooldowns, and TTLs so the system
never doom-loops on the same stocks or stacks unbounded work. The intended
workflow for an agent:

  1. get_upcoming_events(ticker)      — when does the catalyst drop?
  2. list_scheduled_research()        — is it already covered?
  3. schedule_research(...)           — one-shot snipe just after the event
     or request_research_now(...)     — if the catalyst already hit
"""

import json
import logging

from app.tools.registry import registry, PermissionLevel
from app.tools.tool_context import current_agent_name

logger = logging.getLogger(__name__)


@registry.register(
    name="request_research_now",
    description=(
        "Queue an immediate research-only cycle (collect + analyze, NEVER trades) on up to 5 tickers. "
        "Use ONLY when a catalyst already hit and the current data is insufficient. "
        "Governor enforces: max 2 queued at once, 4h per-ticker cooldown, dedupe against scheduled research. "
        "Be picky — routine curiosity will be rejected; check list_scheduled_research first."
    ),
    parameters={
        "type": "object",
        "properties": {
            "tickers": {
                "type": "array",
                "items": {"type": "string"},
                "description": "1-5 ticker symbols, highest conviction only.",
            },
            "reason": {
                "type": "string",
                "description": "The specific catalyst/question this research must answer.",
            },
            "urgency": {
                "type": "string",
                "enum": ["low", "medium", "high", "critical"],
                "description": "critical bypasses the recency cooldown — reserve for genuine breaking catalysts.",
            },
        },
        "required": ["tickers", "reason"],
    },
    tier=1,
    source="research_scheduling",
    permission=PermissionLevel.WRITE,
)
async def request_research_now(tickers: list, reason: str, urgency: str = "medium") -> str:
    from app.services.research_governor import request_research_now as _go
    agent = current_agent_name()
    logger.info("[ResearchTools] request_research_now by %s: %s (%s)", agent, tickers, reason)
    try:
        result = _go(tickers, reason, urgency=urgency)
        return json.dumps(result)
    except Exception as e:
        logger.error("[ResearchTools] request_research_now failed: %s", e)
        return json.dumps({"status": "error", "message": str(e)})


@registry.register(
    name="schedule_research",
    description=(
        "Schedule a ONE-SHOT research cycle (collect + analyze, NEVER trades) sniped to a specific DATED "
        "event. Use this ONLY for a known calendar catalyst (earnings, a Fed decision). For ongoing "
        "'keep an eye on this ticker' monitoring, DO NOT schedule — use set_watch instead (it watches "
        "price/news/etc. in cheap background code and wakes a cycle only when a condition trips). "
        "`when`: OMIT it to auto-snipe the ticker's next earnings (single ticker; the governor resolves "
        "the real date/time for you), OR pass an exact ISO-8601 UTC datetime (e.g. '2026-07-21T21:30:00Z') "
        "for a non-earnings event. Fires ONCE then auto-deactivates. Coarse market windows and "
        "'monitor' are rejected — use set_watch. Governor enforces max 5 active, 10/day, per-ticker "
        "dedupe, 4h cooldown; a vague reason is rejected."
    ),
    parameters={
        "type": "object",
        "properties": {
            "tickers": {
                "type": "array",
                "items": {"type": "string"},
                "description": "1-5 tickers. Omit `when` only for a SINGLE ticker (earnings auto-snipe).",
            },
            "when": {
                "type": "string",
                "description": "Omit to auto-snipe next earnings (single ticker), or an ISO-8601 UTC datetime for a dated event.",
            },
            "reason": {
                "type": "string",
                "description": "The specific event/catalyst being sniped and the question to answer (≥10 chars).",
            },
            "review_intent": {
                "type": "string",
                "enum": ["reassess", "trade_window", "event_followup"],
                "description": "Why this research exists. Default event_followup. (For monitoring, use set_watch.)",
            },
            "urgency": {
                "type": "string",
                "enum": ["low", "medium", "high", "critical"],
            },
            "reason_codes": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Short catalyst codes, e.g. ['earnings_2026-07-21', 'guidance_cut'].",
            },
        },
        "required": ["tickers", "reason"],
    },
    tier=1,
    source="research_scheduling",
    permission=PermissionLevel.WRITE,
)
async def schedule_research(
    tickers: list,
    when: str | None = None,
    reason: str = "",
    review_intent: str = "event_followup",
    urgency: str = "medium",
    reason_codes: list | None = None,
) -> str:
    from app.services.research_governor import schedule_research as _go
    agent = current_agent_name()
    logger.info("[ResearchTools] schedule_research by %s: %s when=%s (%s)", agent, tickers, when, reason)
    try:
        result = await _go(
            tickers, when, reason,
            review_intent=review_intent, urgency=urgency, reason_codes=reason_codes,
        )
        return json.dumps(result)
    except Exception as e:
        logger.error("[ResearchTools] schedule_research failed: %s", e)
        return json.dumps({"status": "error", "message": str(e)})


@registry.register(
    name="list_scheduled_research",
    description=(
        "List all pending/scheduled bot research (active schedules + queued immediate research) plus "
        "tickers researched in the last 48h and the governor's limits. ALWAYS check this before "
        "requesting or scheduling research — duplicates are rejected."
    ),
    parameters={"type": "object", "properties": {}},
    tier=1,
    source="research_scheduling",
    permission=PermissionLevel.READ_ONLY,
)
async def list_scheduled_research() -> str:
    from app.services.research_governor import list_scheduled_research as _go
    try:
        return json.dumps(_go(), default=str)
    except Exception as e:
        logger.error("[ResearchTools] list_scheduled_research failed: %s", e)
        return json.dumps({"status": "error", "message": str(e)})


@registry.register(
    name="cancel_scheduled_research",
    description=(
        "Cancel a bot-created research schedule (sch-bot-* id from list_scheduled_research) that is no "
        "longer worth running — e.g. the thesis changed or the event passed. Frees a slot under the "
        "5-active cap."
    ),
    parameters={
        "type": "object",
        "properties": {
            "schedule_id": {"type": "string", "description": "The sch-bot-* schedule id."},
            "reason": {"type": "string", "description": "Why it is no longer needed."},
        },
        "required": ["schedule_id"],
    },
    tier=1,
    source="research_scheduling",
    permission=PermissionLevel.WRITE,
)
async def cancel_scheduled_research(schedule_id: str, reason: str = "") -> str:
    from app.services.research_governor import cancel_scheduled_research as _go
    agent = current_agent_name()
    logger.info("[ResearchTools] cancel_scheduled_research by %s: %s (%s)", agent, schedule_id, reason)
    try:
        return json.dumps(_go(schedule_id, reason))
    except Exception as e:
        logger.error("[ResearchTools] cancel_scheduled_research failed: %s", e)
        return json.dumps({"status": "error", "message": str(e)})


@registry.register(
    name="get_upcoming_events",
    description=(
        "Get upcoming scheduled events for a ticker over the next 90 days — currently earnings dates with "
        "report timing (bmo = before market open, amc = after market close) and EPS/revenue estimates. "
        "Use this to time-snipe research: e.g. earnings on 2026-07-21 amc → schedule_research with "
        "when='2026-07-21T21:30:00Z' so the analysis runs on the fresh numbers, not before them."
    ),
    parameters={
        "type": "object",
        "properties": {
            "ticker": {"type": "string", "description": "The stock ticker."},
        },
        "required": ["ticker"],
    },
    tier=1,
    source="research_scheduling",
    permission=PermissionLevel.READ_ONLY,
)
async def get_upcoming_events(ticker: str) -> str:
    from app.collectors.finnhub_collector import collect_earnings_calendar
    ticker = (ticker or "").upper().strip()
    try:
        events = await collect_earnings_calendar(ticker)
        macro = _upcoming_macro_events()
        if not events:
            return json.dumps({
                "ticker": ticker,
                "events": [],
                "macro_events": macro,
                "note": "No scheduled earnings in the next 90 days.",
            })
        slim = [
            {
                "date": e.get("date"),
                "hour": e.get("hour") or "unknown",  # bmo / amc / dmh
                "eps_estimate": e.get("epsEstimate"),
                "revenue_estimate": e.get("revenueEstimate"),
                "quarter": e.get("quarter"),
                "year": e.get("year"),
            }
            for e in events[:8]
        ]
        return json.dumps({
            "ticker": ticker,
            "events": slim,
            "macro_events": macro,
            "hint": "bmo=before market open (snipe at ~14:00 UTC same day); "
                    "amc=after market close (snipe at ~21:30 UTC same day or next_pre_market).",
        })
    except Exception as e:
        logger.error("[ResearchTools] get_upcoming_events failed for %s: %s", ticker, e)
        return json.dumps({"status": "error", "message": str(e)})


def _upcoming_macro_events(limit: int = 8) -> list[dict]:
    """High/medium-importance market-wide events from the economic_calendar
    table (populated by the tradingeconomics collector every 12h). Empty list
    when the table has no future rows — never blocks the earnings answer."""
    try:
        from app.db.connection import get_db

        with get_db() as db:
            rows = db.execute(
                "SELECT event_date, event_name, country, importance, forecast, previous "
                "FROM economic_calendar "
                "WHERE event_date >= CURRENT_DATE AND importance IN ('high', 'medium') "
                "ORDER BY event_date ASC LIMIT %s",
                [limit],
            ).fetchall()
        return [
            {
                "date": str(r[0]),
                "event": r[1],
                "country": r[2],
                "importance": r[3],
                "forecast": r[4],
                "previous": r[5],
            }
            for r in rows
        ]
    except Exception:
        return []
