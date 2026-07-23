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

from app.db.connection import get_db
from app.services.parameter_store import get_param
from app.validation.schedule_validator import ScheduleValidator

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


def _clean_tickers(tickers: list) -> list[str]:
    seen = []
    for t in tickers or []:
        if not t or not isinstance(t, str):
            continue
        t = t.upper().strip()
        if t and t not in seen:
            seen.append(t)
    return seen


def _recently_researched(db, tickers: list[str]) -> list[str]:
    """Tickers with an analysis_results row inside the cooldown window."""
    if not tickers:
        return []
    cooldown_hours = int(get_param("TICKER_COOLDOWN_HOURS"))
    try:
        from app.db import mongo_store
        if mongo_store.reads_mongo("analysis_results"):
            from datetime import datetime, timedelta
            cutoff = datetime.utcnow() - timedelta(hours=cooldown_hours)
            return [t for t in mongo_store.distinct_values(
                "analysis_results", "ticker",
                {"ticker": {"$in": tickers}, "created_at": {"$gte": cutoff}},
            ) if t]
    except Exception as me:
        logger.warning("[governor] mongo cooldown read failed, PG fallback: %s", me)
    rows = db.execute(
        "SELECT DISTINCT ticker FROM analysis_results "
        "WHERE ticker = ANY(%s) AND created_at >= NOW() - make_interval(hours => %s)",
        [tickers, cooldown_hours],
    ).fetchall()
    return [r[0] for r in rows]


def _tickers_already_queued(db, tickers: list[str]) -> list[str]:
    """Tickers already covered by an active bot schedule or pending research command."""
    covered = set()
    rows = db.execute(
        "SELECT tickers FROM cycle_schedules "
        "WHERE id LIKE %s AND is_active = TRUE",
        ["sch-bot-%"],
    ).fetchall()
    for (tickers_json,) in rows:
        try:
            for t in json.loads(tickers_json or "[]"):
                covered.add(str(t).upper())
        except Exception:
            continue
    rows = db.execute(
        "SELECT payload FROM v3_system_commands "
        "WHERE status = 'pending' AND command_type = 'START_CYCLE' "
        "AND payload::text LIKE %s",
        ["%research_request%"],
    ).fetchall()
    for (payload,) in rows:
        try:
            p = json.loads(payload) if isinstance(payload, str) else (payload or {})
            for t in p.get("tickers", []):
                covered.add(str(t).upper())
        except Exception:
            continue
    return [t for t in tickers if t in covered]


def _guard_common(db, tickers: list[str], urgency: str) -> str | None:
    """Shared pickiness gates. Returns a rejection reason or None."""
    if not tickers:
        return "No valid tickers given — research requests must name specific tickers."
    if len(tickers) > MAX_TICKERS_PER_REQUEST:
        return (
            f"Too many tickers ({len(tickers)} > {MAX_TICKERS_PER_REQUEST}). "
            "Be picky: pick only the highest-conviction candidates."
        )

    dup = _tickers_already_queued(db, tickers)
    if dup:
        return (
            f"Research already queued for: {', '.join(dup)}. "
            "One outstanding request per ticker — check list_scheduled_research first."
        )

    if urgency != "critical":
        recent = _recently_researched(db, tickers)
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
    with get_db() as db:
        pending = db.execute(
            "SELECT COUNT(*) FROM v3_system_commands "
            "WHERE status = 'pending' AND command_type = 'START_CYCLE' "
            "AND payload::text LIKE %s",
            ["%research_request%"],
        ).fetchone()[0]
        if pending >= MAX_PENDING_RESEARCH_NOW:
            return {
                "status": "rejected",
                "reason": f"{pending} research cycles already queued (max {MAX_PENDING_RESEARCH_NOW}). "
                          "Wait for them to finish.",
            }

        rej = _guard_common(db, tickers, urgency)
        if rej:
            return {"status": "rejected", "reason": rej}

        cmd_id = f"sch-rsrch-{uuid.uuid4().hex[:8]}"
        payload = {
            "tickers": tickers,
            "collect": True,
            "analyze": True,
            "trade": False,
            "dynamic_selection_mode": False,
            "research_request": True,
            "research_reason": (reason or "").strip()[:500],
        }
        db.execute(
            "INSERT INTO v3_system_commands (id, command_type, payload) VALUES (%s, %s, %s)",
            [cmd_id, "START_CYCLE", json.dumps(payload)],
        )
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

    with get_db() as db:
        system_active = db.execute(
            "SELECT COUNT(*) FROM cycle_schedules WHERE is_active = TRUE"
        ).fetchone()[0]

        ok, why = ScheduleValidator.validate_proposal({
            "schedule_scope": "single_ticker" if len(tickers) == 1 else "watchlist_subset",
            "review_intent": review_intent,
            "urgency": urgency,
            "earliest_window": "exact_time",
            "reason_codes": reason_codes or [],
        }, active_count=system_active)
        if not ok:
            return {"status": "rejected", "reason": why}

        active = db.execute(
            "SELECT COUNT(*) FROM cycle_schedules WHERE id LIKE %s AND is_active = TRUE",
            ["sch-bot-%"],
        ).fetchone()[0]
        max_active = int(get_param("MAX_ACTIVE_BOT_SCHEDULES"))
        if active >= max_active:
            return {
                "status": "rejected",
                "reason": f"{active} bot research schedules already active (max {max_active}). "
                          "Cancel one first or let them run — be picky.",
            }
        daily = db.execute(
            "SELECT COUNT(*) FROM cycle_schedules "
            "WHERE id LIKE %s AND created_at >= NOW() - INTERVAL '24 hours'",
            ["sch-bot-%"],
        ).fetchone()[0]
        max_daily = int(get_param("MAX_DAILY_BOT_CREATIONS"))
        if daily >= max_daily:
            return {
                "status": "rejected",
                "reason": f"Daily budget spent ({daily}/{max_daily} schedules in 24h). "
                          "Only the best research ideas get scheduled — try again tomorrow.",
            }

        rej = _guard_common(db, tickers, urgency)
        if rej:
            return {"status": "rejected", "reason": rej}

        schedule_id = f"sch-bot-{uuid.uuid4().hex[:8]}"
        # Expiry must sit AFTER run_at so the TTL guard doesn't kill the schedule
        # before it fires (earnings can be weeks out).
        expiry = (run_at + timedelta(days=2)).replace(tzinfo=None)
        name = f"Research: {', '.join(tickers)} ({reason[:60]})"
        db.execute(
            """
            INSERT INTO cycle_schedules
                (id, name, schedule_type, earliest_window, run_at, expiry_at,
                 schedule_scope, review_intent, urgency, reason_codes,
                 collect, "analyze", trade, tickers, market_hours_only, is_active)
            VALUES (%s, %s, 'once', NULL, %s, %s, %s, %s, %s, %s, TRUE, TRUE, FALSE, %s, FALSE, TRUE)
            """,
            [
                schedule_id, name,
                run_at.isoformat(), expiry.isoformat(),
                "single_ticker" if len(tickers) == 1 else "watchlist_subset",
                review_intent, urgency,
                json.dumps(reason_codes or [reason[:120]]),
                json.dumps(tickers),
            ],
        )
        db.execute(
            "INSERT INTO system_commands (id, command_type, payload) VALUES (%s, %s, %s)",
            [f"cmd-{uuid.uuid4().hex[:8]}", "REFRESH_SCHEDULE", json.dumps({"job_id": schedule_id})],
        )

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
    with get_db() as db:
        sched_rows = db.execute(
            "SELECT id, name, schedule_type, earliest_window, run_at, tickers, urgency, "
            "next_run_at, run_count, last_status, expiry_at "
            "FROM cycle_schedules WHERE id LIKE %s AND is_active = TRUE "
            "ORDER BY created_at DESC",
            ["sch-bot-%"],
        ).fetchall()
        pending_rows = db.execute(
            "SELECT id, payload, created_at FROM v3_system_commands "
            "WHERE status = 'pending' AND command_type = 'START_CYCLE' "
            "AND payload::text LIKE %s ORDER BY created_at",
            ["%research_request%"],
        ).fetchall()
        recent_rows = None
        try:
            from app.db import mongo_store
            if mongo_store.reads_mongo("analysis_results"):
                from datetime import datetime, timedelta
                cutoff = datetime.utcnow() - timedelta(hours=48)
                recent_rows = [
                    (d["_id"], d.get("last_date"))
                    for d in mongo_store.aggregate("analysis_results", [
                        {"$match": {"created_at": {"$gte": cutoff}}},
                        {"$group": {"_id": "$ticker", "last_date": {"$max": "$created_at"}}},
                        {"$sort": {"last_date": -1}},
                        {"$limit": 25},
                    ])
                ]
        except Exception as me:
            logger.warning("[governor] mongo recent read failed, PG fallback: %s", me)
            recent_rows = None
        if recent_rows is None:
            recent_rows = db.execute(
                "SELECT ticker, MAX(created_at) FROM analysis_results "
                "WHERE created_at >= NOW() - INTERVAL '48 hours' GROUP BY ticker "
                "ORDER BY MAX(created_at) DESC LIMIT 25"
            ).fetchall()

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
    with get_db() as db:
        row = db.execute(
            "SELECT is_active FROM cycle_schedules WHERE id = %s", [schedule_id]
        ).fetchone()
        if not row:
            return {"status": "rejected", "reason": f"Schedule {schedule_id} not found."}
        db.execute(
            "UPDATE cycle_schedules SET is_active = FALSE, next_run_at = NULL, last_status = %s, updated_at = NOW() WHERE id = %s",
            [f"cancelled: {reason[:120]}" if reason else "cancelled", schedule_id],
        )
        db.execute(
            "INSERT INTO system_commands (id, command_type, payload) VALUES (%s, %s, %s)",
            [f"cmd-{uuid.uuid4().hex[:8]}", "REFRESH_SCHEDULE", json.dumps({"job_id": schedule_id})],
        )
    logger.info("[GOVERNOR] Schedule %s cancelled (%s)", schedule_id, reason)
    return {"status": "cancelled", "schedule_id": schedule_id}
