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
from app.validation.schedule_validator import ScheduleValidator

logger = logging.getLogger(__name__)

# ── Policy knobs ────────────────────────────────────────────────────────────
MAX_ACTIVE_BOT_SCHEDULES = 5     # active sch-bot-* rows at any moment
MAX_DAILY_BOT_CREATIONS = 10     # sch-bot-* rows created per rolling 24h
MAX_TICKERS_PER_REQUEST = 5      # forces prioritisation
MAX_PENDING_RESEARCH_NOW = 2     # queued immediate research cycles
TICKER_COOLDOWN_HOURS = 4        # fresh analysis_results row blocks re-research
DEFAULT_TTL_DAYS = 7             # every bot schedule expires

VALID_WINDOWS = ("next_pre_market", "next_open", "midday", "pre_close")


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
    rows = db.execute(
        "SELECT DISTINCT ticker FROM analysis_results "
        f"WHERE ticker = ANY(%s) AND created_at >= NOW() - INTERVAL '{TICKER_COOLDOWN_HOURS} hours'",
        [tickers],
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
                f"{TICKER_COOLDOWN_HOURS}h. Build on the existing thesis instead of re-running, "
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


def schedule_research(
    tickers: list,
    when: str,
    reason: str,
    review_intent: str = "event_followup",
    urgency: str = "medium",
    reason_codes: list | None = None,
) -> dict:
    """Create a one-shot scheduled research cycle.

    `when` is either a market window (next_pre_market | next_open | midday |
    pre_close) or an ISO-8601 datetime (UTC assumed if naive) for sniping a
    specific event, e.g. 30 minutes after an earnings release.
    """
    tickers = _clean_tickers(tickers)
    reason = (reason or "").strip()
    if not reason or len(reason) < 10:
        return {
            "status": "rejected",
            "reason": "A specific research reason is required (what event/catalyst, what question to answer).",
        }

    # Parse `when` into either a policy window or an exact run_at.
    when = (when or "").strip()
    schedule_type, earliest_window, run_at = None, None, None
    if when in VALID_WINDOWS:
        schedule_type = "policy"
        earliest_window = when
    else:
        try:
            run_dt = datetime.fromisoformat(when.replace("Z", "+00:00"))
            if run_dt.tzinfo is None:
                run_dt = run_dt.replace(tzinfo=timezone.utc)
            run_dt = run_dt.astimezone(timezone.utc)
        except ValueError:
            return {
                "status": "rejected",
                "reason": f"`when` must be one of {VALID_WINDOWS} or an ISO datetime, got: {when!r}",
            }
        now = datetime.now(timezone.utc)
        if run_dt <= now:
            return {"status": "rejected", "reason": "`when` is in the past — use request_research_now instead."}
        if run_dt > now + timedelta(days=DEFAULT_TTL_DAYS):
            return {
                "status": "rejected",
                "reason": f"`when` is more than {DEFAULT_TTL_DAYS} days out. Schedule closer to the event — "
                          "long-range intentions belong in memory, not the scheduler.",
            }
        schedule_type = "once"
        run_at = run_dt

    with get_db() as db:
        system_active = db.execute(
            "SELECT COUNT(*) FROM cycle_schedules WHERE is_active = TRUE"
        ).fetchone()[0]

        # The previously-dormant validator: scope/intent/urgency sanity rules
        # plus the system-wide active cap.
        ok, why = ScheduleValidator.validate_proposal({
            "schedule_scope": "single_ticker" if len(tickers) == 1 else "watchlist_subset",
            "review_intent": review_intent,
            "urgency": urgency,
            "earliest_window": earliest_window or "exact_time",
            "reason_codes": reason_codes or [],
        }, active_count=system_active)
        if not ok:
            return {"status": "rejected", "reason": why}

        active = db.execute(
            "SELECT COUNT(*) FROM cycle_schedules WHERE id LIKE %s AND is_active = TRUE",
            ["sch-bot-%"],
        ).fetchone()[0]
        if active >= MAX_ACTIVE_BOT_SCHEDULES:
            return {
                "status": "rejected",
                "reason": f"{active} bot research schedules already active (max {MAX_ACTIVE_BOT_SCHEDULES}). "
                          "Cancel one first or let them run — be picky.",
            }
        daily = db.execute(
            "SELECT COUNT(*) FROM cycle_schedules "
            "WHERE id LIKE %s AND created_at >= NOW() - INTERVAL '24 hours'",
            ["sch-bot-%"],
        ).fetchone()[0]
        if daily >= MAX_DAILY_BOT_CREATIONS:
            return {
                "status": "rejected",
                "reason": f"Daily budget spent ({daily}/{MAX_DAILY_BOT_CREATIONS} schedules in 24h). "
                          "Only the best research ideas get scheduled — try again tomorrow.",
            }

        rej = _guard_common(db, tickers, urgency)
        if rej:
            return {"status": "rejected", "reason": rej}

        schedule_id = f"sch-bot-{uuid.uuid4().hex[:8]}"
        now = datetime.now(timezone.utc)
        expiry = (now + timedelta(days=DEFAULT_TTL_DAYS)).replace(tzinfo=None)
        name = f"Research: {', '.join(tickers)} ({reason[:60]})"
        db.execute(
            """
            INSERT INTO cycle_schedules
                (id, name, schedule_type, earliest_window, run_at, expiry_at,
                 schedule_scope, review_intent, urgency, reason_codes,
                 collect, "analyze", trade, tickers, market_hours_only, is_active)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, TRUE, TRUE, FALSE, %s, FALSE, TRUE)
            """,
            [
                schedule_id, name, schedule_type, earliest_window,
                run_at.isoformat() if run_at else None,
                expiry.isoformat(),
                "single_ticker" if len(tickers) == 1 else "watchlist_subset",
                review_intent, urgency,
                json.dumps(reason_codes or [reason[:120]]),
                json.dumps(tickers),
            ],
        )
        # Nudge the scheduler engine (lives in the cycle process) to register
        # the new job without waiting for a reboot.
        db.execute(
            "INSERT INTO system_commands (id, command_type, payload) VALUES (%s, %s, %s)",
            [f"cmd-{uuid.uuid4().hex[:8]}", "REFRESH_SCHEDULE", json.dumps({"job_id": schedule_id})],
        )

    logger.info(
        "[GOVERNOR] Research scheduled %s type=%s when=%s tickers=%s",
        schedule_id, schedule_type, when, tickers,
    )
    return {
        "status": "scheduled",
        "schedule_id": schedule_id,
        "type": schedule_type,
        "fires": earliest_window or (run_at.isoformat() if run_at else None),
        "expires": expiry.isoformat() + "Z",
        "tickers": tickers,
        "note": "One-shot: deactivates after it runs. trade=False is enforced.",
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
            "max_active_schedules": MAX_ACTIVE_BOT_SCHEDULES,
            "max_daily_creations": MAX_DAILY_BOT_CREATIONS,
            "max_tickers_per_request": MAX_TICKERS_PER_REQUEST,
            "ticker_cooldown_hours": TICKER_COOLDOWN_HOURS,
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
            "UPDATE cycle_schedules SET is_active = FALSE, last_status = %s, updated_at = NOW() WHERE id = %s",
            [f"cancelled: {reason[:120]}" if reason else "cancelled", schedule_id],
        )
        db.execute(
            "INSERT INTO system_commands (id, command_type, payload) VALUES (%s, %s, %s)",
            [f"cmd-{uuid.uuid4().hex[:8]}", "REFRESH_SCHEDULE", json.dumps({"job_id": schedule_id})],
        )
    logger.info("[GOVERNOR] Schedule %s cancelled (%s)", schedule_id, reason)
    return {"status": "cancelled", "schedule_id": schedule_id}
