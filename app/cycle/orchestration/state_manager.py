"""Pipeline state persistence via PostgreSQL."""

import json
import logging
import os
import threading
import time
from datetime import datetime, timezone

from app.db import connection

logger = logging.getLogger(__name__)


def _stringify_timestamp(value):
    if not value:
        return None
    if isinstance(value, str):
        if not value.endswith("Z") and "+" not in value:
            return value + "Z"
        return value
    if hasattr(value, "tzinfo") and value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.isoformat() if hasattr(value, "isoformat") else value


_OPERATIONAL_PHASES = {
    "created",
    "queued",
    "started",
    "collecting",
    "analyzing",
    "gated",
    "traded",
    "persisted",
    "evaluated",
    "closed",
    "done",
    "error",
    "stopped",
}


class PipelineStateDB:
    SINGLETON_ID = "current"

    @classmethod
    def get_state(cls, summary_only: bool = False) -> dict:
        """Read full cycle state from PostgreSQL."""
        try:
            with connection.get_db() as db:
                row = db.execute(
                    "SELECT * FROM pipeline_state WHERE singleton_id = %s",
                    [cls.SINGLETON_ID],
                ).fetchone()

                if row:
                    cols = [desc[0] for desc in db.description]
                    d = dict(zip(cols, row))

                    # Parse JSON cols
                    for jcol in ("tickers",):
                        try:
                            parsed = d.get(jcol)
                            if isinstance(parsed, str):
                                d[jcol] = json.loads(parsed)
                            elif parsed is None:
                                d[jcol] = []
                        except Exception:
                            d[jcol] = []

                    # Stringify timestamps for API compatibility
                    for tcol in ("started_at", "finished_at", "updated_at"):
                        if d.get(tcol):
                            d[tcol] = _stringify_timestamp(d[tcol])

                    d.pop("singleton_id", None)

                    if summary_only:
                        d["events"] = []
                        d["results"] = []
                        return d

                    # Fetch relational events mapped for UI
                    if d.get("cycle_id"):
                        ev_rows = db.execute(
                            "SELECT timestamp as ts, phase, step, detail, status, data_json, elapsed_ms "
                            "FROM pipeline_events WHERE cycle_id = %s ORDER BY timestamp ASC",
                            [d["cycle_id"]],
                        ).fetchall()
                        events = []
                        for erow in ev_rows:
                            ts = _stringify_timestamp(erow[0])
                            events.append(
                                {
                                    "ts": ts,
                                    "phase": erow[1],
                                    "step": erow[2],
                                    "detail": erow[3],
                                    "status": erow[4],
                                    "data": json.loads(erow[5]) if erow[5] else {},
                                    "elapsed_ms": erow[6] or 0,
                                }
                            )
                        d["events"] = events
                    else:
                        d["events"] = []

                    # Fetch relational results map
                    if d.get("cycle_id"):
                        ar_rows = db.execute(
                            "SELECT ticker, result_json FROM analysis_results WHERE cycle_id = %s",
                            [d["cycle_id"]],
                        ).fetchall()
                        results = []
                        for ar in ar_rows:
                            try:
                                res = json.loads(ar[1])
                                if "ticker" not in res:
                                    res["ticker"] = ar[0]
                                results.append(res)
                            except Exception:
                                pass
                        d["results"] = results
                    else:
                        d["results"] = []

                    return d
        except Exception as e:
            logger.error("[PipelineStateDB] Failed to read state: %s", e)

        return cls.default_state()

    @classmethod
    def save_state(cls, state: dict):
        """Write core state scalar values to PostgreSQL."""
        try:
            with connection.get_db() as db:
                tickers_str = json.dumps(state.get("tickers", []))

                started_at = state.get("started_at")
                finished_at = state.get("finished_at")

                db.execute(
                    """
                    INSERT INTO pipeline_state (
                        singleton_id, status, cycle_id, started_at, finished_at,
                        requested_pipeline_version, effective_pipeline_version,
                        benchmark_group, execution_mode, v2_stage,
                        tickers, progress, error, phase,
                        operational_phase, step_count, total_steps,
                        collect_flag, analyze_flag, trade_flag,
                        max_tickers, discovered_tickers, dynamic_selection_mode,
                        updated_at
                    ) VALUES (
                        %s, %s, %s, %s, %s,
                        %s, %s, %s, %s, %s,
                        %s::jsonb, %s, %s, %s,
                        %s, %s, %s,
                        %s, %s, %s,
                        %s, %s, %s,
                        CURRENT_TIMESTAMP
                    )
                ON CONFLICT (singleton_id) DO UPDATE SET
                    status = EXCLUDED.status,
                    cycle_id = EXCLUDED.cycle_id,
                    started_at = EXCLUDED.started_at,
                    finished_at = EXCLUDED.finished_at,
                    requested_pipeline_version = EXCLUDED.requested_pipeline_version,
                    effective_pipeline_version = EXCLUDED.effective_pipeline_version,
                    benchmark_group = EXCLUDED.benchmark_group,
                    execution_mode = EXCLUDED.execution_mode,
                    v2_stage = EXCLUDED.v2_stage,
                    tickers = EXCLUDED.tickers,
                    progress = EXCLUDED.progress,
                    error = EXCLUDED.error,
                    phase = EXCLUDED.phase,
                    operational_phase = EXCLUDED.operational_phase,
                    step_count = EXCLUDED.step_count,
                    total_steps = EXCLUDED.total_steps,
                    collect_flag = EXCLUDED.collect_flag,
                    analyze_flag = EXCLUDED.analyze_flag,
                    trade_flag = EXCLUDED.trade_flag,
                    max_tickers = EXCLUDED.max_tickers,
                    discovered_tickers = EXCLUDED.discovered_tickers,
                    dynamic_selection_mode = EXCLUDED.dynamic_selection_mode,
                    updated_at = CURRENT_TIMESTAMP
                """,
                    [
                        cls.SINGLETON_ID,
                        state.get("status", "idle"),
                        state.get("cycle_id"),
                        started_at,
                        finished_at,
                        state.get("requested_pipeline_version", "v2"),
                        state.get("effective_pipeline_version", "v2"),
                        state.get("benchmark_group", "baseline"),
                        state.get("execution_mode", "production"),
                        state.get("v2_stage", 0),
                        tickers_str,
                        state.get("progress", ""),
                        state.get("error"),
                        state.get("phase", ""),
                        state.get("operational_phase", ""),
                        None, # step_count (removed logic)
                        None, # total_steps (removed logic)
                        state.get("collect_flag", True),
                        state.get("analyze_flag", True),
                        state.get("trade_flag", False),
                        state.get("max_tickers"),
                        state.get("discovered_tickers"),
                        state.get("dynamic_selection_mode", False),
                    ],
                )
        except Exception as e:
            logger.error("[PipelineStateDB] Failed to save DB core state: %s", e)

    @classmethod
    def append_event(cls, cycle_id: str, event: dict):
        cls.append_events(cycle_id, [event])

    @classmethod
    def append_events(cls, cycle_id: str, events: list[dict]):
        """Append multiple real-time events directly to PostgreSQL."""
        try:
            if not cycle_id or not events:
                return
            with connection.get_db() as db:
                import uuid

                rows = [
                    (
                        f"evt_{uuid.uuid4().hex[:8]}",
                        cycle_id,
                        e.get("ts"),
                        e.get("phase"),
                        e.get("step"),
                        e.get("detail"),
                        e.get("status", "ok"),
                        json.dumps(e.get("data", {})),
                        e.get("elapsed_ms", 0),
                    )
                    for e in events
                ]
                db.executemany(
                    """
                    INSERT INTO pipeline_events 
                    (id, cycle_id, timestamp, phase, step, detail, status, data_json, elapsed_ms) 
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s)
                    """,
                    rows,
                )
        except Exception as e:
            logger.error("[PipelineStateDB] Failed to append SQL events: %s", e)

    @classmethod
    def get_cycle_events(cls, cycle_id: str | None) -> list[dict]:
        """Fetch persisted events for a cycle without loading the full state."""
        if not cycle_id:
            return []
        try:
            with connection.get_db() as db:
                rows = db.execute(
                    "SELECT timestamp as ts, phase, step, detail, status, data_json, elapsed_ms "
                    "FROM pipeline_events WHERE cycle_id = %s ORDER BY timestamp ASC",
                    [cycle_id],
                ).fetchall()
                events = []
                for row in rows:
                    ts = _stringify_timestamp(row[0])
                    events.append(
                        {
                            "ts": ts,
                            "phase": row[1],
                            "step": row[2],
                            "detail": row[3],
                            "status": row[4],
                            "data": json.loads(row[5]) if row[5] else {},
                            "elapsed_ms": row[6] or 0,
                        }
                    )
                return events
        except Exception as e:
            logger.error("[PipelineStateDB] Failed to fetch events: %s", e)
            return []

    @classmethod
    def log_execution_error(
        cls,
        cycle_id: str,
        phase: str,
        ticker: str,
        error_type: str,
        error_message: str,
        stack_trace: str,
    ):
        """Log a pipeline execution error to the database for post-cycle reporting."""
        try:
            with connection.get_db() as db:
                import uuid

                db.execute(
                    """
                    INSERT INTO execution_errors 
                    (id, cycle_id, phase, ticker, error_type, error_message, stack_trace) 
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                    """,
                    [
                        f"err_{uuid.uuid4().hex[:8]}",
                        cycle_id or "unknown",
                        phase or "unknown",
                        ticker or "system",
                        error_type,
                        error_message,
                        stack_trace,
                    ],
                )
        except Exception as e:
            logger.error("[PipelineStateDB] Failed to log execution error: %s", e)

    @classmethod
    def safe_log_execution_error(
        cls,
        cycle_id: str | None,
        phase: str | None,
        error_type: str,
        error: Exception | str,
        ticker: str = "system",
    ):
        """Safely log an execution error by handling string slicing and stack trace generation internally.
        Guaranteed not to raise an exception.
        """
        try:
            import traceback
            error_message = str(error)[:500]
            stack_trace = traceback.format_exc()[:2000]
            # If traceback has no active exception stack, use empty string
            if "NoneType: None" in stack_trace:
                stack_trace = ""
            cls.log_execution_error(
                cycle_id=cycle_id or "unknown",
                phase=phase or "unknown",
                ticker=ticker,
                error_type=error_type,
                error_message=error_message,
                stack_trace=stack_trace,
            )
        except Exception as e:
            logger.error("[PipelineStateDB] safe_log_execution_error failed: %s", e)

    @classmethod
    def default_state(cls) -> dict:
        return {
            "status": "idle",
            "cycle_id": None,
            "requested_pipeline_version": "v2",
            "effective_pipeline_version": "v2",
            "benchmark_group": "baseline",
            "execution_mode": "production",
            "v2_stage": 0,
            "started_at": None,
            "finished_at": None,
            "tickers": [],
            "progress": "",
            "results": [],
            "error": None,
            "events": [],
            "phase": "",
            "operational_phase": "",
            "step_count": None,
            "total_steps": None,
            "collect_flag": True,
            "analyze_flag": True,
            "trade_flag": False,
            "max_tickers": None,
            "discovered_tickers": None,
            "dynamic_selection_mode": False,
        }

    # ─── Cycle checkpoint methods (resume after crash) ───

    @classmethod
    def save_checkpoint(
        cls,
        cycle_id: str,
        completed_phases: list[str],
        completed_tickers: dict[str, list[str]],
        cycle_config: dict,
        original_started_at: str | None = None,
    ):
        """Persist a checkpoint so an interrupted cycle can be resumed."""
        try:
            with connection.get_db() as db:
                db.execute(
                    """
                    INSERT INTO cycle_resume_state (
                        cycle_id, status, completed_phases, completed_tickers,
                        cycle_config, checkpoint_ts, original_started_at
                    ) VALUES (%s, 'interrupted', %s::jsonb, %s::jsonb, %s::jsonb, CURRENT_TIMESTAMP, %s)
                    ON CONFLICT (cycle_id) DO UPDATE SET
                        completed_phases = EXCLUDED.completed_phases,
                        completed_tickers = EXCLUDED.completed_tickers,
                        cycle_config = EXCLUDED.cycle_config,
                        checkpoint_ts = CURRENT_TIMESTAMP
                    """,
                    [
                        cycle_id,
                        json.dumps(completed_phases),
                        json.dumps(completed_tickers),
                        json.dumps(cycle_config),
                        original_started_at,
                    ],
                )
                logger.info(
                    "[CHECKPOINT] Saved checkpoint for %s (phases: %s, tickers: %s)",
                    cycle_id,
                    completed_phases,
                    {k: len(v) for k, v in completed_tickers.items()},
                )
        except Exception as e:
            logger.error("[CHECKPOINT] Failed to save checkpoint: %s", e)
            raise

    @classmethod
    def get_checkpoint(cls, cycle_id: str | None = None) -> dict | None:
        """Retrieve the most recent checkpoint. If cycle_id is None, get any
        checkpoint with status='interrupted'."""
        try:
            with connection.get_db() as db:
                if cycle_id:
                    row = db.execute(
                        "SELECT cycle_id, status, completed_phases, completed_tickers, "
                        "cycle_config, checkpoint_ts, original_started_at "
                        "FROM cycle_resume_state WHERE cycle_id = %s AND status = 'interrupted'",
                        [cycle_id],
                    ).fetchone()
                else:
                    row = db.execute(
                        "SELECT cycle_id, status, completed_phases, completed_tickers, "
                        "cycle_config, checkpoint_ts, original_started_at "
                        "FROM cycle_resume_state WHERE status = 'interrupted' "
                        "ORDER BY checkpoint_ts DESC LIMIT 1"
                    ).fetchone()

                if not row:
                    return None

                checkpoint_ts = row[5]
                if checkpoint_ts:
                    checkpoint_ts = _stringify_timestamp(checkpoint_ts)

                original_started = row[6]
                if original_started:
                    original_started = _stringify_timestamp(original_started)

                return {
                    "cycle_id": row[0],
                    "status": row[1],
                    "completed_phases": json.loads(row[2])
                    if isinstance(row[2], str)
                    else (row[2] or []),
                    "completed_tickers": json.loads(row[3])
                    if isinstance(row[3], str)
                    else (row[3] or {}),
                    "cycle_config": json.loads(row[4])
                    if isinstance(row[4], str)
                    else (row[4] or {}),
                    "checkpoint_ts": checkpoint_ts,
                    "original_started_at": original_started,
                }
        except Exception as e:
            logger.error("[CHECKPOINT] Failed to read checkpoint: %s", e)
            return None

    @classmethod
    def clear_checkpoint(cls, cycle_id: str):
        """Delete a checkpoint after successful completion or user discard."""
        try:
            with connection.get_db() as db:
                db.execute(
                    "DELETE FROM cycle_resume_state WHERE cycle_id = %s", [cycle_id]
                )
                logger.info("[CHECKPOINT] Cleared checkpoint for %s", cycle_id)
        except Exception as e:
            logger.error("[CHECKPOINT] Failed to clear checkpoint: %s", e)

    @classmethod
    def expire_old_checkpoints(cls, max_age_hours: int = 6):
        """Mark checkpoints older than max_age_hours as expired so they
        aren't offered for resume."""
        try:
            from datetime import timedelta

            cutoff = datetime.now(timezone.utc) - timedelta(hours=max_age_hours)
            with connection.get_db() as db:
                db.execute(
                    "UPDATE cycle_resume_state SET status = 'expired' "
                    "WHERE status = 'interrupted' "
                    "AND checkpoint_ts < %s",
                    [cutoff],
                )
        except Exception as e:
            logger.error("[CHECKPOINT] Failed to expire old checkpoints: %s", e)


class PipelineStateMixin:
    _state = PipelineStateDB.default_state()
    _cycle_task = None
    _scout_task = None
    _consumer_task = None
    _checkpoint_task = None
    _macro_task = None
    _analysis_task = None
    _cycle_summary = {}

    _emit_events = []
    _emit_timer = None
    _emit_lock = threading.Lock()
    _cached_states = {}
    _cache_lock = threading.Lock()

    @classmethod
    def load_state(cls, summary_only: bool = False):
        """Load state from PostgreSQL to memory."""
        cls._state = PipelineStateDB.get_state(summary_only=summary_only)

    @classmethod
    def save_state(cls):
        """Save memory state to PostgreSQL."""
        PipelineStateDB.save_state(cls._state)

    @classmethod
    def get_current_state(cls, summary_only: bool = False) -> dict:
        now = time.time()
        with cls._cache_lock:
            cached = cls._cached_states.get(summary_only)
            if cached:
                cache_ts, cached_state = cached
                if now - cache_ts < 0.25:  # 250ms TTL
                    return cached_state

        # Use telemetry state as primary source
        cycle_id = cls._state.get("cycle_id")
        try:
            from app.telemetry.bus import get_cycle_state
            tel_state = get_cycle_state(cycle_id)
        except Exception:
            tel_state = None

        if tel_state and tel_state.cycle_id:
            state = {
                "status": tel_state.status,
                "cycle_id": tel_state.cycle_id,
                "phase": tel_state.phase,
                "progress": tel_state.progress,
                "tickers": tel_state.tickers,
                "results": tel_state.results,
                "events": tel_state.events if not summary_only else [],
                "started_at": tel_state.started_at,
                "finished_at": tel_state.finished_at,
                "error": cls._state.get("error"),
                "requested_pipeline_version": cls._state.get("requested_pipeline_version", "v2"),
                "effective_pipeline_version": cls._state.get("effective_pipeline_version", "v2"),
                "benchmark_group": cls._state.get("benchmark_group", "baseline"),
                "execution_mode": cls._state.get("execution_mode", "production"),
                "v2_stage": cls._state.get("v2_stage", 0),
                "collect_flag": cls._state.get("collect_flag", True),
                "analyze_flag": cls._state.get("analyze_flag", True),
                "trade_flag": cls._state.get("trade_flag", False),
                "max_tickers": cls._state.get("max_tickers"),
                "discovered_tickers": cls._state.get("discovered_tickers"),
                "dynamic_selection_mode": cls._state.get("dynamic_selection_mode", False),
            }
        else:
            state = PipelineStateDB.get_state(summary_only=summary_only)

        # ── Fix: Use in-memory cycle_id as authoritative source ──────────
        # The DB cycle_id can become stale due to emit timer race conditions
        # during container restart + cycle start. The in-memory _state is
        # updated atomically by start_cycle() and emit(), so it's always
        # correct. We override the DB cycle_id if the in-memory one exists.
        mem_cycle_id = cls._state.get("cycle_id")
        if mem_cycle_id and (mem_cycle_id != state.get("cycle_id") or cls._state.get("status") in ("done", "error", "stopped", "interrupted")):
            if mem_cycle_id != state.get("cycle_id"):
                logger.debug(
                    "[get_current_state] Correcting stale DB cycle_id %s → %s",
                    state.get("cycle_id"), mem_cycle_id,
                )
            state["cycle_id"] = mem_cycle_id

            # Also sync critical fields from in-memory state
            # NOTE: started_at and finished_at MUST be included here.
            # Without them the frontend gets a new cycle_id paired with
            # the old cycle's started_at, causing timer flicker.
            for key in ("status", "phase", "progress", "started_at", "finished_at", "max_tickers", "discovered_tickers", "dynamic_selection_mode"):
                mem_val = cls._state.get(key)
                if mem_val is not None:
                    state[key] = mem_val


            # Re-fetch events for the correct (in-memory) cycle_id.
            # The DB query above used the old cycle_id, so events would
            # be from the previous cycle — causing phantom data.
            if not summary_only:
                try:
                    corrected_events = PipelineStateDB.get_cycle_events(mem_cycle_id)
                    state["events"] = corrected_events or []
                except Exception:
                    state["events"] = []
        # ─────────────────────────────────────────────────────────────────

        try:
            from app.services.vllm_client import llm
            from app.monitoring.llm_tracker import tracker

            active_requests = 0
            queued_requests = 0
            per_box = {}
            for ep in llm._endpoints.values():
                # Use client-side state combined with container metrics for real-time responsiveness
                ep_active = max(ep.active_count, ep.requests_running)
                ep_queued = max(ep.queue.qsize() if ep.queue else 0, ep.requests_waiting)
                active_requests += ep_active
                queued_requests += ep_queued
                per_box[ep.name] = {
                    "active": ep_active,
                    "queued": ep_queued,
                    "max_concurrent": ep.max_concurrent,
                }

            # Per-endpoint TPS from recent call history
            tps_by_ep = tracker.get_recent_tps_by_endpoint(60)
            for ep_name, tps_val in tps_by_ep.items():
                if ep_name in per_box:
                    per_box[ep_name]["tps"] = tps_val

            state["llm_stats"] = {
                "active_requests": active_requests,
                "queued_requests": queued_requests,
                "tokens_per_second": tracker.get_recent_tps(60),
                "total_calls": tracker.total_calls,
                "total_tokens": tracker.total_tokens,
                "per_box": per_box,
            }
        except Exception as e:
            logger.debug(f"[get_current_state] Could not attach llm_stats: {e}")

        # Attach checkpoint info when cycle is interrupted
        if state.get("status") == "interrupted":
            try:
                checkpoint = PipelineStateDB.get_checkpoint(state.get("cycle_id"))
                if checkpoint:
                    state["checkpoint"] = {
                        "cycle_id": checkpoint["cycle_id"],
                        "completed_phases": checkpoint["completed_phases"],
                        "completed_tickers": checkpoint["completed_tickers"],
                        "checkpoint_ts": checkpoint["checkpoint_ts"],
                        "original_started_at": checkpoint["original_started_at"],
                    }
                else:
                    state["checkpoint"] = None
            except Exception as e:
                logger.warning("[PipelineStateDB] Failed to load checkpoint for interrupted state: %s", e)
                state["checkpoint"] = None

        state["step_count"] = None
        state["total_steps"] = None

        with cls._cache_lock:
            cls._cached_states[summary_only] = (now, state)

        return state

    @classmethod
    def reset_on_boot(cls):
        """Called once on server startup to handle zombie cycles.

        If a checkpoint exists for the interrupted cycle, set status to
        'interrupted' so the frontend can offer Resume / Start Fresh.
        Otherwise, force-reset to idle as before.

        Also checks for orphaned checkpoints when status is 'stopped'
        (e.g. server was shut down gracefully mid-cycle but the shutdown
        handler set 'stopped' before checkpoint logic existed).

        # BUG: partial-write not handled
        # If the DB was mid-write when the server crashed (e.g. partial cycle row in DB,
        # status='running' rows, orphaned ticker_analysis rows linked to a dead cycle,
        # or file locks), this method does NOT currently clean them up. This can lead to
        # dirty database state.
        """

        cls._cycle_task = None
        if cls._checkpoint_task:
            cls._checkpoint_task.cancel()
            cls._checkpoint_task = None

        # Expire any checkpoints older than 6 hours
        PipelineStateDB.expire_old_checkpoints(max_age_hours=6)

        # Reset any stuck 'running' commands in system_commands table
        try:
            with connection.get_db() as db:
                db.execute(
                    "UPDATE system_commands SET status = 'error', completed_at = CURRENT_TIMESTAMP, "
                    "error_message = 'Orphaned command - backend restarted mid-execution' "
                    "WHERE status = 'running'"
                )
                logger.info("[Boot] Cleaned up stuck running commands in system_commands")
        except Exception as e:
            logger.error("[Boot] Failed to clean up stuck running commands: %s", e)

        cls.load_state()
        prev_status = cls._state.get("status", "idle")

        # Statuses that might have a resumable checkpoint:
        #   - Active phases (collecting, analyzing, trading, starting, paused)
        #     → zombie cycle, process was killed
        #   - "stopped" → graceful shutdown set this, but checkpoint may exist
        #   - "interrupted" → already flagged from a prior boot, re-validate
        _terminal_no_checkpoint = ("idle", "done", "persisted")

        # ── Handle "error" and "stopped" states: reset to idle ──────────
        # Previously these were treated as terminal and left in place,
        # which caused the frontend to permanently show stale crashed-cycle
        # data. Now we reset to idle so the next cycle starts cleanly.
        if prev_status in ("error", "stopped"):
            stale_cycle_id = cls._state.get("cycle_id")
            logger.warning(
                "[CYCLE] Previous cycle ended with '%s' — resetting to idle on boot (cycle: %s)",
                prev_status,
                stale_cycle_id,
            )
            # Clean up stale pipeline_events for the crashed cycle so the
            # frontend doesn't show thousands of zombie timeout events.
            if stale_cycle_id:
                try:
                    with connection.get_db() as db:
                        deleted = db.execute(
                            "DELETE FROM pipeline_events WHERE cycle_id = %s RETURNING id",
                            [stale_cycle_id],
                        ).fetchall()
                        logger.info(
                            "[CYCLE] Cleaned up %d stale events for crashed cycle %s",
                            len(deleted), stale_cycle_id,
                        )
                except Exception as e:
                    logger.error("[CYCLE] Failed to clean up stale events: %s", e)

            cls._state = PipelineStateDB.default_state()
            cls._state["finished_at"] = datetime.now(timezone.utc).isoformat()
            cls.save_state()
            return

        if prev_status not in _terminal_no_checkpoint:
            zombie_cycle_id = cls._state.get("cycle_id")
            checkpoint = PipelineStateDB.get_checkpoint(zombie_cycle_id)

            if checkpoint:
                logger.warning(
                    "[CYCLE] Interrupted cycle detected (was '%s') — checkpoint found, "
                    "setting to 'interrupted' for possible resume (cycle: %s)",
                    prev_status,
                    zombie_cycle_id,
                )
                cls._state["status"] = "interrupted"
                cls._state["phase"] = "interrupted"
                cls._state["progress"] = (
                    f"Cycle {zombie_cycle_id} was interrupted. "
                    f"Completed phases: {', '.join(checkpoint['completed_phases']) or 'none'}. "
                    "Choose to resume or start fresh."
                )
                cls._state["finished_at"] = datetime.now(timezone.utc).isoformat()
                cls.save_state()
                return

            # No checkpoint — force-reset to idle
            if prev_status not in _terminal_no_checkpoint:
                zombie_cycle_id = cls._state.get("cycle_id")
                logger.warning(
                    "[CYCLE] Stale cycle detected (was '%s') — no checkpoint, "
                    "force-resetting to idle on boot",
                    prev_status,
                )
                if zombie_cycle_id:
                    try:
                        with connection.get_db() as db:
                            db.execute("DELETE FROM pipeline_events WHERE cycle_id = %s", [zombie_cycle_id])
                            db.execute("DELETE FROM analysis_results WHERE cycle_id = %s", [zombie_cycle_id])
                            db.execute("DELETE FROM debate_history WHERE cycle_id = %s", [zombie_cycle_id])
                            logger.info("[CYCLE] Cleaned up orphaned data for dead cycle %s", zombie_cycle_id)
                    except Exception as e:
                        logger.error("[CYCLE] Failed to clean up dead cycle data: %s", e)
                        
                cls._state = PipelineStateDB.default_state()
                cls._state["finished_at"] = datetime.now(timezone.utc).isoformat()
                cls.save_state()
                return

        # Also catch any truly anomalous status that slipped through
        if cls._state["status"] not in (
            "idle",
            "done",
            "error",
            "stopped",
            "interrupted",
        ):
            cls._state["status"] = "idle"
            cls._state["phase"] = ""
            cls._state["progress"] = ""
            cls._state["operational_phase"] = ""
            cls.save_state()

    @classmethod
    def emit(
        cls,
        phase: str,
        step: str,
        detail: str,
        status: str = "ok",
        data: dict | None = None,
        elapsed_ms: int = 0,
        data_type: str | None = None,
        room: str | None = None,
        **kwargs,
    ):
        data = data or {}
        if data_type:
            data["data_type"] = data_type
        if room:
            data["room"] = room

        if "data_type" not in data:
            STEP_TO_DATA_TYPE = {
                "yfinance": "price_data",
                "fundamental": "fundamental_data",
                "technical": "technical_data",
                "news": "news_data",
                "reddit": "reddit_data",
                "youtube": "youtube_data",
                "sentiment": "news_data",
                "llm": "llm_analysis",
                "synthesis": "synthesis",
                "debate": "debate",
                "consensus": "consensus",
                "trade": "trade_execution",
                "risk": "risk_check",
                "janitor": "cleanup",
                "purge": "cleanup",
            }
            inferred = next(
                (v for k, v in STEP_TO_DATA_TYPE.items() if k in (step or "").lower()),
                None
            )
            if inferred:
                data["data_type"] = inferred

        # Check if we should append to the last event in memory (for streaming chunks)
        appended = False
        with cls._emit_lock:
            if kwargs.get("append") and cls._emit_events:
                last_evt = cls._emit_events[-1]
                if (
                    last_evt["phase"] == phase
                    and last_evt["step"] == step
                    and last_evt["status"] == status
                ):
                    last_evt["detail"] += detail
                    if data:
                        last_evt["data"].update(data)
                    last_evt["elapsed_ms"] += elapsed_ms
                    appended = True
                    event = last_evt

        if not appended:
            event = {
                "ts": datetime.now(timezone.utc).isoformat(),
                "phase": phase,
                "step": step,
                "detail": detail,
                "status": status,
                "data": data,
                "elapsed_ms": elapsed_ms,
            }

        cls._state["phase"] = phase
        cls._state["progress"] = f"[{phase}] {step}: {event['detail']}"
        if phase in _OPERATIONAL_PHASES:
            cls._state["operational_phase"] = phase

        cid = cls._state.get("cycle_id") or "no-id"
        logger.info("[CYCLE %s] %s/%s: %s (%s)", cid, phase, step, detail, status)

        # Publish event to telemetry bus
        try:
            from app.telemetry.bus import publish_event
            from app.telemetry.schema import TelemetryEvent
            
            kind = "pipeline"
            if step == "heartbeat":
                kind = "heartbeat"
            elif phase == "analyzing" and (step.startswith("v2_start") or step.startswith("curator")):
                kind = "pipeline"
            
            publish_event(TelemetryEvent(
                ts=event["ts"],
                cycle_id=cid,
                ticker=data.get("ticker", "") if data else "",
                kind=kind,
                source="cycle_runner",
                status=status,
                step=step,
                detail=detail,
                phase=phase,
                elapsed_ms=elapsed_ms,
                data=data or {}
            ))
        except Exception as tel_e:
            logger.debug("[PipelineStateMixin] Failed to publish telemetry event: %s", tel_e)

        if not appended:
            with cls._emit_lock:
                cls._emit_events.append(event)
                if cls._emit_timer is None:

                    def flush():
                        with cls._emit_lock:
                            events_to_flush = list(cls._emit_events)
                            cls._emit_events.clear()
                            cls._emit_timer = None

                        if not events_to_flush:
                            return

                        try:
                            current_state = dict(cls._state)
                            PipelineStateDB.save_state(current_state)
                            PipelineStateDB.append_events(cid, events_to_flush)
                        except Exception as e:
                            logger.error("[PipelineStateMixin] emit flush failed: %s", e)

                    cls._emit_timer = threading.Timer(0.1, flush)
                    cls._emit_timer.start()

    @classmethod
    def flush_events(cls):
        """Synchronously flush any pending events to the database."""
        with cls._emit_lock:
            if cls._emit_timer:
                cls._emit_timer.cancel()
                cls._emit_timer = None
            events_to_flush = list(cls._emit_events)
            cls._emit_events.clear()

        if not events_to_flush:
            return

        cid = cls._state.get("cycle_id") or "no-id"
        try:
            PipelineStateDB.save_state(dict(cls._state))
            PipelineStateDB.append_events(cid, events_to_flush)
        except Exception as e:
            logger.error("[PipelineStateMixin] flush_events failed: %s", e)

