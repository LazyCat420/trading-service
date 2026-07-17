"""Watch Desk agent tools — let an agent leave 'wake me if…' notes on a ticker.

These are how the agent keeps itself OFF until something thesis-relevant happens.
After analyzing a ticker, the agent calls `watch_ticker` with the conditions under
which its view would need revisiting; the Watch Desk checks them in the background
(no LLM) and only wakes a targeted cycle when one trips. See
app/services/watch_desk.py.
"""

import json
import logging

from app.tools.registry import registry, PermissionLevel
from app.tools.tool_context import current_agent_name, current_cycle_id

logger = logging.getLogger(__name__)


@registry.register(
    name="watch_ticker",
    description=(
        "Leave a 'wake me if…' note on a ticker so the expensive agent stays OFF until a "
        "real, thesis-relevant condition trips — the Watch Desk checks it in code "
        "(no LLM) and only then re-runs a targeted cycle. Call this at the END of analysing a "
        "ticker with the conditions that would change your view. Supersedes any existing watch "
        "for the ticker (re-arm). Trigger types: "
        "price_above{level}, price_below{level} (your target / invalidation levels); "
        "pct_change{ref,pct,direction} (moved pct off a reference price; direction up|down|any); "
        "rsi{op,value} (op gt|lt); volume_spike{mult} (× 20d avg); "
        "news{categories} (earnings|guidance|downgrade|upgrade|mna|litigation|insider); "
        "staleness{max_days} (re-check backstop). Always include an invalidation level and a "
        "staleness backstop."
    ),
    parameters={
        "type": "object",
        "properties": {
            "ticker": {"type": "string", "description": "The ticker to watch."},
            "triggers": {
                "type": "array",
                "description": "List of trigger objects, e.g. [{\"type\":\"price_below\",\"level\":145.5}, "
                               "{\"type\":\"staleness\",\"max_days\":10}].",
                "items": {"type": "object"},
            },
            "reason": {"type": "string", "description": "Why you're watching (the thesis hook)."},
            "thesis_summary": {"type": "string", "description": "Optional 1-2 line current thesis to seed the wake."},
            "cooldown_minutes": {"type": "integer", "description": "Min minutes between wakes (debounce). Default 240."},
            "expiry_days": {"type": "integer", "description": "Auto-expire the watch after N days. Default 30."},
        },
        "required": ["ticker", "triggers"],
    },
    tier=1,
    source="watch_desk",
    permission=PermissionLevel.WRITE,
)
async def watch_ticker(
    ticker: str,
    triggers: list,
    reason: str = "",
    thesis_summary: str | None = None,
    cooldown_minutes: int = 240,
    expiry_days: int = 30,
    **_extra,
) -> str:
    from app.services.watch_desk import create_watch
    agent = current_agent_name()
    logger.info("[WatchDeskTools] watch_ticker by %s: %s (%d triggers)", agent, ticker, len(triggers or []))
    try:
        result = create_watch(
            ticker=ticker, triggers=triggers, reason=reason,
            thesis_summary=thesis_summary, cooldown_minutes=cooldown_minutes,
            expiry_days=expiry_days, source_cycle_id=current_cycle_id(),
        )
        return json.dumps(result)
    except Exception as e:
        logger.error("[WatchDeskTools] watch_ticker failed: %s", e)
        return json.dumps({"status": "error", "message": str(e)})


@registry.register(
    name="list_watches",
    description="List active Watch Desk watches (optionally for one ticker) — their triggers, "
                "fire counts, and expiry. Check before setting a new watch.",
    parameters={
        "type": "object",
        "properties": {
            "ticker": {"type": "string", "description": "Optional ticker filter."},
        },
    },
    tier=1,
    source="watch_desk",
    permission=PermissionLevel.READ_ONLY,
)
async def list_watches(ticker: str | None = None, **_extra) -> str:
    from app.services.watch_desk import list_watches as _list
    try:
        return json.dumps({"watches": _list(ticker=ticker)}, default=str)
    except Exception as e:
        logger.error("[WatchDeskTools] list_watches failed: %s", e)
        return json.dumps({"status": "error", "message": str(e)})


@registry.register(
    name="clear_watch",
    description="Deactivate a Watch Desk watch by watch_id, or all active watches for a ticker — "
                "use when the thesis changed and the old triggers no longer apply.",
    parameters={
        "type": "object",
        "properties": {
            "watch_id": {"type": "string", "description": "The watch-* id to clear."},
            "ticker": {"type": "string", "description": "Or clear all active watches for this ticker."},
        },
    },
    tier=1,
    source="watch_desk",
    permission=PermissionLevel.WRITE,
)
async def clear_watch(watch_id: str | None = None, ticker: str | None = None, **_extra) -> str:
    from app.services.watch_desk import clear_watch as _clear
    try:
        return json.dumps(_clear(ticker=ticker, watch_id=watch_id))
    except Exception as e:
        logger.error("[WatchDeskTools] clear_watch failed: %s", e)
        return json.dumps({"status": "error", "message": str(e)})
