import time
import asyncio
import logging
import uuid
import json
from datetime import datetime, timezone
from app.cycle.orchestration.state_manager import PipelineStateDB
from app.cycle.core import PipelineContext
from app.config import settings
from app.db.connection import get_db
from app.cognition.orchestration import resolve_cycle_runtime

logger = logging.getLogger(__name__)

# ── Terminal states where no cycle is active ──
_TERMINAL_STATES = ("idle", "done", "error", "stopped", "cancelled", "interrupted")


class TaskRegistry:
    """Lightweight task registry for deterministic cleanup.

    All asyncio.Task objects spawned by the lifecycle controller are
    registered here instead of bare class attributes. This provides:
    - Single cancel_all() for stop/cleanup
    - is_active() guard for deduplication
    - Deterministic ordering of cancellation
    """

    def __init__(self):
        self._tasks: dict[str, asyncio.Task] = {}

    def register(self, name: str, task: asyncio.Task) -> None:
        """Register a task. If one already exists with that name, it is NOT cancelled."""
        self._tasks[name] = task

    async def cancel_and_await(self, name: str, timeout: float = 2.0) -> None:
        """Cancel a specific task and wait for it to finish."""
        task = self._tasks.pop(name, None)
        if task and not task.done():
            task.cancel()
            try:
                await asyncio.wait_for(task, timeout=timeout)
            except (asyncio.CancelledError, asyncio.TimeoutError, Exception):
                pass

    def is_active(self, name: str) -> bool:
        """Return True if the named task exists and is still running."""
        task = self._tasks.get(name)
        return task is not None and not task.done()

    async def cancel_all(self, timeout: float = 2.0) -> int:
        """Cancel all registered tasks and wait for them. Returns count cancelled."""
        count = 0
        names = list(self._tasks.keys())
        for name in names:
            task = self._tasks.pop(name, None)
            if task and not task.done():
                task.cancel()
                count += 1
        # Wait for all cancelled tasks together
        tasks_to_wait = [t for t in [self._tasks.get(n) for n in names] if t and not t.done()]
        if tasks_to_wait:
            await asyncio.gather(*tasks_to_wait, return_exceptions=True)
        return count

    def get(self, name: str) -> asyncio.Task | None:
        """Get a task by name."""
        return self._tasks.get(name)

    def clear(self) -> None:
        """Clear all references without cancelling."""
        self._tasks.clear()


# Module-level singleton — shared across all mixin users
_task_registry = TaskRegistry()


class LifecycleControllerMixin:
    _action_lock = None

    @classmethod
    def _get_lock(cls):
        if cls._action_lock is None:
            cls._action_lock = asyncio.Lock()
        return cls._action_lock

    @classmethod
    async def start_cycle(
        cls,
        tickers: list[str],
        collect: bool = True,
        analyze: bool = True,
        trade: bool = True,  # Paper trading ALWAYS armed as per user directive.
        trigger_type: str = "manual",
        schedule_id: str | None = None,
        max_tickers: int | None = None,
        discovered_tickers: int | None = None,
        pipeline_version: str | None = None,
        benchmark_group: str | None = None,
        start_fresh: bool = False,
    ):
        async with cls._get_lock():
            # Check memory state first to avoid race conditions with DB read lag
            current_status = cls._state.get("status", "idle")
            if current_status not in _TERMINAL_STATES:
                raise ValueError(f"Cycle already running: {current_status}")

            # Task slot guard: reject if asyncio task is still alive
            if _task_registry.is_active("cycle"):
                raise ValueError("Cycle task already running — rejecting duplicate START_CYCLE")

            cls.load_state()
            if cls._state["status"] not in _TERMINAL_STATES:
                raise ValueError(f"Cycle already running: {cls._state['status']}")
            # Auto-resume check if not starting fresh and not an edge case response
            if not start_fresh and not trigger_type.startswith("edge_case_"):
                # Clean up expired checkpoints (> 6 hours old)
                PipelineStateDB.expire_old_checkpoints(max_age_hours=6)
                checkpoint = PipelineStateDB.get_checkpoint()
                if checkpoint:
                    logger.info(
                        "[CYCLE] Auto-resuming recent interrupted cycle %s",
                        checkpoint["cycle_id"]
                    )
                    cls._state["status"] = "interrupted"
                    cls._state["cycle_id"] = checkpoint["cycle_id"]
                    cls.save_state()
                    return await cls.resume_interrupted_cycle()

            cycle_id = f"cycle-{int(time.time())}"

            cls._state.update(
                {
                    "status": "starting",
                    "cycle_id": cycle_id,
                    "progress": f"Initializing cycle {cycle_id}...",
                    "error": None,
                    "phase": "starting",
                    "started_at": datetime.now(timezone.utc).isoformat(),
                    "finished_at": None,
                }
            )
            cls.save_state()

        logger.info(
            "[CYCLE] Start requested. Initializing in background for instant UI response."
        )

        loop = asyncio.get_running_loop()
        loop.create_task(
            cls._background_start_cycle(
                tickers=tickers,
                collect=collect,
                analyze=analyze,
                trade=trade,
                trigger_type=trigger_type,
                schedule_id=schedule_id,
                max_tickers=max_tickers,
                discovered_tickers=discovered_tickers,
                pipeline_version=pipeline_version,
                benchmark_group=benchmark_group,
                cycle_id=cycle_id,
            )
        )

        # Watchdog: if status is still 'starting' after 180s, force error
        loop.create_task(cls._starting_state_watchdog(cycle_id, timeout_s=180))

        return {
            "status": "starting",
            "cycle_id": cycle_id,
            "message": "Cycle initialization started in background",
        }

    @classmethod
    async def _background_start_cycle(
        cls,
        tickers: list[str],
        collect: bool,
        analyze: bool,
        trade: bool,
        trigger_type: str,
        schedule_id: str | None,
        max_tickers: int | None,
        discovered_tickers: int | None,
        pipeline_version: str | None,
        benchmark_group: str | None,
        cycle_id: str,
    ):
        try:
            # If starting fresh from an interrupted state, clear the old checkpoint
            if cls._state.get("status") == "interrupted":
                old_cycle_id = cls._state.get("cycle_id")
                if old_cycle_id and old_cycle_id != cycle_id:
                    PipelineStateDB.clear_checkpoint(old_cycle_id)
                    logger.info(
                        "[CYCLE] Cleared old checkpoint for %s (user chose Start Fresh)",
                        old_cycle_id,
                    )

            trade = True  # Enforced: 100% armed, always paper trading.
            route = resolve_cycle_runtime(
                requested_version=pipeline_version,
                benchmark_group=benchmark_group,
            )

            # Compute hard total cap: max_tickers from UI > MAX_CYCLE_TICKERS > MAX_ANALYSIS_TICKERS
            dynamic_selection_mode = (max_tickers == 0 and discovered_tickers == 0)
            if dynamic_selection_mode:
                cap = 10000  # Large enough to fetch all candidate tickers
            elif max_tickers is not None and max_tickers > 0:
                cap = max_tickers
            elif settings.MAX_CYCLE_TICKERS > 0:
                cap = settings.MAX_CYCLE_TICKERS
            else:
                cap = settings.MAX_ANALYSIS_TICKERS  # fallback: 30

            logger.info("[CYCLE] Hard cap on TOTAL tickers: %d", cap)

            # ── Phase 0: News-Driven Ticker Discovery ──
            # Scan news/reddit/congress data already in DB and use LLM to
            # extract fresh ticker opportunities BEFORE the selector runs.
            try:
                from app.pipeline.analysis.news_discovery import run_news_discovery
                phase0_discovered = await run_news_discovery(emit=cls.emit)
                if phase0_discovered:
                    logger.info(
                        "[CYCLE] Phase 0 discovered %d new tickers: %s",
                        len(phase0_discovered), ", ".join(phase0_discovered),
                    )
                    cls.emit(
                        "starting", "phase0_done",
                        f"Phase 0: Discovered {len(phase0_discovered)} new tickers from today's news",
                        status="ok",
                        data={"discovered": phase0_discovered},
                    )
                else:
                    logger.info("[CYCLE] Phase 0: No new tickers discovered from news data")
            except Exception as disc_err:
                logger.warning("[CYCLE] Phase 0 discovery failed (non-fatal): %s", disc_err)

            from app.pipeline.ticker_selector import TickerSelector, TickerSelectionResult

            if trigger_type == "smoke_test":
                logger.info("[CYCLE] Smoke test trigger detected — bypassing TickerSelector to run exactly: %s", tickers)
                selection = TickerSelectionResult(non_position_tickers=tickers)
            else:
                selection = TickerSelector.select_tickers_for_cycle_v2(tickers, cap, discovered_tickers=discovered_tickers)
            tickers = selection.all_tickers

            if not tickers:
                logger.info(
                    "[CYCLE] No tickers available — running in discovery-only mode"
                )

            n_pos = len(selection.position_tickers)
            n_extra = len(selection.non_position_tickers)
            if n_pos:
                logger.info(
                    "[CYCLE] Portfolio positions (count against cap): %d — %s",
                    n_pos,
                    ", ".join(selection.position_tickers),
                )
            logger.info(
                "[CYCLE] Non-position tickers: %d (remaining slots from cap %d - %d positions)",
                n_extra,
                cap,
                n_pos,
            )

            from app.cycle.orchestration.cycle_control import cycle_control

            cycle_control.reset()

            # Reset the LLM kill switch so requests can flow for the new cycle.
            # Must happen AFTER cycle_control.reset() to prevent race conditions
            # where the dispatch loop sees is_stopped=False but _killed=True.
            try:
                from app.services.vllm_client import llm
                llm.reset_kill_switch()
                # Begin new Prism cycle generation — clears stale sessions and
                # increments the generation ID so late-arriving responses from
                # a previous cycle can be detected and discarded.
                if hasattr(llm, "prism_client") and llm.prism_client:
                    llm.prism_client.begin_cycle()
            except Exception as e:
                logger.warning("[CYCLE] Failed to reset LLM kill switch (non-fatal): %s", e)

            try:
                from app.services.memory.working_memory import working_memory
                from app.services.session_profile import profile_memory

                working_memory.clear()

                # Load persistent last cycle context into working memory
                last_context = profile_memory.get_last_trade_context()
                if last_context:
                    working_memory.add_event(
                        content=str(last_context),
                        source="last_trade_context",
                        ticker="global",
                    )
                    logger.info("[MEMORY] Loaded last trade context from disk profile")

                logger.debug("[MEMORY] Cleared working memory for new cycle")
            except ImportError:
                pass

            cls._state.update(
                {
                    "requested_pipeline_version": route["requested_version"],
                    "effective_pipeline_version": route["effective_version"],
                    "benchmark_group": route["benchmark_group"],
                    "execution_mode": route["execution_mode"],
                    "v2_stage": route["v2_stage"],
                    "tickers": tickers,
                    "position_tickers": selection.position_tickers,
                    "non_position_tickers": selection.non_position_tickers,
                    "progress": f"Starting cycle {cycle_id} for {len(tickers)} tickers ({n_pos} portfolio + {n_extra} new)",
                    "step_count": None,
                    "total_steps": None,
                    "collect_flag": collect,
                    "analyze_flag": analyze,
                    "trade_flag": trade,
                    "max_tickers": max_tickers,
                    "discovered_tickers": discovered_tickers,
                    "dynamic_selection_mode": dynamic_selection_mode,
                }
            )
            cls.save_state()

            logger.info("=" * 70)
            logger.info(
                "  CYCLE %s STARTED — %d tickers (%d portfolio + %d new)",
                cycle_id,
                len(tickers),
                n_pos,
                n_extra,
            )
            logger.info("=" * 70)

            ticker_msg = (
                f"{len(tickers)} tickers ({n_pos} portfolio + {n_extra} new)"
                if tickers
                else "Discovery-only mode"
            )
            cls.emit(
                "starting",
                "init",
                f"Cycle started: {ticker_msg}",
                data={
                    "tickers": tickers,
                    "position_tickers": selection.position_tickers,
                    "non_position_tickers": selection.non_position_tickers,
                    "collect": collect,
                    "analyze": analyze,
                    "trade": trade,
                    "requested_version": route["requested_version"],
                    "effective_version": route["effective_version"],
                    "benchmark_group": route["benchmark_group"],
                    "execution_mode": route["execution_mode"],
                    "v2_stage": route["v2_stage"],
                },
            )

            ctx = PipelineContext(
                tickers=tickers,
                collect=collect,
                analyze=analyze,
                trade=trade,
                cycle_id=cycle_id,
                trigger_type=trigger_type,
                schedule_id=schedule_id,
                max_tickers=0 if dynamic_selection_mode else cap,
                discovered_tickers=discovered_tickers,
                dynamic_selection_mode=dynamic_selection_mode,
            )

            cls._cycle_task = asyncio.create_task(cls._run_cycle(ctx))
            _task_registry.register("cycle", cls._cycle_task)

            cls._checkpoint_task = asyncio.create_task(
                cls._checkpoint_heartbeat(cycle_id)
            )
            _task_registry.register("checkpoint", cls._checkpoint_task)

        except Exception as e:
            logger.error("[CYCLE] Failed to initialize cycle in background: %s", e)
            cls._state.update(
                {
                    "status": "error",
                    "progress": f"Failed to initialize cycle: {e}",
                    "error": str(e),
                    "finished_at": datetime.now(timezone.utc).isoformat(),
                }
            )
            cls.save_state()
            cls.emit(
                "error",
                "init_error",
                f"Failed to initialize cycle: {e}",
                status="error",
            )

    @classmethod
    async def _starting_state_watchdog(cls, cycle_id: str, timeout_s: int = 180):
        """Watchdog: if status is still 'starting' after timeout, force error.

        Prevents the UI from hanging indefinitely when _background_start_cycle
        fails silently or takes too long.
        """
        try:
            await asyncio.sleep(timeout_s)
            current_status = cls._state.get("status")
            current_cycle = cls._state.get("cycle_id")
            if current_status == "starting" and current_cycle == cycle_id:
                logger.error(
                    "[CYCLE] WATCHDOG: status still 'starting' after %ds for cycle %s. "
                    "Forcing error state.",
                    timeout_s,
                    cycle_id,
                )
                cls._state.update(
                    {
                        "status": "error",
                        "error": f"Worker did not acknowledge START_CYCLE within {timeout_s}s",
                        "finished_at": datetime.now(timezone.utc).isoformat(),
                    }
                )
                cls.save_state()
                cls.emit(
                    "error",
                    "watchdog_timeout",
                    f"Cycle startup timed out after {timeout_s}s",
                    status="error",
                )
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.debug("[CYCLE] Watchdog error (non-fatal): %s", e)

    @classmethod
    async def stop_cycle(cls, _stop_t1: float | None = None) -> dict:
        """Deterministic 9-step shutdown sequence.

        Steps:
          1. Set pipeline_state = stopping in DB
          2. Call cycle_control.stop() — sets stop flag, unblocks paused tasks
          3. Call llm.abort_active_requests() — kill switch + TCP kill
          4. Cancel and await all registered tasks
          5. Cancel and await the main cycle task
          6. Call cycle_control.stop_and_drain() — flush pending work
          7. Call prism_client.cleanup_all_sessions()
          8. Clear the task registry
          9. Set pipeline_state to cancelled/interrupted in DB and emit SSE

        _stop_t1: optional monotonic timestamp from the poller (T1)
        """
        from app.cycle.orchestration.cycle_control import cycle_control
        from app.services.vllm_client import llm

        _t_base = _stop_t1 or time.monotonic()

        # ── Step 1: Set stopping state ──
        cls._state["status"] = "stopping"
        cls._state["progress"] = "Stopping cycle..."
        cls.save_state()

        # ── Step 2: Signal stop via cycle_control ──
        T2 = time.monotonic()
        cycle_control.stop()
        logger.info("[STOP_TRACE] T2: cycle_control.stop() called (Δ=%.3fs from T1)", T2 - _t_base)

        # ── Step 3: Kill LLM requests ──
        await llm.abort_active_requests()
        T3 = time.monotonic()
        logger.info("[STOP_TRACE] T3: llm.abort_active_requests() done (Δ=%.3fs)", T3 - _t_base)

        # ── Step 4: Cancel subsidiary tasks via registry + class attrs ──
        for name, task in [
            ("scout", getattr(cls, "_scout_task", None)),
            ("consumer", getattr(cls, "_consumer_task", None)),
            ("checkpoint", getattr(cls, "_checkpoint_task", None)),
            ("macro", getattr(cls, "_macro_task", None)),
            ("analysis", getattr(cls, "_analysis_task", None)),
        ]:
            if task and not task.done():
                logger.info("[CYCLE] Cancelling %s task...", name)
                task.cancel()
                try:
                    await asyncio.wait_for(task, timeout=1.0)
                except (asyncio.CancelledError, asyncio.TimeoutError, Exception):
                    pass
        cls._scout_task = None
        cls._consumer_task = None
        cls._checkpoint_task = None
        cls._macro_task = None
        cls._analysis_task = None

        # ── Step 5: Cancel and await the main cycle task ──
        cycle_task = getattr(cls, "_cycle_task", None)
        if cycle_task is None or cycle_task.done():
            if cls._state["status"] in _TERMINAL_STATES and cls._state["status"] != "stopping":
                _task_registry.clear()
                return {"status": "already_idle", "message": "No cycle running"}
            cls._cycle_task = None
        else:
            T4_start = time.monotonic()
            logger.info("[STOP_TRACE] T4: task.cancel() called (task_id=%s)", id(cycle_task))
            cycle_task.cancel()
            try:
                await asyncio.wait_for(cycle_task, timeout=2.0)
            except (asyncio.CancelledError, asyncio.TimeoutError, Exception):
                pass
            T4 = time.monotonic()
            logger.info(
                "[STOP_TRACE] T4: await task returned (Δ=%.3fs — cancellation latency)",
                T4 - T4_start,
            )
            cls._cycle_task = None

        # ── Step 6: Drain remaining work ──
        try:
            await cycle_control.stop_and_drain(drain_seconds=0.5)
        except Exception as e:
            logger.debug("[CYCLE] stop_and_drain error (non-fatal): %s", e)

        # ── Step 7: Clean up Prism sessions ──
        session_count = 0
        try:
            if hasattr(llm, "prism_client") and llm.prism_client:
                session_count = len(llm.prism_client._sessions)
                llm.prism_client.cleanup_all_sessions()
        except Exception as e:
            logger.debug("[CYCLE] Prism cleanup error (non-fatal): %s", e)
        T6 = time.monotonic()
        logger.info("[STOP_TRACE] T6: Prism sessions cleaned (count=%d, Δ=%.3fs)", session_count, T6 - _t_base)

        # ── Step 8: Clear the task registry ──
        _task_registry.clear()

        # ── Step 9: Set terminal state in DB ──
        cycle_id = cls._state.get("cycle_id")
        checkpoint = PipelineStateDB.get_checkpoint(cycle_id) if cycle_id else None

        if not checkpoint and cycle_id:
            # Force create a synthetic checkpoint so the user can resume
            phase = cls._state.get("operational_phase", "collecting")
            completed_phases = []
            if phase == "analyzing":
                completed_phases.append("collecting")
            elif phase == "trading":
                completed_phases.extend(["collecting", "analyzing"])

            logger.info(
                "[CYCLE] Creating synthetic checkpoint for interrupted cycle %s",
                cycle_id,
            )
            try:
                PipelineStateDB.save_checkpoint(
                    cycle_id=cycle_id,
                    completed_phases=completed_phases,
                    completed_tickers={},
                    cycle_config={
                        "tickers": cls._state.get("tickers", []),
                        "collect_flag": cls._state.get("collect_flag", True),
                        "analyze_flag": cls._state.get("analyze_flag", True),
                        "trade_flag": cls._state.get("trade_flag", True),
                        "macro_memo": cls._state.get("macro_memo", ""),
                    },
                    original_started_at=cls._state.get("started_at"),
                )
                checkpoint = PipelineStateDB.get_checkpoint(cycle_id)
            except Exception as e:
                logger.error("[CYCLE] Failed to save synthetic checkpoint: %s", e)
                checkpoint = None

        T5 = time.monotonic()
        logger.info("[STOP_TRACE] T5: pipeline_state written to DB (Δ=%.3fs)", T5 - _t_base)

        if checkpoint:
            cls._state["status"] = "interrupted"
            cls._state["phase"] = "interrupted"
            cls._state["progress"] = (
                f"Cycle {cycle_id} stopped. "
                f"Completed phases: {', '.join(checkpoint['completed_phases']) or 'none'}. "
                "Resume or start fresh on next run."
            )
            cls._state["finished_at"] = datetime.now(timezone.utc).isoformat()
            cls.save_state()
            cls.emit(
                "interrupted",
                "user_stop",
                "Cycle stopped — checkpoint available for resume",
                status="ok",
            )
            T7 = time.monotonic()
            logger.info("[STOP_TRACE] T7: SSE 'interrupted' event emitted (total stop time=%.3fs)", T7 - _t_base)
            return {
                "status": "interrupted",
                "message": "Cycle stopped. Checkpoint available for resume.",
            }

        cls._state["status"] = "cancelled"
        cls._state["finished_at"] = datetime.now(timezone.utc).isoformat()
        cls.save_state()
        cls.emit("cancelled", "user_stop", "Cycle cancelled by user", status="ok")
        T7 = time.monotonic()
        logger.info("[STOP_TRACE] T7: SSE 'cancelled' event emitted (total stop time=%.3fs)", T7 - _t_base)
        return {
            "status": "cancelled",
            "message": "Cycle cancelled by user.",
        }

    @classmethod
    def request_stop(cls) -> dict:
        """Non-blocking stop: signal cancellation immediately, clean up in background.

        This returns in <50ms so the frontend gets instant feedback.
        The heavy task cancellation (up to 7s of awaits) runs in a
        background asyncio task via the existing stop_cycle() method.
        """
        from app.cycle.orchestration.cycle_control import cycle_control

        T1 = time.monotonic()
        logger.info("[STOP_TRACE] T1: request_stop() entered")

        prev_status = cls._state.get("status", "idle")
        if prev_status in _TERMINAL_STATES:
            return {"status": "already_idle", "message": "No cycle running"}

        # 1. Signal the pipeline to stop (immediate flag flip)
        cycle_control.stop()

        # 1b. Engage LLM kill switch IMMEDIATELY — don't wait for
        # background stop_cycle(). This prevents any new pipeline
        # requests from being sent to Prism while cleanup runs.
        try:
            from app.services.vllm_client import llm
            llm._killed = True
            llm.cancel_active_requests()
            llm.drain_queues()
            logger.info("[CYCLE] Kill switch engaged immediately on stop request")
        except Exception:
            pass

        # 2. Set state to 'stopping' so UI reflects it instantly
        cls._state["status"] = "stopping"
        cls._state["progress"] = "Stopping cycle..."
        cls.save_state()

        logger.info(
            "[CYCLE] Stop requested (non-blocking). "
            "Previous status: %s. Background cleanup scheduled.",
            prev_status,
        )

        # 3. Schedule the heavy cleanup as a background task
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(cls._background_stop_cleanup(_stop_t1=T1))
        except RuntimeError:
            # If no event loop, fall through — next status poll will
            # detect the 'stopping' state and the cycle task's own
            # cancellation handler will clean up.
            logger.warning(
                "[CYCLE] No event loop for background cleanup. "
                "Forcing immediate transition to interrupted."
            )
            cls._state["status"] = "interrupted"
            cls.save_state()

        return {
            "status": "stopping",
            "message": "Stop signal sent. Cycle is shutting down.",
        }

    @classmethod
    async def _background_stop_cleanup(cls, _stop_t1: float | None = None):
        """Background task that performs the heavy stop cleanup."""
        try:
            await cls.stop_cycle(_stop_t1=_stop_t1)
        except Exception as e:
            logger.error("[CYCLE] Background stop cleanup failed: %s", e)
        finally:
            if cls._state.get("status") == "stopping":
                logger.warning("[CYCLE] Failsafe: Forcing status from 'stopping' to 'interrupted'")
                cls._state["status"] = "interrupted"
                cls.save_state()

    @classmethod
    def pause_cycle(cls):
        from app.cycle.orchestration.cycle_control import cycle_control

        if cls._state["status"] not in (
            "collecting",
            "analyzing",
            "trading",
            "starting",
        ):
            raise ValueError("No active cycle to pause")
        cycle_control.pause()
        
        try:
            from app.services.vllm_client import llm
            llm.cancel_active_requests()
            llm.drain_queues()
            logger.info("[CYCLE] VLLM queues drained and active requests cancelled for pause")
        except Exception as e:
            logger.error("[CYCLE] Failed to cancel LLM requests on pause: %s", e)
            
        cls._state["status"] = "paused"
        cls.save_state()
        cls.emit("paused", "user_pause", "Cycle paused by user", status="ok")

    @classmethod
    async def resume_cycle(cls):
        from app.cycle.orchestration.cycle_control import cycle_control

        if cls._state["status"] != "paused":
            raise ValueError("Cycle is not paused")

        resume_phase = cls._state.get("operational_phase") or "collecting"
        if resume_phase not in ("collecting", "analyzing", "trading"):
            resume_phase = "collecting"

        cls._state["status"] = resume_phase
        cls.save_state()
        cls.emit("resumed", "user_resume", "Cycle resumed by user", status="ok")

        cycle_task = getattr(cls, "_cycle_task", None)
        if cycle_task is None or cycle_task.done():
            # The original async task is dead (crashed or was cancelled).
            # Instead of blindly restarting from scratch (which causes
            # collisions and data loss), mark as interrupted and delegate
            # to resume_interrupted_cycle() which has full checkpoint
            # recovery logic.
            cycle_id = cls._state.get("cycle_id")
            logger.warning(
                "[CYCLE] resume_cycle() found dead task — delegating to "
                "checkpoint-based resume for cycle %s",
                cycle_id,
            )
            # Force a checkpoint save so resume_interrupted_cycle has data
            cls.force_save_checkpoint()
            cls._state["status"] = "interrupted"
            cls.save_state()
            try:
                await cls.resume_interrupted_cycle()
            except ValueError as e:
                # If no checkpoint exists, fall back to a fresh cycle start
                logger.warning(
                    "[CYCLE] Checkpoint resume failed (%s) — falling back to idle",
                    e,
                )
                cls._state["status"] = "idle"
                cls.save_state()
                raise ValueError(
                    "Cycle task crashed and no checkpoint available. "
                    "Please start a fresh cycle."
                )
            return

        cycle_control.resume()

    @classmethod
    async def cancel_cycle_shutdown(cls):
        """Graceful shutdown handler — preserves checkpoints for resume.

        If a checkpoint exists for the running cycle, status is set to
        'interrupted' so that reset_on_boot() on the next startup will
        offer the user a Resume / Start Fresh choice.
        """
        from app.services.vllm_client import llm

        await llm.abort_active_requests()

        # If cycle already completed successfully, skip all checkpoint logic.
        # This prevents uvicorn --reload from overriding "done" to "stopped".
        current_status = cls._state.get("status", "idle")
        if current_status in ("idle", "done", "error"):
            logger.info(
                "[SHUTDOWN] Cycle already in terminal state '%s' — no checkpoint preservation needed",
                current_status,
            )
            # Still cancel background tasks to avoid orphaned coroutines
            for name, task in [
                ("scout", getattr(cls, "_scout_task", None)),
                ("consumer", getattr(cls, "_consumer_task", None)),
                ("checkpoint", getattr(cls, "_checkpoint_task", None)),
                ("macro", getattr(cls, "_macro_task", None)),
                ("analysis", getattr(cls, "_analysis_task", None)),
            ]:
                if task and not task.done():
                    task.cancel()
            cls._scout_task = None
            cls._consumer_task = None
            cls._checkpoint_task = None
            cls._macro_task = None
            cls._analysis_task = None
            cls._cycle_task = None
            return

        for name, task in [
            ("scout", getattr(cls, "_scout_task", None)),
            ("consumer", getattr(cls, "_consumer_task", None)),
            ("checkpoint", getattr(cls, "_checkpoint_task", None)),
            ("macro", getattr(cls, "_macro_task", None)),
            ("analysis", getattr(cls, "_analysis_task", None)),
        ]:
            if task and not task.done():
                task.cancel()
        cls._scout_task = None
        cls._consumer_task = None
        cls._checkpoint_task = None
        cls._macro_task = None
        cls._analysis_task = None

        cycle_task = getattr(cls, "_cycle_task", None)
        if cycle_task and not cycle_task.done():
            cycle_task.cancel()
            try:
                await asyncio.wait_for(cycle_task, timeout=2.0)
            except (asyncio.CancelledError, asyncio.TimeoutError, Exception):
                pass
            cls._cycle_task = None

            # Preserve checkpoint for resume on next boot
            cycle_id = cls._state.get("cycle_id")
            checkpoint = PipelineStateDB.get_checkpoint(cycle_id) if cycle_id else None
            if checkpoint:
                logger.info(
                    "[SHUTDOWN] Checkpoint found for cycle %s — marking as interrupted for resume",
                    cycle_id,
                )
                cls._state["status"] = "interrupted"
                cls._state["phase"] = "interrupted"
                cls._state["progress"] = (
                    f"Cycle {cycle_id} interrupted by server shutdown. "
                    f"Completed phases: {', '.join(checkpoint['completed_phases']) or 'none'}."
                )
            else:
                cls._state["status"] = "stopped"

            cls._state["finished_at"] = datetime.now(timezone.utc).isoformat()
            cls.save_state()
        else:
            if cls._state["status"] not in (
                "idle",
                "done",
                "error",
                "stopped",
                "interrupted",
            ):
                logger.info(
                    "[SHUTDOWN] Cycle task finished but state is '%s' — forcing checkpoint",
                    cls._state["status"],
                )
                cls.force_save_checkpoint()
                cycle_id = cls._state.get("cycle_id")
                checkpoint = (
                    PipelineStateDB.get_checkpoint(cycle_id) if cycle_id else None
                )
                if checkpoint:
                    cls._state["status"] = "interrupted"
                    cls._state["phase"] = "interrupted"
                    cls._state["progress"] = (
                        f"Cycle {cycle_id} interrupted by server shutdown (task done but DB pending). "
                        f"Completed phases: {', '.join(checkpoint['completed_phases']) or 'none'}."
                    )
                else:
                    cls._state["status"] = "stopped"
                cls._state["finished_at"] = datetime.now(timezone.utc).isoformat()
                cls.save_state()

    @classmethod
    async def _checkpoint_heartbeat(cls, cycle_id: str):
        """Background task that saves a checkpoint every 30 seconds while cycle is running."""
        try:
            while True:
                await asyncio.sleep(30)
                # Only save if we are actually starting, collecting, analyzing, or trading
                status = cls._state.get("status")
                if status in (
                    "starting",
                    "collecting",
                    "analyzing",
                    "trading",
                    "paused",
                ):
                    try:
                        cls.force_save_checkpoint()
                    except Exception as e:
                        logger.warning("[CHECKPOINT] Heartbeat save skipped: %s", e)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error("[CHECKPOINT] Heartbeat crashed: %s", e)

    @classmethod
    async def resume_interrupted_cycle(cls) -> dict:
        """Resume an interrupted cycle from its last checkpoint.

        This method returns instantly. The heavy initialization runs in background.
        """
        cls.load_state()
        if cls._state.get("status") != "interrupted":
            raise ValueError(
                f"No interrupted cycle to resume (status: {cls._state.get('status')})"
            )

        cycle_id = cls._state.get("cycle_id")

        cls._state.update(
            {
                "status": "starting",
                "phase": "starting",
                "progress": f"Resuming cycle {cycle_id} in background...",
                "error": None,
                "finished_at": None,
            }
        )
        cls.save_state()

        logger.info(
            "[RESUME] Resume requested for %s. Initializing in background.", cycle_id
        )

        loop = asyncio.get_running_loop()
        loop.create_task(cls._background_resume_cycle(cycle_id))

        return {
            "status": "starting",
            "cycle_id": cycle_id,
            "message": "Cycle resume initialized in background",
        }

    @classmethod
    async def _background_resume_cycle(cls, cycle_id: str):
        try:
            checkpoint = await asyncio.to_thread(
                PipelineStateDB.get_checkpoint, cycle_id
            )
            if not checkpoint:
                logger.error("[RESUME_FAIL] Missing checkpoint for cycle %s", cycle_id)
                raise ValueError(f"No checkpoint found for cycle {cycle_id}")

            config = checkpoint["cycle_config"]
            completed_phases = set(checkpoint["completed_phases"])

            logger.info(
                "[RESUME] Parsed checkpoint for %s. Completed phases: %s",
                cycle_id,
                list(completed_phases),
            )

            if "analyzing" in completed_phases:
                resume_from = "trading"
            elif "collecting" in completed_phases:
                resume_from = "analyzing"
            else:
                resume_from = "collecting"

            logger.info("[RESUME] Determined resume_from phase: %s", resume_from)

            tickers = config.get("tickers", cls._state.get("tickers", []))
            collect = config.get("collect_flag", True)
            analyze = config.get("analyze_flag", True)
            trade = True  # Always armed

            def fetch_already_analyzed():
                with get_db() as db:
                    rows = db.execute(
                        "SELECT DISTINCT ticker FROM analysis_results WHERE cycle_id = %s",
                        [cycle_id],
                    ).fetchall()
                return [r[0] for r in rows]

            already_analyzed = await asyncio.to_thread(fetch_already_analyzed)
            if already_analyzed:
                logger.info(
                    "[RESUME] Found %d already-analyzed tickers in DB",
                    len(already_analyzed),
                )

            def fetch_existing_results():
                results = []
                if resume_from in ("analyzing", "trading"):
                    with get_db() as db:
                        ar_rows = db.execute(
                            "SELECT ticker, result_json FROM analysis_results WHERE cycle_id = %s",
                            [cycle_id],
                        ).fetchall()
                    for ar in ar_rows:
                        try:
                            res = json.loads(ar[1])
                            if "ticker" not in res:
                                res["ticker"] = ar[0]
                            results.append(res)
                        except Exception:
                            pass
                return results

            existing_results = await asyncio.to_thread(fetch_existing_results)
            if existing_results:
                logger.info(
                    "[RESUME] Loaded %d existing analysis results from DB",
                    len(existing_results),
                )

            from app.cycle.orchestration.cycle_control import cycle_control

            cycle_control.reset()

            max_tickers = config.get("max_tickers")
            discovered_tickers = config.get("discovered_tickers")
            dynamic_selection_mode = config.get("dynamic_selection_mode", False)

            cls._state.update(
                {
                    "progress": f"Resuming cycle {cycle_id} from {resume_from} phase",
                    "max_tickers": max_tickers,
                    "discovered_tickers": discovered_tickers,
                    "dynamic_selection_mode": dynamic_selection_mode,
                }
            )
            cls.save_state()

            cls.emit(
                "starting",
                "resume",
                f"♻️ Resuming interrupted cycle {cycle_id} from '{resume_from}' phase "
                f"({len(already_analyzed)} tickers already analyzed, "
                f"{len(tickers) - len(already_analyzed)} remaining)",
                status="ok",
                data={
                    "resume_from": resume_from,
                    "already_analyzed": already_analyzed,
                    "remaining": [t for t in tickers if t not in already_analyzed],
                },
            )

            logger.info("=" * 70)
            logger.info(
                "  CYCLE %s RESUMED from '%s' — %d tickers (%d already done)",
                cycle_id,
                resume_from,
                len(tickers),
                len(already_analyzed),
            )
            logger.info("=" * 70)

            ctx = PipelineContext(
                tickers=tickers,
                collect=collect,
                analyze=analyze,
                trade=trade,
                cycle_id=cycle_id,
                trigger_type="resume",
                resume_from=resume_from,
                already_analyzed=already_analyzed,
                existing_results=existing_results,
                macro_memo=config.get("macro_memo", ""),
                max_tickers=max_tickers,
                discovered_tickers=discovered_tickers,
                dynamic_selection_mode=dynamic_selection_mode,
            )

            cls._cycle_task = asyncio.create_task(cls._run_cycle(ctx))
            _task_registry.register("cycle", cls._cycle_task)

            cls._checkpoint_task = asyncio.create_task(
                cls._checkpoint_heartbeat(cycle_id)
            )
            _task_registry.register("checkpoint", cls._checkpoint_task)

        except Exception as e:
            logger.error("[RESUME] Failed to resume cycle in background: %s", e)
            cls._state.update(
                {
                    "status": "error",
                    "progress": f"Failed to resume cycle: {e}",
                    "error": str(e),
                    "finished_at": datetime.now(timezone.utc).isoformat(),
                }
            )
            cls.save_state()
            cls.emit(
                "error", "resume_error", f"Failed to resume cycle: {e}", status="error"
            )

    @classmethod
    def discard_checkpoint(cls) -> dict:
        """Discard the checkpoint for an interrupted cycle and reset to idle."""
        cls.load_state()
        cycle_id = cls._state.get("cycle_id")

        if cycle_id:
            PipelineStateDB.clear_checkpoint(cycle_id)
            logger.info("[CHECKPOINT] User discarded checkpoint for %s", cycle_id)

        cls._state = PipelineStateDB.default_state()
        cls._state["finished_at"] = datetime.now(timezone.utc).isoformat()
        cls.save_state()
        return {"status": "idle", "message": f"Checkpoint discarded for {cycle_id}"}

    @classmethod
    def force_save_checkpoint(cls):
        """Manually or periodically trigger a checkpoint save using the current state."""
        cycle_id = cls._state.get("cycle_id")
        if not cycle_id:
            return

        phase = cls._state.get("operational_phase", "")
        completed_phases = []
        if phase == "analyzing":
            completed_phases.append("collecting")
        elif phase == "trading":
            completed_phases.extend(["collecting", "analyzing"])

        cycle_config = {
            "tickers": cls._state.get("tickers", []),
            "collect_flag": cls._state.get("collect_flag", True),
            "analyze_flag": cls._state.get("analyze_flag", True),
            "trade_flag": cls._state.get("trade_flag", True),
            "macro_memo": cls._state.get("macro_memo", ""),
            "max_tickers": cls._state.get("max_tickers"),
            "discovered_tickers": cls._state.get("discovered_tickers"),
            "dynamic_selection_mode": cls._state.get("dynamic_selection_mode", False),
        }

        completed_tickers = {}
        try:
            with get_db() as db:
                rows = db.execute(
                    "SELECT DISTINCT ticker FROM analysis_results WHERE cycle_id = %s",
                    [cycle_id],
                ).fetchall()
                if rows:
                    completed_tickers["analyzing"] = [r[0] for r in rows]
        except Exception as e:
            logger.warning("[CHECKPOINT] Failed to query completed tickers: %s", e)

        PipelineStateDB.save_checkpoint(
            cycle_id=cycle_id,
            completed_phases=completed_phases,
            completed_tickers=completed_tickers,
            cycle_config=cycle_config,
            original_started_at=cls._state.get("started_at"),
        )
        logger.debug(
            "[CHECKPOINT] Time-based or manual checkpoint saved for cycle %s", cycle_id
        )
