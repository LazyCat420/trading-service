"""
Scheduler Service — Manages APScheduler for automated cycle runs.
"""

import asyncio
import os
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
from app.services.market_calendar import MarketCalendar
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
                "created_at, updated_at, run_at, expiry_at FROM cycle_schedules WHERE id = %s", [schedule_id]
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
                "run_at",
                "expiry_at",
            ]
            s = dict(zip(cols, row))

            if not s["is_active"]:
                logger.info(
                    "[SCHEDULER] Schedule %s is inactive, skipping.", schedule_id
                )
                return

            # TTL guardrail: expired schedules deactivate instead of running —
            # keeps stale bot-created research from firing forever.
            if SchedulerService._expire_if_past_ttl(s, db):
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
                if s["schedule_type"] in ("once", "policy"):
                    # DateTrigger is spent after firing — without re-arming, the
                    # schedule silently dies until the next reboot. Re-aim at
                    # the next market open.
                    SchedulerService._rearm_date_schedule(s, minutes_from_now=None)
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

            # One-shot semantics for DateTrigger-based schedules ('once' and
            # 'policy'). Without this they stay is_active=TRUE forever and
            # re-fire at EVERY reboot when load_all_schedules re-registers
            # them — a research doom loop. Success → deactivate. Failure or
            # skip → bounded retry (20 min), give up after 5 attempts total.
            if s["schedule_type"] in ("once", "policy"):
                attempts = (s["run_count"] or 0) + 1
                if run_status == "ok":
                    db.execute(
                        "UPDATE cycle_schedules SET is_active = FALSE, next_run_at = NULL, updated_at = %s WHERE id = %s",
                        [now.isoformat(), schedule_id],
                    )
                    logger.info(
                        "[SCHEDULER] One-shot schedule %s completed — deactivated.",
                        schedule_id,
                    )
                elif attempts >= 5:
                    db.execute(
                        "UPDATE cycle_schedules SET is_active = FALSE, next_run_at = NULL, last_status = 'gave_up', updated_at = %s WHERE id = %s",
                        [now.isoformat(), schedule_id],
                    )
                    logger.warning(
                        "[SCHEDULER] One-shot schedule %s gave up after %d attempts.",
                        schedule_id,
                        attempts,
                    )
                else:
                    SchedulerService._rearm_date_schedule(s, minutes_from_now=20)

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
                "created_at, updated_at, run_at, expiry_at FROM cycle_schedules WHERE is_active = TRUE"
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
                "run_at",
                "expiry_at",
            ]

            count = 0
            for row in rows:
                s = dict(zip(cols, row))
                if SchedulerService._expire_if_past_ttl(s, db):
                    continue
                # Retired: coarse-window 'policy' schedules are superseded by
                # the Watch Desk (watch_ticker). Deactivate any lingering rows
                # instead of arming them.
                if s["schedule_type"] == "policy":
                    db.execute(
                        "UPDATE cycle_schedules SET is_active = FALSE, next_run_at = NULL, "
                        "last_status = 'retired_policy' WHERE id = %s",
                        [s["id"]],
                    )
                    logger.info("[SCHEDULER] Retired policy schedule %s — deactivated.", s["id"])
                    continue
                # A DateTrigger-based schedule that already ran successfully
                # must not be re-armed at boot (pre-fix rows may still be
                # active with a spent trigger — see one-shot semantics).
                if s["schedule_type"] in ("once", "policy") and s["last_status"] == "ok" and s["last_run_at"]:
                    db.execute(
                        "UPDATE cycle_schedules SET is_active = FALSE, next_run_at = NULL WHERE id = %s",
                        [s["id"]],
                    )
                    logger.info(
                        "[SCHEDULER] One-shot schedule %s already ran (%s) — deactivating instead of re-arming.",
                        s["id"], s["last_run_at"],
                    )
                    continue
                SchedulerService._add_job_to_scheduler(s)
                count += 1

            logger.info("[SCHEDULER] Loaded %d active schedules.", count)

    @staticmethod
    def _expire_if_past_ttl(s: dict, db) -> bool:
        """Deactivate a schedule whose expiry_at has passed. Returns True if expired."""
        expiry = s.get("expiry_at")
        if not expiry:
            return False
        if isinstance(expiry, str):
            try:
                expiry = datetime.fromisoformat(expiry)
            except ValueError:
                return False
        # expiry_at is TIMESTAMP (naive); stored values are UTC.
        now_naive = datetime.now(timezone.utc).replace(tzinfo=None)
        if expiry.tzinfo is not None:
            expiry = expiry.astimezone(timezone.utc).replace(tzinfo=None)
        if expiry <= now_naive:
            db.execute(
                "UPDATE cycle_schedules SET is_active = FALSE, next_run_at = NULL, last_status = 'expired' WHERE id = %s",
                [s["id"]],
            )
            try:
                if scheduler.get_job(s["id"]):
                    scheduler.remove_job(s["id"])
            except Exception:
                pass
            logger.info("[SCHEDULER] Schedule %s expired (TTL %s) — deactivated.", s["id"], expiry)
            return True
        return False

    @staticmethod
    def _rearm_date_schedule(s: dict, minutes_from_now: int | None):
        """Re-register a spent DateTrigger schedule for a retry.

        minutes_from_now=None re-aims at the next market open; otherwise the
        job fires that many minutes from now.
        """
        from apscheduler.triggers.date import DateTrigger

        try:
            if minutes_from_now is None:
                run_time = MarketCalendar.get_next_window("next_open")
            else:
                run_time = datetime.now(timezone.utc) + timedelta(minutes=minutes_from_now)
            scheduler.add_job(
                SchedulerService.execute_schedule,
                trigger=DateTrigger(run_date=run_time),
                args=[s["id"]],
                id=s["id"],
                replace_existing=True,
                misfire_grace_time=3600,
                coalesce=True,
            )
            logger.info("[SCHEDULER] Re-armed schedule %s for %s.", s["id"], run_time)
        except Exception as e:
            logger.error("[SCHEDULER] Failed to re-arm schedule %s: %s", s["id"], e)

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
        # NOTE: 'policy' schedules are retired — load_all_schedules deactivates
        # every policy row on boot (Watch Desk owns condition-driven wakes), so
        # the old DateTrigger branch for them was dead code and is gone.
        elif s["schedule_type"] == "once" and s.get("run_at"):
            # One-shot at an exact datetime — used by agents to snipe research
            # around known events (earnings drops, Fed announcements, ...).
            try:
                from apscheduler.triggers.date import DateTrigger

                run_time = s["run_at"]
                if isinstance(run_time, str):
                    run_time = datetime.fromisoformat(run_time)
                if run_time.tzinfo is None:
                    # Stored naive — treat as UTC (governor writes UTC).
                    run_time = run_time.replace(tzinfo=timezone.utc)
                if run_time < datetime.now(timezone.utc):
                    # Missed (e.g. reboot) — fire shortly, execute_schedule's
                    # gates still apply.
                    run_time = datetime.now(timezone.utc) + timedelta(seconds=30)
                trigger = DateTrigger(run_date=run_time)
            except Exception as e:
                logger.error("[SCHEDULER] Failed to create once trigger for %s: %s", job_id, e)

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
            # NB: must select earliest_window — _add_job_to_scheduler KeyErrors
            # on policy-type schedules without it.
            row = db.execute(
                "SELECT id, name, schedule_type, cron_expression, interval_hours, earliest_window, "
                "collect, \"analyze\", trade, tickers, max_tickers, discovered_tickers, market_hours_only, "
                "is_active, last_run_at, next_run_at, run_count, last_status, last_error, "
                "created_at, updated_at, run_at, expiry_at FROM cycle_schedules WHERE id = %s", [schedule_id]
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
                "run_at",
                "expiry_at",
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

            # ── Self-healing watchdog (hourly, diagnose-only by default) ──
            # Previously this existed as a script nothing ever called: no
            # scheduler wiring, no importer. Engineering failures sat in
            # pipeline_state.error until a human noticed.
            try:
                scheduler.add_job(
                    SchedulerService._run_self_healing,
                    trigger=IntervalTrigger(hours=1, timezone=local_tz),
                    id="self_healing_watchdog",
                    replace_existing=True,
                    misfire_grace_time=1800,
                    coalesce=True,
                    max_instances=1,   # a repair pass must never overlap itself
                )
                logger.info(
                    "[SCHEDULER] Registered self-healing watchdog (interval: 1h, mode=%s)",
                    os.getenv("SELF_HEAL_MODE", "diagnose"),
                )
            except Exception as e:
                logger.warning(
                    "[SCHEDULER] Failed to register self-healing watchdog: %s", e
                )

            # ── Equation Lab: nightly strategy R&D (8 PM Pacific, after close) ──
            # Compiles the most-used unbacktestable equation stubs from the
            # tournament into real signal code and backtests them, so the jury
            # sees actual PnL instead of "N/A" and the library accumulates
            # honest win_rate/sharpe stats over time.
            try:
                scheduler.add_job(
                    SchedulerService._run_equation_lab,
                    trigger=CronTrigger(hour=20, minute=0, timezone=pytz.timezone("America/Los_Angeles")),
                    id="equation_lab_nightly",
                    replace_existing=True,
                    misfire_grace_time=3600,
                    coalesce=True,
                )
                logger.info("[SCHEDULER] Registered Equation Lab (cron: 8:00 PM PT)")
            except Exception as e:
                logger.warning("[SCHEDULER] Failed to register Equation Lab: %s", e)

            # ── Macro data refresh (5 PM PT weekdays, after FRED's ~4:15 ET
            # daily updates settle). Collection was startup-only before, so
            # data freshness was an accident of deploy frequency.
            try:
                scheduler.add_job(
                    SchedulerService._run_macro_refresh,
                    trigger=CronTrigger(hour=17, minute=0, day_of_week="mon-fri",
                                        timezone=pytz.timezone("America/Los_Angeles")),
                    id="macro_refresh_daily",
                    replace_existing=True,
                    misfire_grace_time=3600,
                    coalesce=True,
                )
                logger.info("[SCHEDULER] Registered macro refresh (cron: 5:00 PM PT, mon-fri)")
            except Exception as e:
                logger.warning("[SCHEDULER] Failed to register macro refresh: %s", e)

            # ── Smart money: 13F collection + return recomputation ──
            # 13F filings are due 45 days after quarter end, so funds land in a
            # burst through mid-Feb/May/Aug/Nov. We sweep daily during those
            # months rather than once per quarter — a single quarterly fire that
            # misses (container restart, EDGAR timeout) would cost a whole
            # quarter of history. collect_all_funds() had NO caller at all
            # before this, which is why only 15 of 540 filers had any history.
            try:
                scheduler.add_job(
                    SchedulerService._run_13f_collection,
                    trigger=CronTrigger(month="2,5,8,11", day="14-28", hour=3, minute=0,
                                        timezone=pytz.timezone("America/Los_Angeles")),
                    id="sec_13f_collection",
                    replace_existing=True,
                    misfire_grace_time=7200,
                    coalesce=True,
                )
                logger.info("[SCHEDULER] Registered 13F collection (cron: 3:00 AM PT, filing months)")
            except Exception as e:
                logger.warning("[SCHEDULER] Failed to register 13F collection: %s", e)

            # 13F weekly sweep, year-round: the filing-month cron above misses
            # early filers and amendments (nothing ran May 28 → Aug 14, leaving
            # holdings a full quarter stale). collect_all_funds is idempotent.
            try:
                scheduler.add_job(
                    SchedulerService._run_13f_collection,
                    trigger=CronTrigger(day_of_week="sun", hour=3, minute=30,
                                        timezone=pytz.timezone("America/Los_Angeles")),
                    id="sec_13f_weekly",
                    replace_existing=True,
                    misfire_grace_time=7200,
                    coalesce=True,
                )
                logger.info("[SCHEDULER] Registered weekly 13F sweep (Sun 3:30 AM PT)")
            except Exception as e:
                logger.warning("[SCHEDULER] Failed to register weekly 13F sweep: %s", e)

            # Nightly stale-first fundamentals refresh (2026-07-23 audit:
            # 653/727 tickers >14d stale because nothing refreshed off-cycle).
            try:
                scheduler.add_job(
                    SchedulerService._run_fundamentals_refresh,
                    trigger=CronTrigger(hour=2, minute=30,
                                        timezone=pytz.timezone("America/Los_Angeles")),
                    id="fundamentals_refresh",
                    replace_existing=True,
                    misfire_grace_time=3600,
                    coalesce=True,
                )
                logger.info("[SCHEDULER] Registered nightly fundamentals refresh (2:30 AM PT, 40 stalest)")
            except Exception as e:
                logger.warning("[SCHEDULER] Failed to register fundamentals refresh: %s", e)

            # ── Formerly-orphaned collectors (2026-07-23): these five modules
            # existed with zero callers, so put_call_ratio / insider_trades /
            # economic_calendar / social_posts sat at 0 rows while agents
            # queried them. All wrappers are fail-soft (log and move on). ──
            try:
                scheduler.add_job(
                    SchedulerService._run_pcr_collection,
                    trigger=CronTrigger(hour=13, minute=15,
                                        timezone=pytz.timezone("America/Los_Angeles")),
                    id="pcr_collection",
                    replace_existing=True,
                    misfire_grace_time=3600,
                    coalesce=True,
                )
                scheduler.add_job(
                    SchedulerService._run_insider_collection,
                    trigger=CronTrigger(hour=4, minute=30,
                                        timezone=pytz.timezone("America/Los_Angeles")),
                    id="insider_collection",
                    replace_existing=True,
                    misfire_grace_time=3600,
                    coalesce=True,
                )
                scheduler.add_job(
                    SchedulerService._run_economic_calendar_collection,
                    trigger=IntervalTrigger(hours=12),
                    id="economic_calendar_collection",
                    replace_existing=True,
                    misfire_grace_time=3600,
                    coalesce=True,
                )
                scheduler.add_job(
                    SchedulerService._run_social_collection,
                    trigger=IntervalTrigger(hours=6),
                    id="social_collection",
                    replace_existing=True,
                    misfire_grace_time=3600,
                    coalesce=True,
                )
                scheduler.add_job(
                    SchedulerService._run_youtube_prewarm,
                    trigger=IntervalTrigger(hours=4),
                    id="youtube_prewarm",
                    replace_existing=True,
                    misfire_grace_time=3600,
                    coalesce=True,
                )
                scheduler.add_job(
                    SchedulerService._run_youtube_channel_sweep,
                    trigger=CronTrigger(hour=1, minute=30,
                                        timezone=pytz.timezone("America/Los_Angeles")),
                    id="youtube_channel_sweep",
                    replace_existing=True,
                    misfire_grace_time=7200,
                    coalesce=True,
                )
                # Missed-slot catch-up: the scheduler is in-memory, so a
                # restart shortly before/during the 1:30 AM slot loses the job
                # (misfire_grace only covers jobs that were registered when the
                # slot passed — observed 07-23: container up 01:16, scheduler
                # up 01:43, sweep never ran). If we boot within 6h after the
                # slot, run once shortly after startup; transcripts dedup on
                # video_id so a double-run is a cheap no-op.
                from apscheduler.triggers.date import DateTrigger
                _pt_now = datetime.now(pytz.timezone("America/Los_Angeles"))
                _slot = _pt_now.replace(hour=1, minute=30, second=0, microsecond=0)
                if _slot < _pt_now < _slot + timedelta(hours=6):
                    scheduler.add_job(
                        SchedulerService._run_youtube_channel_sweep,
                        trigger=DateTrigger(
                            run_date=datetime.now(timezone.utc) + timedelta(minutes=3)),
                        id="youtube_channel_sweep_catchup",
                        replace_existing=True,
                    )
                    logger.info("[SCHEDULER] youtube channel sweep: missed 1:30 AM PT slot — catch-up run in 3 min")
                logger.info(
                    "[SCHEDULER] Registered collectors: PCR (1:15 PM PT), insider "
                    "(4:30 AM PT), economic calendar (12h), social (6h), "
                    "youtube prewarm (4h), youtube channel sweep (1:30 AM PT)"
                )
            except Exception as e:
                logger.warning("[SCHEDULER] Failed to register wave collectors: %s", e)

            # Returns are recomputed nightly: new prices arrive daily, so a
            # trade scored today with a partial window becomes fully scoreable
            # later. Idempotent — safe to re-run.
            try:
                scheduler.add_job(
                    SchedulerService._run_smart_money_returns,
                    trigger=CronTrigger(hour=2, minute=0,
                                        timezone=pytz.timezone("America/Los_Angeles")),
                    id="smart_money_returns",
                    replace_existing=True,
                    misfire_grace_time=7200,
                    coalesce=True,
                )
                logger.info("[SCHEDULER] Registered smart-money returns recompute (cron: 2:00 AM PT)")
            except Exception as e:
                logger.warning("[SCHEDULER] Failed to register smart-money returns: %s", e)

            # ── Morning Trading Cycle (market open: 6:30 AM Pacific = 9:30 AM ET) ──
            pt_tz = pytz.timezone("America/Los_Angeles")
            try:
                scheduler.add_job(
                    SchedulerService._run_market_open_cycle,
                    trigger=CronTrigger(hour=6, minute=30, day_of_week="mon-fri", timezone=pt_tz),
                    id="market_open_cycle",
                    replace_existing=True,
                    misfire_grace_time=3600,
                    coalesce=True,
                )
                logger.info(
                    "[SCHEDULER] Registered market-open trading cycle (cron: 6:30 AM PT, mon-fri)"
                )
            except Exception as e:
                logger.warning(
                    "[SCHEDULER] Failed to register market-open trading cycle: %s", e
                )

            # ── Live Feed Reports — governed interval (default 4h); report type auto-selected by time of day ──
            try:
                from app.services.parameter_store import get_param as _get_param
                _flash_hours = int(_get_param("FLASH_BRIEFING_INTERVAL_HOURS"))
                scheduler.add_job(
                    SchedulerService._run_flash_briefing,
                    trigger=IntervalTrigger(hours=_flash_hours, timezone=local_tz),
                    id="flash_briefing_4h",
                    replace_existing=True,
                    misfire_grace_time=3600,
                    coalesce=True,
                )
                logger.info(
                    "[SCHEDULER] Registered live feed flash briefings (interval: every %dh)",
                    _flash_hours,
                )
            except Exception as e:
                logger.warning(
                    "[SCHEDULER] Failed to register flash briefings: %s", e
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

            # ── Watch Desk: cheap background watch evaluation (no LLM) ──
            # Evaluates agent-defined watch conditions every 15m and wakes the
            # agent ONLY when a trigger trips. This is the energy-saver: the
            # expensive cycle stays off until a real, thesis-relevant condition
            # is met. See app/services/watch_desk.py.
            try:
                from app.services.parameter_store import get_param as _get_param
                _wd_minutes = int(_get_param("WATCHDESK_EVAL_INTERVAL_MINUTES"))
                scheduler.add_job(
                    SchedulerService._run_watchdesk_evaluation,
                    trigger=IntervalTrigger(minutes=_wd_minutes, timezone=local_tz),
                    id="watchdesk_evaluation",
                    replace_existing=True,
                    misfire_grace_time=300,
                    coalesce=True,
                )
                logger.info(
                    "[SCHEDULER] Registered Watch Desk evaluation (interval: %dm)",
                    _wd_minutes,
                )
            except Exception as e:
                logger.warning(
                    "[SCHEDULER] Failed to register Watch Desk evaluation: %s", e
                )

            # ── Cadence sync: reconcile governed intervals onto live jobs ──
            # Agents adjust cadence parameters through the Parameter Governor;
            # this periodic pass (plus a best-effort push from the governor)
            # retunes the APScheduler jobs to match the store.
            try:
                scheduler.add_job(
                    SchedulerService.sync_cadence_jobs,
                    trigger=IntervalTrigger(minutes=5, timezone=local_tz),
                    id="parameter_cadence_sync",
                    replace_existing=True,
                    misfire_grace_time=300,
                    coalesce=True,
                )
                logger.info("[SCHEDULER] Registered parameter cadence sync (interval: 5m)")
            except Exception as e:
                logger.warning(
                    "[SCHEDULER] Failed to register parameter cadence sync: %s", e
                )

    @staticmethod
    def sync_cadence_jobs() -> list[str]:
        """Retune interval jobs whose governed cadence parameter changed.

        Reads every PARAMETER_REGISTRY entry with a scheduler_job binding and
        reschedules the live APScheduler job when its interval no longer
        matches the store. Returns the list of job ids that were retuned.
        """
        from app.services.parameter_store import PARAMETER_REGISTRY, get_param

        retuned: list[str] = []
        for key, spec in PARAMETER_REGISTRY.items():
            if not spec.scheduler_job:
                continue
            job_id, unit = spec.scheduler_job
            try:
                job = scheduler.get_job(job_id)
                if job is None or not isinstance(job.trigger, IntervalTrigger):
                    continue
                desired = int(get_param(key))
                desired_sec = desired * (3600 if unit == "hours" else 60)
                current_sec = int(job.trigger.interval.total_seconds())
                if current_sec != desired_sec:
                    scheduler.reschedule_job(
                        job_id,
                        trigger=IntervalTrigger(**{unit: desired}, timezone=local_tz),
                    )
                    retuned.append(job_id)
                    logger.warning(
                        "[SCHEDULER] Cadence retuned: %s %ds -> %ds (%s=%s)",
                        job_id, current_sec, desired_sec, key, desired,
                    )
            except Exception as e:
                logger.warning("[SCHEDULER] Cadence sync failed for %s: %s", job_id, e)
        return retuned

    @staticmethod
    async def _run_background_stop_loss():
        """Run stop loss, take profit, and custom trigger checks for EVERY bot
        holding positions.

        Sweeping only the active bot silently orphaned every other profile's
        positions — audited live at ~$69k of entry value (cycle-backend + test
        bots) carrying no stop-loss, take-profit, or trigger monitoring at
        all. Risk protection must not depend on which bot the UI has selected.
        """
        try:
            with get_db() as db:
                rows = db.execute(
                    "SELECT DISTINCT bot_id FROM positions WHERE qty > 0"
                ).fetchall()
            bot_ids = [r[0] for r in rows if r and r[0]]
            active = get_active_bot_id()
            if active and active not in bot_ids:
                bot_ids.append(active)

            # Per-pass cycle id, NOT the constant "background": sell()'s
            # duplicate-order guard keys on (cycle_id, ticker, side), so a
            # constant id meant any ticker that background-stopped ONCE could
            # never be background-sold again — its protective stop was
            # silently dead forever after (live-confirmed on GOOGL/AMP).
            # A per-minute id keeps the double-sell protection WITHIN a pass
            # (stop-loss and take-profit share it) while future passes start clean.
            from datetime import datetime, timezone
            bg_cycle = f"background-{datetime.now(timezone.utc):%Y%m%d%H%M}"

            for bot_id in bot_ids:
                try:
                    await check_stop_losses(bot_id, cycle_id=bg_cycle)
                    await check_take_profits(bot_id, cycle_id=bg_cycle)
                    # Custom order triggers (stop_loss, take_profit, buy_limit,
                    # sell_limit, trailing_stop)
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
                        logger.error(
                            "[SCHEDULER] Order trigger check failed for bot '%s': %s",
                            bot_id, trig_err,
                        )
                except Exception as bot_err:
                    logger.error(
                        "[SCHEDULER] Background risk sweep failed for bot '%s': %s",
                        bot_id, bot_err,
                    )
        except Exception as e:
            logger.error("[SCHEDULER] Background stop-loss check failed: %s", e)

    @staticmethod
    async def _run_self_healing():
        """Hourly engineering-failure diagnosis.

        Defaults to diagnose-only (SELF_HEAL_MODE): the debate council proposes a
        patch and it is persisted for review, but nothing is written to disk and
        nothing is ever committed, pushed, or redeployed.

        Calls heal_once(), NOT run_healing_cycle() — the latter is the standalone
        entrypoint and shuts the service down when it finishes.
        """
        import sys as _sys
        from pathlib import Path as _Path

        try:
            # scripts/ is not a package; put it on the path to import the watchdog.
            _scripts = str(_Path(__file__).resolve().parents[2] / "scripts")
            if _scripts not in _sys.path:
                _sys.path.insert(0, _scripts)
            from self_healing_watchdog import heal_once

            logger.info(
                "[SCHEDULER] Self-healing sweep starting (mode=%s)",
                os.getenv("SELF_HEAL_MODE", "diagnose"),
            )
            await heal_once()
        except Exception as e:
            logger.error("[SCHEDULER] Self-healing sweep failed: %s", e)

    @staticmethod
    async def _run_equation_lab():
        """Nightly equation R&D — compile + backtest stubbed tournament equations."""
        try:
            from app.cognition.debate.equation_lab import run_equation_lab
            await run_equation_lab()
        except Exception as e:
            logger.error("[SCHEDULER] Equation Lab run failed: %s", e)

    @staticmethod
    async def _run_macro_refresh():
        """Daily FRED + futures/commodities refresh (weekday post-close)."""
        import asyncio as _asyncio
        try:
            from app.collectors.fred_collector import sync_collect_fred
            total = await _asyncio.to_thread(sync_collect_fred, lambda: False)
            logger.info("[SCHEDULER] Macro refresh: %d FRED rows", total)
        except Exception as e:
            logger.error("[SCHEDULER] FRED refresh failed: %s", e)
        try:
            from app.collectors.market_regime_collector import collect_market_data
            result = await collect_market_data(period="6mo")
            logger.info("[SCHEDULER] Macro refresh: %s market rows", result.get("total", 0))
        except Exception as e:
            logger.error("[SCHEDULER] Market data refresh failed: %s", e)

    @staticmethod
    async def _run_13f_collection():
        """Pull 13F holdings for every tracked fund, then seed leads.

        Also injects consensus buys into discovered_tickers so institutional
        conviction can surface a ticker the watchlist never contained.
        """
        try:
            from app.collectors.sec_collector import collect_all_funds

            result = await collect_all_funds()
            total = sum(result.values()) if isinstance(result, dict) else 0
            logger.info("[SCHEDULER] 13F collection: %s holdings rows", total)
        except Exception as e:
            logger.error("[SCHEDULER] 13F collection failed: %s", e)

        try:
            await SchedulerService._inject_smart_money_leads()
        except Exception as e:
            logger.error("[SCHEDULER] Smart-money lead injection failed: %s", e)

    @staticmethod
    async def _run_fundamentals_refresh(batch: int = 40):
        """Refresh the stalest active-watchlist fundamentals (provider-rotated).

        Fundamentals previously refreshed ONLY when a ticker was analyzed in a
        cycle (5-8/cycle), so 653/727 tickers sat >14 days stale. 40/night
        covers the whole active watchlist in under a week.
        """
        try:
            from app.collectors.data_rotator import fetch_fundamentals
            from app.db.connection import get_db

            with get_db() as db:
                rows = db.execute(
                    """
                    SELECT w.ticker, MAX(f.snapshot_date) AS last_snap
                    FROM watchlist w
                    LEFT JOIN fundamentals f ON f.ticker = w.ticker
                    WHERE w.status = 'active'
                    GROUP BY w.ticker
                    ORDER BY last_snap ASC NULLS FIRST
                    LIMIT %s
                    """,
                    [batch],
                ).fetchall()
            done = 0
            for (ticker, _last) in rows:
                try:
                    if await fetch_fundamentals(ticker):
                        done += 1
                except Exception as te:
                    logger.warning("[SCHEDULER] fundamentals refresh %s failed: %s", ticker, te)
            logger.info("[SCHEDULER] Fundamentals refresh: %d/%d tickers updated", done, len(rows))
        except Exception as e:
            logger.error("[SCHEDULER] Fundamentals refresh failed: %s", e)

    @staticmethod
    async def _run_youtube_prewarm(batch: int = 10):
        """Pre-warm youtube_transcripts for the stalest active-watchlist
        tickers so per-ticker precollect finds them already stored (the live
        fetch blows even the 90s budget on cold tickers). Pure scraper, no LLM.
        """
        try:
            from app.collectors.youtube_collector import collect_for_ticker
            from app.db.connection import get_db

            with get_db() as db:
                rows = db.execute(
                    """
                    SELECT w.ticker, MAX(y.published_at) AS newest
                    FROM watchlist w
                    LEFT JOIN youtube_transcripts y ON y.ticker = w.ticker
                    WHERE w.status = 'active'
                    GROUP BY w.ticker
                    ORDER BY newest ASC NULLS FIRST
                    LIMIT %s
                    """,
                    [batch],
                ).fetchall()
            total = 0
            for i, (ticker, _newest) in enumerate(rows):
                try:
                    stats = await collect_for_ticker(ticker, max_results=5)
                    total += stats.get("stored", 0) if isinstance(stats, dict) else 0
                except Exception as te:
                    logger.warning("[SCHEDULER] youtube prewarm %s failed: %s", ticker, te)
                # Deliberately slow: this runs in the background between
                # cycles, so pace the scrape (rate-limit avoidance) — the data
                # just needs to be warm by the time the next cycle starts.
                if i < len(rows) - 1:
                    await asyncio.sleep(45)
            logger.info(
                "[SCHEDULER] YouTube prewarm: %d transcripts stored over %d tickers",
                total, len(rows),
            )
        except Exception as e:
            logger.error("[SCHEDULER] YouTube prewarm failed: %s", e)

    @staticmethod
    async def _run_youtube_channel_sweep():
        """Nightly channel + search sweep (collect_all had NO caller — channel
        discovery never ran). Bounded to keep the scrape load modest."""
        try:
            from app.collectors.youtube_collector import collect_all

            total = await collect_all(max_videos=3, days_back=7, max_queries=10)
            logger.info("[SCHEDULER] YouTube channel sweep: %s transcripts stored", total)
        except Exception as e:
            logger.error("[SCHEDULER] YouTube channel sweep failed: %s", e)

    @staticmethod
    async def _run_pcr_collection():
        """Daily SPY put/call ratio snapshot (yfinance, no key needed)."""
        try:
            from app.collectors.pcr_collector import collect_all

            ok = await collect_all()
            logger.info("[SCHEDULER] PCR collection: %s", "stored" if ok else "no data")
        except Exception as e:
            logger.error("[SCHEDULER] PCR collection failed: %s", e)

    @staticmethod
    async def _run_insider_collection():
        """Openinsider cluster-buy sweep → insider_trades."""
        try:
            from app.collectors.openinsider_collector import collect_all

            result = await collect_all()
            logger.info("[SCHEDULER] Insider cluster-buy collection: %s", result)
        except Exception as e:
            logger.error("[SCHEDULER] Insider collection failed: %s", e)

    @staticmethod
    async def _run_economic_calendar_collection():
        """TradingEconomics calendar scrape → economic_calendar (read by
        the get_upcoming_events tool's macro section)."""
        try:
            from app.collectors.tradingeconomics_collector import collect_all

            result = await collect_all()
            logger.info("[SCHEDULER] Economic calendar collection: %s", result)
        except Exception as e:
            logger.error("[SCHEDULER] Economic calendar collection failed: %s", e)

    @staticmethod
    async def _run_social_collection():
        """Social sentiment sweep → social_posts (twitter/fintwit via
        scraper-service, plus StockTwits for active watchlist tickers)."""
        try:
            from app.collectors.twitter_collector import collect_all as collect_twitter

            n = await collect_twitter()
            logger.info("[SCHEDULER] Twitter/fintwit sweep: %s posts", n)
        except Exception as e:
            logger.error("[SCHEDULER] Twitter sweep failed: %s", e)
        try:
            from app.collectors.stocktwits_collector import collect_for_ticker
            from app.db.connection import get_db

            with get_db() as db:
                rows = db.execute(
                    "SELECT ticker FROM watchlist WHERE status = 'active' LIMIT 15"
                ).fetchall()
            total = 0
            for (ticker,) in rows:
                total += await collect_for_ticker(ticker, limit=20) or 0
            logger.info(
                "[SCHEDULER] StockTwits sweep: %s posts over %d tickers", total, len(rows)
            )
        except Exception as e:
            logger.error("[SCHEDULER] StockTwits sweep failed: %s", e)

    @staticmethod
    async def _run_smart_money_returns():
        """Recompute real alpha for congress + fund disclosures."""
        try:
            from app.analytics.returns_engine import compute_all

            stats = await asyncio.to_thread(compute_all)
            logger.info("[SCHEDULER] Smart-money returns recomputed: %s", stats)
        except Exception as e:
            logger.error("[SCHEDULER] Smart-money returns recompute failed: %s", e)

        try:
            await SchedulerService._inject_smart_money_leads()
        except Exception as e:
            logger.error("[SCHEDULER] Smart-money lead injection failed: %s", e)

    @staticmethod
    async def _inject_smart_money_leads(days: int = 120, min_buyers: int = 3, limit: int = 25):
        """Feed consensus smart-money buys into the discovered_tickers inbox.

        13F conviction already reached ticker selection via pipeline_service's
        Phase 4C, but CONGRESS did not — despite being by far the deeper dataset
        (30k disclosures vs 8k holdings rows). Both cohorts now land here, each
        under its own source label so downstream scoring can weight them apart.
        """
        from app.db.connection import get_db as _get_db

        def _work() -> int:
            with _get_db() as db:
                rows = db.execute(
                    """
                    SELECT s.ticker,
                           s.actor_type,
                           COUNT(DISTINCT s.actor_id) AS buyers,
                           COUNT(DISTINCT s.actor_id) FILTER (
                               WHERE p.rankable AND p.avg_alpha > 0
                           ) AS proven_buyers,
                           MAX(s.event_date) AS latest
                    FROM smart_money_trade_scores s
                    LEFT JOIN smart_money_performance p
                      ON p.actor_type = s.actor_type
                     AND p.actor_id   = s.actor_id
                     AND p.horizon    = '1y'
                    WHERE s.direction = 'buy'
                      AND s.event_date >= CURRENT_DATE - MAKE_INTERVAL(days => %s)
                    GROUP BY s.ticker, s.actor_type
                    HAVING COUNT(DISTINCT s.actor_id) >= %s
                    ORDER BY proven_buyers DESC, buyers DESC
                    LIMIT %s
                    """,
                    (days, min_buyers, limit),
                ).fetchall()

                written = 0
                for ticker, actor_type, buyers, proven, latest in rows:
                    source = "congress" if actor_type == "congress" else "institutional"
                    # Score leans on buyers WITH a proven track record — consensus
                    # among actors who actually beat SPY is a stronger lead than
                    # consensus among actors we cannot score at all.
                    score = float(buyers) + (float(proven or 0) * 2.0)
                    context = (
                        f"{buyers} distinct {actor_type} buyers "
                        f"({proven or 0} with positive proven 1y alpha); "
                        f"latest disclosure {latest}"
                    )
                    db.execute(
                        """
                        INSERT INTO discovered_tickers (ticker, source, score, context, discovered_at)
                        VALUES (%s, %s, %s, %s, CURRENT_TIMESTAMP)
                        ON CONFLICT (ticker, source) DO UPDATE SET
                            score = EXCLUDED.score,
                            context = EXCLUDED.context,
                            discovered_at = CURRENT_TIMESTAMP
                        """,
                        (ticker, source, score, context),
                    )
                    written += 1
                return written

        count = await asyncio.to_thread(_work)
        logger.info("[SCHEDULER] Smart-money leads injected: %s tickers", count)

    @staticmethod
    async def _run_market_open_cycle():
        """Kick off a full trading cycle at market open (6:30 AM PT / 9:30 ET).

        Enqueues a START_CYCLE command onto v3_system_commands — the same queue
        cycle_main polls — so this reuses the normal cycle dispatch path. Gated
        on pause/stop state, market holidays, and an already-running cycle.
        """
        if cycle_control.is_paused or cycle_control.is_stopped:
            logger.info("[SCHEDULER] Skipping market-open cycle: system is PAUSED/STOPPED.")
            return

        # The cron only fires on weekdays; this additionally skips market holidays.
        state = MarketCalendar.get_market_state()
        if state in ("holiday", "closed"):
            logger.info("[SCHEDULER] Skipping market-open cycle: market state=%s.", state)
            return

        try:
            with get_db() as db:
                state_row = db.execute(
                    "SELECT status FROM pipeline_state WHERE singleton_id = 'current'"
                ).fetchone()
                if state_row and state_row[0] not in ("idle", "done", "error", "stopped", "interrupted"):
                    logger.info(
                        "[SCHEDULER] Market-open cycle skipped: a cycle is already running (%s).",
                        state_row[0],
                    )
                    return

                payload = {
                    "tickers": [],
                    "collect": True,
                    "analyze": True,
                    "trade": True,
                    "dynamic_selection_mode": True,
                }
                cmd_id = f"sch-open-{uuid.uuid4().hex[:8]}"
                db.execute(
                    "INSERT INTO v3_system_commands (id, command_type, payload) VALUES (%s, %s, %s)",
                    [cmd_id, "START_CYCLE", json.dumps(payload)],
                )
            logger.info("[SCHEDULER] Market-open trading cycle enqueued (START_CYCLE %s).", cmd_id)
        except Exception as e:
            logger.error("[SCHEDULER] Failed to enqueue market-open cycle: %s", e)

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
    async def _run_watchdesk_evaluation():
        """Evaluate agent-defined watch conditions (cheap, no LLM) and wake the
        agent only when a trigger trips."""
        if cycle_control.is_paused or cycle_control.is_stopped:
            return
        try:
            from app.services.watch_desk import evaluate_watches
            await evaluate_watches()
        except Exception as e:
            logger.error("[SCHEDULER] Watch Desk evaluation failed: %s", e)

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
