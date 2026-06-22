"""
Schedule Management Tools — Agentic self-scheduling for the trading bot.

Allows the bot to create, update, and inspect its own cycle schedules.
This closes the autonomy gap where cycles complete but the bot has no
mechanism to plan its own next execution.

Safety limits:
  - Max 6 active schedules (prevents runaway schedule creation)
  - Minimum interval: 1 hour (prevents self-DoS)
  - Maximum interval: 168 hours / 1 week
  - Cannot delete the last active schedule via tool (must use UI)
"""

import json
import logging
import uuid
from datetime import datetime, timezone

from app.db.connection import get_db
from app.tools.registry import registry, PermissionLevel

logger = logging.getLogger(__name__)

# ── Hard Safety Limits ──
MAX_ACTIVE_SCHEDULES = 6
MIN_INTERVAL_HOURS = 1.0
MAX_INTERVAL_HOURS = 168.0  # 1 week


def _count_active_schedules() -> int:
    """Count currently active schedules in the database."""
    with get_db() as db:
        row = db.execute(
            "SELECT COUNT(*) FROM cycle_schedules WHERE is_active = TRUE"
        ).fetchone()
        return row[0] if row else 0


# ═══════════════════════════════════════════════════════════════════
# TOOL: create_or_update_schedule
# ═══════════════════════════════════════════════════════════════════


@registry.register(
    name="create_or_update_schedule",
    description=(
        "Create a new automated trading cycle schedule or update an existing one. "
        "Use this after completing a trading cycle to ensure the bot runs again. "
        "The scheduler is policy-driven. Instead of arbitrary timestamps, specify "
        "your intent, scope, and urgency."
    ),
    parameters={
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": "Name for the schedule (e.g. 'Post-Trade Follow-Up')",
            },
            "schedule_scope": {
                "type": "string",
                "description": "portfolio | positions | watchlist_subset | sector | single_ticker",
                "enum": ["portfolio", "positions", "watchlist_subset", "sector", "single_ticker"]
            },
            "review_intent": {
                "type": "string",
                "description": "monitor | reassess | trade_window | event_followup | weekly_review | monthly_review",
                "enum": ["monitor", "reassess", "trade_window", "event_followup", "weekly_review", "monthly_review"]
            },
            "urgency": {
                "type": "string",
                "description": "low | medium | high | critical",
                "enum": ["low", "medium", "high", "critical"]
            },
            "earliest_window": {
                "type": "string",
                "description": "next_pre_market | next_open | midday | pre_close | post_close | next_trading_day | next_week",
                "enum": ["next_pre_market", "next_open", "midday", "pre_close", "post_close", "next_trading_day", "next_week"]
            },
            "reason_codes": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Array of triggers e.g., ['news', 'earnings', 'thesis_drift', 'portfolio_risk']"
            },
            "confidence": {
                "type": "number",
                "description": "Confidence score 0-100"
            },
            "anti_overtrading_justification": {
                "type": "string",
                "description": "Explanation of why this run is justified and not just spam."
            },
            "interval_hours": {
                "type": "number",
                "description": "Fallback explicit interval in hours for recurring monitor tasks.",
            },
            "update_schedule_id": {
                "type": "string",
                "description": "If provided, update this existing schedule instead of creating a new one.",
            },
            "tickers": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Explicit tickers this schedule should include.",
            },
            "cron_expression": {
                "type": "string",
                "description": "Cron expression for the schedule (optional).",
            },
            "max_tickers": {
                "type": "integer",
                "description": "Max number of discovery tickers to add per run.",
            },
            "discovered_tickers": {
                "type": "integer",
                "description": "Number of discovered tickers.",
            }
        },
        "required": ["name", "schedule_scope", "review_intent", "urgency", "earliest_window", "anti_overtrading_justification"],
    },
    permission=PermissionLevel.WRITE,
    tier=2,
    source="scheduler",
    tags=["schedule", "autonomy", "self-management"],
)
async def create_or_update_schedule(
    name: str,
    schedule_scope: str,
    review_intent: str,
    urgency: str,
    earliest_window: str,
    anti_overtrading_justification: str,
    reason_codes: list[str] | None = None,
    confidence: int = 100,
    interval_hours: float | None = None,
    update_schedule_id: str | None = None,
    tickers: list[str] | None = None,
    cron_expression: str | None = None,
    max_tickers: int | None = None,
    discovered_tickers: int | None = None,
) -> str:
    """Create or update a policy-driven cycle schedule."""
    try:
        reason_codes = reason_codes or []
        
        # 1. Run Validator
        proposal = {
            "schedule_scope": schedule_scope,
            "review_intent": review_intent,
            "urgency": urgency,
            "earliest_window": earliest_window,
            "reason_codes": reason_codes
        }
        from app.validation.schedule_validator import ScheduleValidator
        is_valid, reject_reason = ScheduleValidator.validate_proposal(proposal)
        if not is_valid:
            return json.dumps({
                "error": "Schedule rejected by Validator",
                "reason": reject_reason,
                "suggestion": "Adjust scope, intent, or provide a catalyst in reason_codes."
            })
            
        schedule_type = "cron" if cron_expression else ("interval" if interval_hours else "policy")

        with get_db() as db:
            if update_schedule_id:
                # ── UPDATE existing schedule ──
                # Fetch existing to avoid overriding with None if parameters were not supplied
                row = db.execute(
                    "SELECT tickers, max_tickers, discovered_tickers FROM cycle_schedules WHERE id = %s",
                    [update_schedule_id]
                ).fetchone()
                if not row:
                    return json.dumps({"error": f"Schedule {update_schedule_id} not found."})

                db_tickers, db_max_tickers, db_discovered_tickers = row

                final_tickers_str = json.dumps(tickers) if tickers is not None else db_tickers
                final_max_tickers = max_tickers if max_tickers is not None else db_max_tickers
                final_discovered_tickers = discovered_tickers if discovered_tickers is not None else db_discovered_tickers

                now = datetime.now(timezone.utc).isoformat()
                db.execute(
                    """
                    UPDATE cycle_schedules SET
                        name = %s, schedule_type = %s, cron_expression = %s, interval_hours = %s,
                        schedule_scope = %s, review_intent = %s, urgency = %s,
                        earliest_window = %s, reason_codes = %s, confidence = %s,
                        anti_overtrading_justification = %s, tickers = %s,
                        max_tickers = %s, discovered_tickers = %s, updated_at = %s
                    WHERE id = %s
                    """,
                    [
                        name,
                        schedule_type,
                        cron_expression,
                        interval_hours,
                        schedule_scope,
                        review_intent,
                        urgency,
                        earliest_window,
                        json.dumps(reason_codes),
                        confidence,
                        anti_overtrading_justification,
                        final_tickers_str,
                        final_max_tickers,
                        final_discovered_tickers,
                        now,
                        update_schedule_id,
                    ],
                )

                try:
                    from app.services.cycle_scheduler import SchedulerService

                    SchedulerService.refresh_job(update_schedule_id)
                except Exception as e:
                    logger.warning("[SCHEDULE-TOOL] Refresh failed: %s", e)

                logger.info(
                    "[SCHEDULE-TOOL] Updated schedule %s: %s (%s)",
                    update_schedule_id,
                    name,
                    schedule_type,
                )
                return json.dumps(
                    {
                        "status": "updated",
                        "id": update_schedule_id,
                        "name": name,
                        "schedule_type": schedule_type,
                        "interval_hours": interval_hours
                        if not cron_expression
                        else None,
                        "cron_expression": cron_expression,
                    }
                )
            else:
                # ── CREATE new schedule ──
                active = _count_active_schedules()
                if active >= MAX_ACTIVE_SCHEDULES:
                    return json.dumps(
                        {
                            "error": f"Cannot create schedule: {active} active schedules already exist (max {MAX_ACTIVE_SCHEDULES}).",
                            "active_count": active,
                            "suggestion": "Update an existing schedule instead using update_schedule_id.",
                        }
                    )

                job_id = f"sch-bot-{uuid.uuid4().hex[:8]}"
                now = datetime.now(timezone.utc).isoformat()

                final_tickers_str = json.dumps(tickers) if tickers is not None else "[]"
                final_max_tickers = max_tickers
                final_discovered_tickers = discovered_tickers

                db.execute(
                    """
                    INSERT INTO cycle_schedules (
                        id, name, schedule_type, cron_expression, interval_hours,
                        schedule_scope, review_intent, urgency, earliest_window,
                        reason_codes, confidence, anti_overtrading_justification,
                        tickers, max_tickers, discovered_tickers, is_active, created_at, updated_at
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    [
                        job_id,
                        name,
                        schedule_type,
                        cron_expression,
                        interval_hours,
                        schedule_scope,
                        review_intent,
                        urgency,
                        earliest_window,
                        json.dumps(reason_codes),
                        confidence,
                        anti_overtrading_justification,
                        final_tickers_str,
                        final_max_tickers,
                        final_discovered_tickers,
                        True,
                        now,
                        now,
                    ],
                )

                try:
                    from app.services.cycle_scheduler import SchedulerService

                    SchedulerService.refresh_job(job_id)
                except Exception as e:
                    logger.warning("[SCHEDULE-TOOL] Refresh failed: %s", e)

                logger.info(
                    "[SCHEDULE-TOOL] Created schedule %s: %s (%s, %.1fh)",
                    job_id,
                    name,
                    schedule_type,
                    interval_hours,
                )
                return json.dumps(
                    {
                        "status": "created",
                        "id": job_id,
                        "name": name,
                        "schedule_type": schedule_type,
                        "interval_hours": interval_hours
                        if not cron_expression
                        else None,
                        "cron_expression": cron_expression,
                        "active_count": active + 1,
                    }
                )


    except Exception as e:
        logger.error("[SCHEDULE-TOOL] create_or_update failed: %s", e)
        return json.dumps({"error": str(e)})


# ═══════════════════════════════════════════════════════════════════
# TOOL: list_active_schedules
# ═══════════════════════════════════════════════════════════════════


@registry.register(
    name="list_active_schedules",
    description=(
        "List all active cycle schedules. Shows schedule name, type, interval, "
        "last run time, and next run time. Use this to inspect the bot's "
        "current scheduling state before creating or modifying schedules."
    ),
    parameters={
        "type": "object",
        "properties": {},
    },
    permission=PermissionLevel.READ_ONLY,
    tier=0,
    source="scheduler",
    tags=["schedule", "autonomy", "status"],
)
async def list_active_schedules() -> str:
    """List all active schedules with their status."""
    try:
        with get_db() as db:
            rows = db.execute(
                """
                SELECT id, name, schedule_type, cron_expression, interval_hours,
                       is_active, last_run_at, next_run_at, run_count, last_status
                FROM cycle_schedules
                ORDER BY is_active DESC, created_at DESC
                """
            ).fetchall()

        schedules = []
        for r in rows:
            schedules.append(
                {
                    "id": r[0],
                    "name": r[1],
                    "schedule_type": r[2],
                    "cron_expression": r[3],
                    "interval_hours": r[4],
                    "is_active": bool(r[5]),
                    "last_run_at": r[6].isoformat() if r[6] else None,
                    "next_run_at": r[7].isoformat() if r[7] else None,
                    "run_count": r[8] or 0,
                    "last_status": r[9],
                }
            )

        return json.dumps(
            {
                "total": len(schedules),
                "active": sum(1 for s in schedules if s["is_active"]),
                "schedules": schedules,
            }
        )

    except Exception as e:
        logger.error("[SCHEDULE-TOOL] list failed: %s", e)
        return json.dumps({"error": str(e), "schedules": []})
