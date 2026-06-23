import logging
import time
from datetime import datetime, timezone
import traceback
import asyncio
from typing import Any

from app.config import settings
from app.db.connection import get_db
from app.cycle.orchestration.state_manager import PipelineStateDB
from app.services.logging.cycle_auditor import CycleAuditor
from app.log_manager import log_manager



from app.utils.trace import set_trace_id
from app.pipeline.orchestration.cycle_control import cycle_control
from app.autoresearch import run_autoresearch
from app.db.checkpoints import checkpoint_manager
from app.pipeline.analysis.benchmark import persist_benchmark
from app.cycle.context import CycleContext
from app.cycle.orchestration.event_bus import event_bus
from app.services.bot_manager import resolve_bot_id

logger = logging.getLogger(__name__)
_auditor = CycleAuditor()

_current_cycle_id: str = ""

def get_current_cycle_id() -> str:
    return _current_cycle_id

class OrchestratorV3Mixin:
    """
    Event-Driven Orchestrator for the Autonomous Trading Cycle.
    Replaces the rigid sequential phases of v2 with asynchronous pub/sub.
    """

    @classmethod
    async def _execute_cycle(cls, ctx: CycleContext) -> None:
        """Main loop for the trading cycle."""
        from app.monitoring.pipeline_profiler import profiler as pipeline_profiler

        pipeline_profiler.start_cycle(ctx.cycle_id)
        cls._start_time = time.monotonic()
        loop_start = datetime.now(timezone.utc)

        global _current_cycle_id
        _current_cycle_id = ctx.cycle_id

        logger.info("===== CYCLE START (EVENT-DRIVEN V3): %s =====", loop_start.strftime("%Y-%m-%d %H:%M:%S"))

        cls._cycle_summary = {
            "cycle_id": ctx.cycle_id,
            "trigger_type": ctx.trigger_type,
            "schedule_id": getattr(ctx, "schedule_id", None),
            "execution_mode": cls._state.get("execution_mode", "production"),
            "started_at": loop_start.isoformat(),
            "finished_at": None,
            "status": "starting",
            "tickers_requested": ctx.tickers,
            "tickers_final": [],
            "collect_requested": ctx.collect,
            "analyze_requested": ctx.analyze,
            "trade_requested": ctx.trade,
            "jetson_healthy_start": False,
            "collector_ok": 0,
            "collector_skipped": 0,
            "collector_error": 0,
            "collector_failures": [],
            "analysis_results_count": 0,
            "buy_count": 0,
            "sell_count": 0,
            "hold_count": 0,
            "review_count": 0,
            "trade_attempted": 0,
            "trade_executed": 0,
            "trade_failed": 0,
            "trade_skip_categories": {},
            "no_trade_reason": None,
            "primary_failure_reason": None,
        }

        bot_id = resolve_bot_id(settings.BOT_ID)
        try:
            await cls._execute_cycle_impl(ctx, bot_id)
        finally:
            cls._scout_task = None
            cls._consumer_task = None
            cls._macro_task = None
            cls._analysis_task = None
            cls._autoresearch_task = None

            import os as _os
            _start_paused = _os.getenv("START_PAUSED", "true").lower() in ("true", "1", "yes")
            if _start_paused:
                await cycle_control.stop_and_drain(drain_seconds=0.5)
                logger.info("[CYCLE] Cycle ended — system drained and re-paused.")

    @classmethod
    async def _execute_cycle_impl(cls, ctx: CycleContext, bot_id: str) -> None:
        """
        Event-Driven Implementation:
          1. Phase 1 (Health)
          2. Event Bus Start
          3. Planner Agent spawns Swarm
          4. Wait for CYCLE_COMPLETE event
          5. Phase 6 (Post Housekeeping)
        """
        try:
            set_trace_id(ctx.cycle_id)

            cls._cycle_summary["cycle_id"] = ctx.cycle_id
            cls._cycle_summary["status"] = "created"
            cls._cycle_summary["bot_id"] = bot_id
            cls._state["cycle_id"] = ctx.cycle_id
            cls._state["tickers"] = ctx.tickers
            cls._state["status"] = "created"
            
            cls._state["status"] = "queued"
            cls.emit("queued", "init", "Cycle queued for execution", status="ok")

            _auditor.phase_entry(ctx.cycle_id, "starting")

            cls._state["status"] = "analyzing"
            cls.emit("started", "concurrent", "Starting Event-Driven Swarm", status="ok")

            # ── Event-Driven Architecture ──
            event_bus.start()
            completion_event = asyncio.Event()
            traded_tickers = set()
            aborted_reason = None

            # Dynamic Selection Mode fallback
            tickers_to_process = ctx.tickers
            if getattr(ctx, "dynamic_selection_mode", False) and not tickers_to_process:
                from app.tools.ticker_candidates import generate_candidate_tickers
                try:
                    tickers_to_process = generate_candidate_tickers()
                except Exception as e:
                    logger.warning(f"Failed to generate candidates, falling back to empty list: {e}")
            
            ctx.tickers = tickers_to_process
            cls._cycle_summary["tickers_final"] = tickers_to_process

            async def trigger_housekeeping():
                event_bus.publish("START_HOUSEKEEPING", {
                    "ctx": ctx,
                    "bot_id": bot_id,
                    "results": [],
                    "trade_result": {"status": "ok", "executed": len(traded_tickers)},
                    "emit": cls.emit,
                    "state": cls._state,
                    "cycle_summary": cls._cycle_summary
                })

            async def on_trade_complete(payload):
                ticker = payload.get("ticker")
                if ticker:
                    traded_tickers.add(ticker)
                if len(traded_tickers) >= len(ctx.tickers):
                    await trigger_housekeeping()

            async def on_cycle_completed(payload):
                await trigger_housekeeping()

            async def on_teardown_complete(payload):
                completion_event.set()

            async def on_cycle_aborted(payload):
                nonlocal aborted_reason
                aborted_reason = payload.get("reason", "unknown")
                completion_event.set()

            event_bus.subscribe("TRADE_COMPLETE", on_trade_complete)
            event_bus.subscribe("CYCLE_COMPLETED", on_cycle_completed)
            event_bus.subscribe("TEARDOWN_COMPLETE", on_teardown_complete)
            event_bus.subscribe("CYCLE_ABORTED", on_cycle_aborted)

            # Manually trigger completion if no tickers to process
            if not tickers_to_process:
                await trigger_housekeeping()

            # Initialize and start the Peer-to-Peer Agent Mesh
            from app.cycle.orchestration.agent_mesh import AgentMesh
            mesh = AgentMesh()
            await mesh.start()

            # Kick off the mesh with the initial event
            event_bus.publish("CYCLE_TRIGGERED", {
                "ctx": ctx,
                "bot_id": bot_id,
                "emit": cls.emit,
                "cycle_summary": cls._cycle_summary,
                "state": cls._state
            })

            # Wait for completion of all trades and teardown
            try:
                await asyncio.wait_for(completion_event.wait(), timeout=5400.0)  # 90 min timeout
            except asyncio.TimeoutError:
                logger.error("[CYCLE] Event Swarm safety timeout (90m) exceeded.")
            finally:
                await mesh.stop()
                event_bus.stop()
                event_bus.clear()

            if aborted_reason:
                raise Exception(f"Cycle aborted: {aborted_reason}")

            ended = datetime.now(timezone.utc).isoformat()
            cls._cycle_summary["status"] = "done"
            cls._cycle_summary["ended_at"] = ended
            cls._state["status"] = "done"
            cls._state["finished_at"] = ended
            cls.save_state()
            cls.emit("done", "post", "Cycle completed successfully", status="done")

            try:
                PipelineStateDB.clear_checkpoint(ctx.cycle_id)
            except Exception:
                pass

        except asyncio.CancelledError:
            # Handle cancellation (timeout vs manual stop)
            elapsed_sec = (time.monotonic() - cls._start_time) if hasattr(cls, "_start_time") else 0
            timeout_sec = int(getattr(settings, "CYCLE_TIMEOUT_MINUTES", 120)) * 60
            is_timeout = elapsed_sec >= timeout_sec
            cancel_reason = "System Timeout Hit (>%d min)" % (timeout_sec // 60) if is_timeout else "User manually stopped the cycle"
            
            logger.warning(f"[CYCLE] PIPELINE CANCELLED ({cancel_reason}).")
            cls._cycle_summary["status"] = "cancelled"
            cls._state["status"] = "cancelled"
            cls.save_state()
            cls.emit("trading", "cancelled", f"Cycle cancelled: {cancel_reason}", status="error")
            raise

        except Exception as e:
            logger.error("[CYCLE] FATAL PIPELINE ERROR: %s", e)
            logger.debug(traceback.format_exc())
            cls._cycle_summary["status"] = "error"
            cls._state["status"] = "error"
            cls.save_state()
            cls.emit("trading", "error", f"Cycle failed: {e}", status="error")
            raise
        finally:
            global _current_cycle_id
            _current_cycle_id = ""
            cls._finalize_cycle_telemetry(ctx)

    @classmethod
    def _finalize_cycle_telemetry(cls, ctx: Any) -> None:
        try:
            if hasattr(cls, "flush_events"):
                cls.flush_events()
        except Exception:
            pass

        try:
            from app.services.vllm_client import llm
            if hasattr(llm, "prism_client") and llm.prism_client:
                llm.prism_client.cleanup_all_sessions()
        except Exception:
            pass

        try:
            from app.monitoring.pipeline_profiler import profiler as pipeline_profiler
            pipeline_profiler.end_cycle()
        except Exception:
            pass
