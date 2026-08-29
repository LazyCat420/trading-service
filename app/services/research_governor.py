"""
Research Governor — guardrails for agent-initiated (self-scheduled) research.

The agents may request extra research cycles (collect+analyze, never trade)
either immediately or scheduled around known events (earnings drops, market
windows). This module is the ONLY writer of bot-created cycle_schedules rows
and enforces the anti-doom-loop policy:

  * hard caps    — max active bot schedules, max creations per day
  * dedupe       — one outstanding request per ticker, ever
  * cooldown     — a ticker researched recently cannot be re-queued
  * TTL          — every bot schedule expires; one-shots deactivate after firing
  * picky-by-design — small ticker budgets force the agent to prioritise

Schedule ids use the `sch-bot-` prefix so bot-created rows are auditable and
capped independently of human-created ones.
"""

import json
import logging
import uuid
from datetime import datetime, timedelta, timezone

from app.services.parameter_store import get_param
from app.validation.schedule_validator import ScheduleValidator
from app.db import mongo_query
from app.db import mongo_store
from app.services.cycle_queue import enqueue_start_cycle, enqueue_refresh_schedule

logger = logging.getLogger(__name__)

# ── Policy knobs ────────────────────────────────────────────────────────────
MAX_ACTIVE_BOT_SCHEDULES = 5     # active sch-bot-* rows at any moment
MAX_DAILY_BOT_CREATIONS = 10     # sch-bot-* rows created per rolling 24h
MAX_TICKERS_PER_REQUEST = 5      # forces prioritisation
MAX_PENDING_RESEARCH_NOW = 2     # queued immediate research cycles
TICKER_COOLDOWN_HOURS = 4        # fresh analysis_results row blocks re-research
DEFAULT_TTL_DAYS = 7             # every bot schedule expires

# Coarse market-window schedules are RETIRED — the Watch Desk (watch_ticker) owns ongoing,
# condition-driven monitoring now. Any of these as `when` is rejected and the
# agent is redirected to watch_ticker.
RETIRED_WINDOWS = (
    "next_pre_market", "next_open", "midday", "pre_close",
    "post_close", "next_trading_day", "next_week",
)
# `once` earnings-snipes can legitimately be weeks out; allow a longer horizon
# than the default 7-day research TTL.
ONCE_MAX_DAYS = 45


# Mongo equivalents of the SQL predicates this module used.
# `id LIKE 'sch-bot-%'` is a PREFIX match, so anchor the regex — an unanchored
# one would also match a human schedule that merely contains the string.
_BOT_SCHEDULE = {"id": {"$regex": "^sch-bot-"}}
# `payload::text LIKE '%research_request%'` is an unanchored substring match on
# the serialised payload; payloads are stored as JSON strings.
_PENDING_RESEARCH = {
    "status": "pending",
    "command_type": "START_CYCLE",
    "payload": {"$regex": "research_request"},
}


def _clean_tickers(tickers: list) -> list[str]:
    seen = []
    for t in tickers or []:
        if not t or not isinstance(t, str):
            continue
        t = t.upper().strip()
        if t and t not in seen:
            seen.append(t)
    return seen


def _recently_researched(tickers: list[str]) -> list[str]:
    """Tickers with an analysis_results row inside the cooldown window."""
    if not tickers:
        return []
    cooldown_hours = int(get_param("TICKER_COOLDOWN_HOURS"))
    cutoff = datetime.now(timezone.utc) - timedelta(hours=cooldown_hours)
    # SELECT DISTINCT ticker → distinct_values; the SQL could not return NULL
    # tickers as a match either, so drop falsy values.
    return [t for t in mongo_store.distinct_values(
        "analysis_results", "ticker",
        {"ticker": {"$in": tickers}, "created_at": {"$gte": cutoff}},
    ) if t]


def _tickers_already_queued(tickers: list[str]) -> list[str]:
    """Tickers already covered by an active bot schedule or pending research command."""
    covered = set()
    rows = mongo_query.find_rows(
        "cycle_schedules", _BOT_SCHEDULE | {"is_active": True}, ["tickers"])
    for (tickers_json,) in rows:
        try:
            # Written as a JSON string; tolerate a native list if a doc ever
            # carries one, rather than silently covering nothing.
            vals = (json.loads(tickers_json or "[]")
                    if isinstance(tickers_json, str) else (tickers_json or []))
            for t in vals:
                covered.add(str(t).upper())
        except Exception:
            continue
    rows = mongo_query.find_rows("v3_system_commands", dict(_PENDING_RESEARCH), ["payload"])
    for (payload,) in rows:
        try:
            p = json.loads(payload) if isinstance(payload, str) else (payload or {})
            for t in p.get("tickers", []):
                covered.add(str(t).upper())
        except Exception:
            continue
    return [t for t in tickers if t in covered]


def _guard_common(tickers: list[str], urgency: str) -> str | None:
    """Shared pickiness gates. Returns a rejection reason or None."""
    if not tickers:
        return "No valid tickers given — research requests must name specific tickers."
    if len(tickers) > MAX_TICKERS_PER_REQUEST:
        return (
            f"Too many tickers ({len(tickers)} > {MAX_TICKERS_PER_REQUEST}). "
            "Be picky: pick only the highest-conviction candidates."
        )

    dup = _tickers_already_queued(tickers)
    if dup:
        return (
            f"Research already queued for: {', '.join(dup)}. "
            "One outstanding request per ticker — check list_scheduled_research first."
        )

    if urgency != "critical":
        recent = _recently_researched(tickers)
        if recent:
            return (
                f"Cooldown: {', '.join(recent)} researched within the last "
                f"{int(get_param('TICKER_COOLDOWN_HOURS'))}h. Build on the existing thesis instead of re-running, "
                "or escalate with urgency='critical' if a genuine catalyst hit."
            )
    return None


def request_research_now(tickers: list, reason: str, urgency: str = "medium") -> dict:
    """Queue an immediate research-only cycle (collect+analyze, trade=False)."""
    tickers = _clean_tickers(tickers)
    pending = mongo_query.count("v3_system_commands", dict(_PENDING_RESEARCH))
    if pending >= MAX_PENDING_RESEARCH_NOW:
        return {
            "status": "rejected",
            "reason": f"{pending} research cycles already queued (max {MAX_PENDING_RESEARCH_NOW}). "
                      "Wait for them to finish.",
        }

    rej = _guard_common(tickers, urgency)
    if rej:
        return {"status": "rejected", "reason": rej}

    payload = {
        "tickers": tickers,
        "collect": True,
        "analyze": True,
        "trade": False,
        "dynamic_selection_mode": False,
        "research_request": True,
        "research_reason": (reason or "").strip()[:500],
    }
    cmd_id = enqueue_start_cycle(payload, prefix="sch-rsrch")
    logger.info("[GOVERNOR] Immediate research queued %s tickers=%s reason=%s", cmd_id, tickers, reason)
    return {
        "status": "queued",
        "command_id": cmd_id,
        "tickers": tickers,
        "note": "Research cycle will run after the current cycle finishes. trade=False is enforced.",
    }


async def _resolve_earnings_run_at(ticker: str):
    """Resolve a ticker's next earnings into a precise UTC snipe time, or None."""
    try:
        from app.collectors.finnhub_collector import collect_earnings_calendar
        from app.services.event_timing import next_earnings_run_at
        events = await collect_earnings_calendar(ticker)
        run_at, _event = next_earnings_run_at(events)
        return run_at
    except Exception as e:
        logger.warning("[GOVERNOR] earnings resolve failed for %s: %s", ticker, e)
        return None


async def schedule_research(
    tickers: list,
    when: str | None = None,
    reason: str = "",
    review_intent: str = "event_followup",
    urgency: str = "medium",
    reason_codes: list | None = None,
) -> dict:
    """Create a one-shot (`once`) scheduled research cycle sniped to a real event.

    Coarse market windows and recurring "monitor" schedules are RETIRED — those are
    now handled by `watch_ticker` (the Watch Desk), which monitors a ticker by condition in
    cheap background code and only wakes the agent on a trip.

    `when`:
      * omitted → the governor auto-resolves the ticker's next earnings datetime
        (single ticker only) and snipes analysis to land right after the report.
      * an ISO-8601 UTC datetime → snipe at that exact instant (e.g. a Fed decision).
    """
    tickers = _clean_tickers(tickers)
    reason = (reason or "").strip()
    if not reason or len(reason) < 10:
        return {
            "status": "rejected",
            "reason": "A specific research reason is required (what event/catalyst, what question to answer).",
        }

    when = (when or "").strip()
    now = datetime.now(timezone.utc)

    # Retired paths → redirect to the Watch Desk.
    if when.lower() in RETIRED_WINDOWS:
        return {
            "status": "rejected",
            "reason": f"Coarse market-window schedules ({when!r}) are retired. To keep watching a ticker, "
                      "use watch_ticker (price/pct/rsi/volume/news/staleness conditions) — it monitors in "
                      "cheap background code and wakes a cycle only on a trip. For a known dated event, "
                      "pass an exact ISO datetime or omit `when` to auto-snipe the next earnings.",
        }
    if (review_intent or "").lower() == "monitor":
        return {
            "status": "rejected",
            "reason": "'monitor' intent is now handled by watch_ticker (the Watch Desk), not a scheduled cycle. "
                      "Leave a watch condition instead.",
        }

    # Resolve the exact run time: explicit ISO wins; else auto-resolve earnings.
    if when:
        try:
            run_dt = datetime.fromisoformat(when.replace("Z", "+00:00"))
            if run_dt.tzinfo is None:
                run_dt = run_dt.replace(tzinfo=timezone.utc)
            run_at = run_dt.astimezone(timezone.utc)
        except ValueError:
            return {
                "status": "rejected",
                "reason": f"`when` must be an ISO-8601 datetime (or omitted to auto-snipe earnings), got: {when!r}",
            }
    else:
        if len(tickers) != 1:
            return {
                "status": "rejected",
                "reason": "Auto earnings-resolution needs a single ticker. For a basket, schedule each "
                          "separately, or pass an explicit ISO `when`.",
            }
        run_at = await _resolve_earnings_run_at(tickers[0])
        if run_at is None:
            return {
                "status": "rejected",
                "reason": f"No upcoming earnings found for {tickers[0]} and no explicit `when` given — can't "
                          "snipe an unknown event. Use watch_ticker to monitor by condition, or "
                          "request_research_now if the catalyst already hit.",
            }

    if run_at <= now:
        return {"status": "rejected", "reason": "The resolved time is in the past — use request_research_now instead."}
    if run_at > now + timedelta(days=ONCE_MAX_DAYS):
        return {
            "status": "rejected",
            "reason": f"The event is more than {ONCE_MAX_DAYS} days out — too far to pin a cycle. Use watch_ticker "
                      "so a condition (or the earnings date closer in) wakes it instead.",
        }

    # Counts SCHEDULE rows only. If anything ever starts mirroring the
    # ~26 APScheduler system jobs (market_open_cycle, stop-loss monitor,
    # collectors, ...) into this table, an unfiltered count would sail past
    # ScheduleValidator.MAX_SYSTEM_SCHEDULES (10) permanently and reject
    # every non-critical agent schedule with "System has reached max active
    # schedules" — a silent throttle of the whole research budget with no
    # visible cause. The exclusion is cheap insurance; system jobs are
    # surfaced read-only via /api/diagnostics/system-jobs instead.
    # COALESCE(job_type,'user') <> 'system'. Measured against the server:
    # {$ne: "system"} DOES match documents where job_type is null or missing —
    # $ne against a VALUE includes absent fields, which is what makes it the
    # COALESCE. (Note the asymmetry with {$ne: None}, which does NOT match a
    # missing field. The two read alike and behave differently; that is why
    # tests/unit/test_sql_null_semantics_in_mongo.py pins both.)
    system_active = mongo_query.count(
        "cycle_schedules", {"is_active": True, "job_type": {"$ne": "system"}})

    ok, why = ScheduleValidator.validate_proposal({
        "schedule_scope": "single_ticker" if len(tickers) == 1 else "watchlist_subset",
        "review_intent": review_intent,
        "urgency": urgency,
        "earliest_window": "exact_time",
        "reason_codes": reason_codes or [],
    }, active_count=system_active)
    if not ok:
        return {"status": "rejected", "reason": why}

    active = mongo_query.count("cycle_schedules", _BOT_SCHEDULE | {"is_active": True})
    max_active = int(get_param("MAX_ACTIVE_BOT_SCHEDULES"))
    if active >= max_active:
        return {
            "status": "rejected",
            "reason": f"{active} bot research schedules already active (max {max_active}). "
                      "Cancel one first or let them run — be picky.",
        }
    daily = mongo_query.count("cycle_schedules", _BOT_SCHEDULE | {
        "created_at": {"$gte": datetime.now(timezone.utc) - timedelta(hours=24)}})
    max_daily = int(get_param("MAX_DAILY_BOT_CREATIONS"))
    if daily >= max_daily:
        return {
            "status": "rejected",
            "reason": f"Daily budget spent ({daily}/{max_daily} schedules in 24h). "
                      "Only the best research ideas get scheduled — try again tomorrow.",
        }

    rej = _guard_common(tickers, urgency)
    if rej:
        return {"status": "rejected", "reason": rej}

    schedule_id = f"sch-bot-{uuid.uuid4().hex[:8]}"
    # Expiry must sit AFTER run_at so the TTL guard doesn't kill the schedule
    # before it fires (earnings can be weeks out).
    expiry = (run_at + timedelta(days=2)).replace(tzinfo=None)
    name = f"Research: {', '.join(tickers)} ({reason[:60]})"
    mongo_store.insert_docs('cycle_schedules', [{'id': schedule_id, 'name': name, 'schedule_type': 'once', 'earliest_window': None, 'run_at': run_at.isoformat(), 'expiry_at': expiry.isoformat(), 'schedule_scope': "single_ticker" if len(tickers) == 1 else "watchlist_subset", 'review_intent': review_intent, 'urgency': urgency, 'reason_codes': json.dumps(reason_codes or [reason[:120]]), 'collect': True, 'analyze': True, 'trade': False, 'tickers': json.dumps(tickers), 'market_hours_only': False, 'is_active': True, 'job_type': 'user', 'run_count': 0, 'discovered_tickers': 0, 'created_at': datetime.now(timezone.utc), 'updated_at': datetime.now(timezone.utc)}])
    enqueue_refresh_schedule(schedule_id, prefix="cmd")

    logger.info(
        "[GOVERNOR] Research scheduled %s type=once run_at=%s tickers=%s",
        schedule_id, run_at.isoformat(), tickers,
    )
    return {
        "status": "scheduled",
        "schedule_id": schedule_id,
        "type": "once",
        "fires": run_at.isoformat(),
        "expires": expiry.isoformat() + "Z",
        "tickers": tickers,
        "note": "One-shot sniped to the event: deactivates after it runs. trade=False is enforced.",
    }


def list_scheduled_research() -> dict:
    """Active bot schedules + queued research commands + recent research history."""
    sched_rows = mongo_query.find_rows(
        "cycle_schedules", _BOT_SCHEDULE | {"is_active": True},
        ["id", "name", "schedule_type", "earliest_window", "run_at", "tickers",
         "urgency", "next_run_at", "run_count", "last_status", "expiry_at"],
        sort=[("created_at", -1)],
    )
    pending_rows = mongo_query.find_rows(
        "v3_system_commands", dict(_PENDING_RESEARCH),
        ["id", "payload", "created_at"], sort=[("created_at", 1)],
    )
    # SELECT ticker, MAX(created_at) ... GROUP BY ticker ORDER BY MAX DESC LIMIT 25
    cutoff = datetime.now(timezone.utc) - timedelta(hours=48)
    recent_rows = mongo_query.group_rows(
        "analysis_results", {"created_at": {"$gte": cutoff}},
        keys=["ticker"], aggs=[("max", "created_at")],
        select=[("key", "ticker"), ("agg", 0)],
        sort=[("a0", -1)], limit=25,
    )

    schedules = []
    for r in sched_rows:
        schedules.append({
            "schedule_id": r[0], "name": r[1], "type": r[2],
            "window": r[3], "run_at": str(r[4]) if r[4] else None,
            "tickers": json.loads(r[5] or "[]"), "urgency": r[6],
            "next_run_at": str(r[7]) if r[7] else None,
            "run_count": r[8], "last_status": r[9],
            "expires_at": str(r[10]) if r[10] else None,
        })
    pending = []
    for r in pending_rows:
        try:
            p = json.loads(r[1]) if isinstance(r[1], str) else (r[1] or {})
        except Exception:
            p = {}
        pending.append({
            "command_id": r[0],
            "tickers": p.get("tickers", []),
            "reason": p.get("research_reason", ""),
            "queued_at": str(r[2]),
        })
    return {
        "active_schedules": schedules,
        "queued_research_now": pending,
        "recently_researched_48h": [{"ticker": t, "at": str(ts)} for t, ts in recent_rows],
        "limits": {
            "max_active_schedules": int(get_param("MAX_ACTIVE_BOT_SCHEDULES")),
            "max_daily_creations": int(get_param("MAX_DAILY_BOT_CREATIONS")),
            "max_tickers_per_request": MAX_TICKERS_PER_REQUEST,
            "ticker_cooldown_hours": int(get_param("TICKER_COOLDOWN_HOURS")),
        },
    }


def cancel_scheduled_research(schedule_id: str, reason: str = "") -> dict:
    """Deactivate a bot-created research schedule."""
    schedule_id = (schedule_id or "").strip()
    if not schedule_id.startswith("sch-bot-"):
        return {"status": "rejected", "reason": "Only bot-created (sch-bot-*) schedules can be cancelled here."}
    row = mongo_query.find_row('cycle_schedules', {'id': schedule_id}, ['is_active'])
    if not row:
        return {"status": "rejected", "reason": f"Schedule {schedule_id} not found."}
    mongo_store.update_docs('cycle_schedules', {'id': schedule_id}, {'$set': {'is_active': False, 'next_run_at': None, 'last_status': f"cancelled: {reason[:120]}" if reason else "cancelled", 'updated_at': datetime.now(timezone.utc)}})
    enqueue_refresh_schedule(schedule_id, prefix="cmd")
    logger.info("[GOVERNOR] Schedule %s cancelled (%s)", schedule_id, reason)
    return {"status": "cancelled", "schedule_id": schedule_id}
