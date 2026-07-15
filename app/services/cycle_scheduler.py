"""
Scheduler Service — Manages APScheduler for automated cycle runs.
"""

import uuid
import json
import logging
import pytz
from datetime import datetime, timedelta, timezone
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from fastapi import HTTPException

from app.services.cycle_control import cycle_control
from app.db.connection import get_db
from app.services.bot_manager import get_active_bot_id
from app.trading.paper_trader import check_stop_losses, check_take_profits

logger = logging.getLogger(__name__)

# Use local timezone
from tzlocal import get_localzone

# Suppress APScheduler execution INFO logs to prevent terminal spam
logging.getLogger("apscheduler.executors.default").setLevel(logging.WARNING)
logging.getLogger("apscheduler.scheduler").setLevel(logging.WARNING)

local_tz = get_localzone()

# Single scheduler instance — misfire_grace_time=3600 means jobs that fire
# up to 1 hour late still execute (APScheduler default is 1 SECOND which
# silently drops jobs when the event loop is busy with database writes etc.)
scheduler = AsyncIOScheduler(
    timezone=local_tz,
    job_defaults={
        "misfire_grace_time": 3600,
        "coalesce": True,  # if multiple fires missed, run once
    },
)


class SchedulerService:
    @staticmethod
    def _is_market_hours() -> bool:
        """Check if we're within US stock market hours (9:30 AM - 4:00 PM ET, weekdays)."""
        et = pytz.timezone("US/Eastern")
        now = datetime.now(et)
        if now.weekday() >= 5:  # Saturday/Sunday
            return False
        market_open = now.replace(hour=9, minute=30, second=0, microsecond=0)
        market_close = now.replace(hour=16, minute=0, second=0, microsecond=0)
        return market_open <= now <= market_close

    @staticmethod
    def _sync_next_run_to_db(job_id: str):
        """Read APScheduler's computed next_run_time and persist to DB."""
        try:
            job = scheduler.get_job(job_id)
            nrt_val = getattr(job, "next_run_time", None) if job else None
            if nrt_val:
                nrt = nrt_val.astimezone(timezone.utc).isoformat()
                with get_db() as db:
                    db.execute(
                        "UPDATE cycle_schedules SET next_run_at = %s WHERE id = %s",
                        [nrt, job_id],
                    )
                    logger.info(
                        "[SCHEDULER] Job %s next_run_at synced: %s", job_id, nrt
                    )
            else:
                if scheduler.running:
                    logger.warning("[SCHEDULER] Job %s has no next_run_time", job_id)
        except Exception as e:
            logger.warning(
                "[SCHEDULER] Failed to sync next_run_at for %s: %s", job_id, e
            )

    @staticmethod
    async def execute_schedule(schedule_id: str):
        """Callback fired by APScheduler when it's time to run a cycle."""
        # CRITICAL FIX: Do NOT auto-resume from paused state.
        # When the user hits Stop, the system enters paused state.
        # Previously, scheduled cycles would auto-resume, causing
        # zombie processing to restart even after explicit stop.
        if cycle_control.is_paused or cycle_control.is_stopped:
            logger.info(
                "[SCHEDULER] System is PAUSED/STOPPED — skipping scheduled cycle %s "
                "(user must manually resume or start a new cycle)",
                schedule_id,
            )
            return

        logger.info(
            "[SCHEDULER] ====== TRIGGER FIRED for schedule %s ======", schedule_id
        )
        with get_db() as db:
            # Load latest config from DB to ensure it wasn't deleted or paused
            row = db.execute(
                "SELECT id, name, schedule_type, cron_expression, interval_hours, earliest_window, "
                "collect, \"analyze\", trade, tickers, max_tickers, discovered_tickers, market_hours_only, "
                "is_active, last_run_at, next_run_at, run_count, last_status, last_error, "
                "created_at, updated_at FROM cycle_schedules WHERE id = %s", [schedule_id]
            ).fetchone()
            if not row:
                logger.warning(
                    "[SCHEDULER] Schedule %s not found in DB, removing from engine.",
                    schedule_id,
                )
                try:
                    scheduler.remove_job(schedule_id)
                except Exception:
                    pass
                return

            cols = [
                "id",
                "name",
                "schedule_type",
                "cron_expression",
                "interval_hours",
                "earliest_window",
                "collect",
                "analyze",
                "trade",
                "tickers",
                "max_tickers",
                "discovered_tickers",
                "market_hours_only",
                "is_active",
                "last_run_at",
                "next_run_at",
                "run_count",
                "last_status",
                "last_error",
                "created_at",
                "updated_at",
            ]
            s = dict(zip(cols, row))

            if not s["is_active"]:
                logger.info(
                    "[SCHEDULER] Schedule %s is inactive, skipping.", schedule_id
                )
                return

            # Pre-run check from validator
            try:
                from app.validation.schedule_validator import ScheduleValidator
                is_valid, reject_reason = ScheduleValidator.pre_run_check(schedule_id)
                if not is_valid:
                    logger.info("[SCHEDULER] Pre-run validation failed for %s: %s", schedule_id, reject_reason)
                    return
            except Exception as val_e:
                logger.warning("[SCHEDULER] Validator error (continuing): %s", val_e)

            if s["market_hours_only"] and not SchedulerService._is_market_hours():
                logger.info(
                    "[SCHEDULER] Schedule %s skipped (outside market hours).",
                    schedule_id,
                )
                # Still sync next_run_at so the timer keeps counting
                SchedulerService._sync_next_run_to_db(schedule_id)
                return

            tickers = []
            try:
                if s["tickers"]:
                    tickers = json.loads(s["tickers"])
            except Exception:
                tickers = []

            # Dispatch cycle via system_commands table (picked up by cycle_main poller)
            payload = {
                "tickers": tickers,
                "collect": bool(s["collect"]),
                "analyze": bool(s["analyze"]),
                "trade": bool(s["trade"]) if s["trade"] is not None else True,
                "max_tickers": s.get("max_tickers"),
                "discovered_tickers": s.get("discovered_tickers"),
                "dynamic_selection_mode": True,
            }

            logger.info(
                "[SCHEDULER] Execute schedule detail: schedule_id=%s, name=%s, tickers=%s, max_tickers=%s, discovered_tickers=%s, payload=%s",
                schedule_id,
                s["name"],
                s["tickers"],
                s.get("max_tickers"),
                s.get("discovered_tickers"),
                json.dumps(payload),
            )

            run_status = "ok"
            err_msg = ""
            try:
                # Check if a cycle is already running before dispatching
                state_row = db.execute(
                    "SELECT status FROM pipeline_state WHERE singleton_id = 'current'"
                ).fetchone()
                if state_row and state_row[0] not in ("idle", "done", "error", "stopped", "interrupted"):
                    raise HTTPException(409, f"Cycle already running: {state_row[0]}")

                cmd_id = f"sch-cmd-{uuid.uuid4().hex[:8]}"
                db.execute(
                    "INSERT INTO v3_system_commands (id, command_type, payload) VALUES (%s, %s, %s)",
                    [cmd_id, "START_CYCLE", json.dumps(payload)]
                )
                logger.info(
                    "[SCHEDULER] Successfully triggered cycle run for schedule %s.",
                    schedule_id,
                )
            except HTTPException as he:
                if he.status_code == 409:
                    logger.info(
                        "[SCHEDULER] Schedule %s skipped: cycle already running.",
                        schedule_id,
                    )
                    run_status = "skipped"
                    err_msg = "cycle already running"
                else:
                    logger.error(
                        "[SCHEDULER] Failed to trigger schedule %s: %s",
                        schedule_id,
                        he.detail,
                    )
                    run_status = "error"
                    err_msg = he.detail
            except Exception as e:
                logger.error(
                    "[SCHEDULER] Unexpected error triggering schedule %s: %s",
                    schedule_id,
                    e,
                    exc_info=True,
                )
                run_status = "error"
                err_msg = str(e)

            # Update run stats in DB
            now = datetime.now(timezone.utc)
            db.execute(
                """
                UPDATE cycle_schedules 
                SET last_run_at = %s, run_count = run_count + 1, last_status = %s, last_error = %s
                WHERE id = %s
            """,
                [now.isoformat(), run_status, err_msg, schedule_id],
            )

            # Sync next_run_at from APScheduler (it auto-advances the trigger)
            SchedulerService._sync_next_run_to_db(schedule_id)

            # Log to scheduler history
            try:
                history_id = f"hist-{uuid.uuid4().hex[:8]}"
                db.execute(
                    """
                    INSERT INTO scheduler_history (id, job_name, started_at, finished_at, status, notes)
                    VALUES (%s, %s, %s, %s, %s, %s)
                """,
                    [
                        history_id,
                        s["name"],
                        now.isoformat(),
                        now.isoformat(),
                        run_status,
                        err_msg,
                    ],
                )
            except Exception as filter_e:
                logger.warning("[SCHEDULER] Failed to write history log: %s", filter_e)

    @staticmethod
    def load_all_schedules():
        """Load all active schedules from DB into APScheduler."""
        logger.info("[SCHEDULER] Loading schedules from DB...")
        with get_db() as db:
            rows = db.execute(
                "SELECT id, name, schedule_type, cron_expression, interval_hours, earliest_window, "
                "collect, \"analyze\", trade, tickers, max_tickers, discovered_tickers, market_hours_only, "
                "is_active, last_run_at, next_run_at, run_count, last_status, last_error, "
                "created_at, updated_at FROM cycle_schedules WHERE is_active = TRUE"
            ).fetchall()

            cols = [
                "id",
                "name",
                "schedule_type",
                "cron_expression",
                "interval_hours",
                "earliest_window",
                "collect",
                "analyze",
                "trade",
                "tickers",
                "max_tickers",
                "discovered_tickers",
                "market_hours_only",
                "is_active",
                "last_run_at",
                "next_run_at",
                "run_count",
                "last_status",
                "last_error",
                "created_at",
                "updated_at",
            ]

            count = 0
            for row in rows:
                s = dict(zip(cols, row))
                SchedulerService._add_job_to_scheduler(s)
                count += 1

            logger.info("[SCHEDULER] Loaded %d active schedules.", count)

    @staticmethod
    def _add_job_to_scheduler(s: dict):
        """Add a job config to the APScheduler engine."""
        job_id = s["id"]

        # Remove if already exists
        if scheduler.get_job(job_id):
            scheduler.remove_job(job_id)

        if not s["is_active"]:
            return

        trigger = None
        if s["schedule_type"] == "cron" and s["cron_expression"]:
            parts = s["cron_expression"].strip().split()
            if len(parts) >= 5:
                trigger = CronTrigger(
                    minute=parts[0],
                    hour=parts[1],
                    day=parts[2],
                    month=parts[3],
                    day_of_week=parts[4],
                    timezone=local_tz,
                )
        elif s["schedule_type"] == "interval" and s["interval_hours"]:
            trigger = IntervalTrigger(
                hours=float(s["interval_hours"]), timezone=local_tz
            )
        elif s["schedule_type"] == "policy" and s["earliest_window"]:
            try:
                from app.services.market_calendar import MarketCalendar
                from apscheduler.triggers.date import DateTrigger
                
                # Check if it was supposed to run in the past but missed
                run_time = MarketCalendar.get_next_window(s["earliest_window"])
                if run_time < datetime.now(local_tz):
                    # It missed its window (e.g. system was down), run immediately
                    run_time = datetime.now(local_tz) + timedelta(seconds=5)
                    
                trigger = DateTrigger(run_date=run_time, timezone=local_tz)
            except Exception as e:
                logger.error("[SCHEDULER] Failed to create policy trigger for %s: %s", job_id, e)

        if trigger:
            scheduler.add_job(
                SchedulerService.execute_schedule,
                trigger=trigger,
                args=[job_id],
                id=job_id,
                replace_existing=True,
                misfire_grace_time=3600,
                coalesce=True,
            )
            # Sync the computed next_run_time to DB
            SchedulerService._sync_next_run_to_db(job_id)

            job = scheduler.get_job(job_id)
            nrt_val = getattr(job, "next_run_time", None) if job else None
            nrt = nrt_val.astimezone(timezone.utc).isoformat() if nrt_val else "UNKNOWN"
            logger.info(
                "[SCHEDULER] Registered job %s (%s) — trigger=%s, next_fire=%s",
                job_id,
                s.get("name", "?"),
                s["schedule_type"],
                nrt,
            )
        else:
            logger.warning("Invalid trigger config for job %s", job_id)

    @staticmethod
    def refresh_job(schedule_id: str):
        """Refresh a specific job in APScheduler from the DB."""
        with get_db() as db:
            row = db.execute(
                "SELECT id, name, schedule_type, cron_expression, interval_hours, "
                "collect, \"analyze\", trade, tickers, max_tickers, discovered_tickers, market_hours_only, "
                "is_active, last_run_at, next_run_at, run_count, last_status, last_error, "
                "created_at, updated_at FROM cycle_schedules WHERE id = %s", [schedule_id]
            ).fetchone()
            if not row:
                if scheduler.get_job(schedule_id):
                    scheduler.remove_job(schedule_id)
                return

            cols = [
                "id",
                "name",
                "schedule_type",
                "cron_expression",
                "interval_hours",
                "collect",
                "analyze",
                "trade",
                "tickers",
                "max_tickers",
                "discovered_tickers",
                "market_hours_only",
                "is_active",
                "last_run_at",
                "next_run_at",
                "run_count",
                "last_status",
                "last_error",
                "created_at",
                "updated_at",
            ]
            s = dict(zip(cols, row))

            if scheduler.get_job(schedule_id):
                scheduler.remove_job(schedule_id)

            if s["is_active"]:
                SchedulerService._add_job_to_scheduler(s)

    @staticmethod
    def get_next_runs() -> dict:
        """Return {job_id: next_run_time_iso} for all registered jobs."""
        result = {}
        for job in scheduler.get_jobs():
            nrt_val = getattr(job, "next_run_time", None)
            result[job.id] = nrt_val.astimezone(timezone.utc).isoformat() if nrt_val else None
        return result

    @staticmethod
    def start():
        if not scheduler.running:
            # Load existing schedules and start engine FIRST
            SchedulerService.load_all_schedules()
            scheduler.start()
            logger.info(
                "[SCHEDULER] Engine started (tz=%s, misfire_grace=3600s, coalesce=True)",
                local_tz,
            )
            
            # Sync next_run_times computed by the engine start
            for job in scheduler.get_jobs():
                SchedulerService._sync_next_run_to_db(job.id)

            # ── Background Stop-Loss Monitor ──
            # Run stop-loss checks for the active bot every 1 minute
            try:
                scheduler.add_job(
                    SchedulerService._run_background_stop_loss,
                    trigger=IntervalTrigger(minutes=1, timezone=local_tz),
                    id="background_stop_loss",
                    replace_existing=True,
                    misfire_grace_time=60,
                    coalesce=True,
                )
                logger.info(
                    "[SCHEDULER] Registered background stop-loss monitor (interval: 1m)"
                )
            except Exception as e:
                logger.warning(
                    "[SCHEDULER] Failed to register background stop-loss: %s", e
                )

            # ── Morning Briefing Generator (6:30 AM Pacific) ──
            pt_tz = pytz.timezone("America/Los_Angeles")
            try:
                scheduler.add_job(
                    SchedulerService._run_morning_briefing,
                    trigger=CronTrigger(hour=6, minute=30, timezone=pt_tz),
                    id="morning_briefing_job",
                    replace_existing=True,
                    misfire_grace_time=3600,
                    coalesce=True,
                )
                logger.info(
                    "[SCHEDULER] Registered morning briefing generator (cron: 6:30 AM PT)"
                )
            except Exception as e:
                logger.warning(
                    "[SCHEDULER] Failed to register morning briefing job: %s", e
                )

            # ── Enriched Live Feed Reports (7:00 AM, 11:00 AM, 1:00 PM, and 6:00 PM Pacific, Weekdays) ──
            try:
                # 7:00 AM Market Open Report
                scheduler.add_job(
                    SchedulerService._run_flash_briefing,
                    trigger=CronTrigger(hour=7, minute=0, day_of_week="mon-fri", timezone=pt_tz),
                    args=["market_open"],
                    id="flash_briefing_7am",
                    replace_existing=True,
                    misfire_grace_time=3600,
                    coalesce=True,
                )
                # 11:00 AM Mid-day Report
                scheduler.add_job(
                    SchedulerService._run_flash_briefing,
                    trigger=CronTrigger(hour=11, minute=0, day_of_week="mon-fri", timezone=pt_tz),
                    args=["mid_day"],
                    id="flash_briefing_11am",
                    replace_existing=True,
                    misfire_grace_time=3600,
                    coalesce=True,
                )
                # 1:00 PM Late-day/Close Report
                scheduler.add_job(
                    SchedulerService._run_flash_briefing,
                    trigger=CronTrigger(hour=13, minute=0, day_of_week="mon-fri", timezone=pt_tz),
                    args=["market_close_soon"],
                    id="flash_briefing_1pm",
                    replace_existing=True,
                    misfire_grace_time=3600,
                    coalesce=True,
                )
                # 6:00 PM After Hours Report
                scheduler.add_job(
                    SchedulerService._run_flash_briefing,
                    trigger=CronTrigger(hour=18, minute=0, day_of_week="mon-fri", timezone=pt_tz),
                    args=["after_hours"],
                    id="flash_briefing_6pm",
                    replace_existing=True,
                    misfire_grace_time=3600,
                    coalesce=True,
                )
                logger.info(
                    "[SCHEDULER] Registered enriched daily live feed briefings (7am, 11am, 1pm, 6pm PT)"
                )
            except Exception as e:
                logger.warning(
                    "[SCHEDULER] Failed to register enriched briefings: %s", e
                )



            # ── Background Ticker Validation ──
            try:
                scheduler.add_job(
                    SchedulerService._run_background_validation,
                    trigger=IntervalTrigger(minutes=5, timezone=local_tz),
                    id="background_validation",
                    replace_existing=True,
                    misfire_grace_time=300,
                    coalesce=True,
                )
                logger.info(
                    "[SCHEDULER] Registered background ticker validation (interval: 5m)"
                )
            except Exception as e:
                logger.warning(
                    "[SCHEDULER] Failed to register background ticker validation: %s", e
                )

    @staticmethod
    async def _run_background_stop_loss():
        """Run stop loss, take profit, and custom trigger checks for the active bot."""
        try:
            bot_id = get_active_bot_id()
            if bot_id:
                await check_stop_losses(bot_id, cycle_id="background")
                await check_take_profits(bot_id, cycle_id="background")
                # Custom order triggers (stop_loss, take_profit, buy_limit, sell_limit, trailing_stop)
                try:
                    from app.trading.order_triggers import check_triggers

                    fired = await check_triggers(bot_id)
                    if fired:
                        logger.info(
                            "[SCHEDULER] %d order trigger(s) fired for bot '%s'",
                            len(fired),
                            bot_id,
                        )
                except Exception as trig_err:
                    logger.error("[SCHEDULER] Order trigger check failed: %s", trig_err)
        except Exception as e:
            logger.error("[SCHEDULER] Background stop-loss check failed: %s", e)

    @staticmethod
    async def _run_morning_briefing():
        """Generate the morning briefing."""
        logger.info("[SCHEDULER] Morning briefing is a legacy V2 feature and is not run in V3.")
        return



    @staticmethod
    async def _run_flash_briefing(report_type: str | None = None):
        """Generate a flash briefing."""
        if cycle_control.is_paused:
            logger.info(f"[SCHEDULER] Skipping flash briefing ({report_type or 'auto'}): System is PAUSED.")
            return
        try:
            from app.services.flash_briefing import generate_flash_briefing
            await generate_flash_briefing(report_type=report_type)
        except Exception as e:
            logger.error(f"[SCHEDULER] Flash briefing ({report_type or 'auto'}) generation failed: {e}")

    @staticmethod
    async def _run_background_validation():
        """Run validation on pending tickers."""
        if cycle_control.is_paused:
            logger.info("[SCHEDULER] Skipping background ticker validation: System is PAUSED.")
            return
        try:
            from app.validation.runner import run_validation_batch
            await run_validation_batch()
        except Exception as e:
            logger.error("[SCHEDULER] Background ticker validation failed: %s", e)

    @staticmethod
    def stop():
        if scheduler.running:
            scheduler.shutdown(wait=False)
            logger.info("[SCHEDULER] Engine stopped.")
