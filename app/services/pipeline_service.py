import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import Any

from app.services.pipeline_state import PipelineStateDB
from app.v3.orchestrator import run_v3_pipeline

logger = logging.getLogger(__name__)

class PipelineService:
    _state = PipelineStateDB.default_state()
    _cycle_task = None
    _stop_requested = False

    @classmethod
    def load_state(cls, summary_only: bool = False):
        cls._state = PipelineStateDB.get_state(summary_only)

    @classmethod
    def save_state(cls):
        PipelineStateDB.save_state(cls._state)

    @classmethod
    def get_current_state(cls, summary_only: bool = False) -> dict:
        return PipelineStateDB.get_state(summary_only)

    @classmethod
    async def start_cycle(cls, tickers: list[str], **kwargs):
        if cls._state.get("status") in ("running", "starting", "stopping"):
            return {"status": "deduplicated", "message": "Cycle already running or stopping"}

        cycle_id = kwargs.get("cycle_id") or f"cycle-v3-{int(time.time())}"
        
        # ── Dynamic Watchlist Pre-Filter (Gatekeeper) ──
        if not tickers or kwargs.get("dynamic_selection_mode") or len(tickers) > 5:
            max_tickers = kwargs.get("max_tickers") or 5
            cls._state.update({
                "status": "starting",
                "progress": f"Screening watchlist for top {max_tickers} setups...",
            })
            cls.save_state()
            
            try:
                from app.trading.watchlist import get_active
                from app.utils.batch_screener import get_watchlist_snapshots
                from app.agents.base_agent import run_agent
                from app.v3.agents.portfolio_manager import SYSTEM_PROMPT, AGENT_NAME
                import json
                
                if tickers:
                    active_tickers = tickers
                else:
                    active_tickers = [t["ticker"] for t in get_active()]
                    
                if not active_tickers:
                    logger.warning("[PipelineService] Watchlist is empty, falling back to default.")
                    tickers = ["AAPL"]
                else:
                    snapshot_table = await get_watchlist_snapshots(active_tickers)
                    
                    system_prompt = SYSTEM_PROMPT.replace("{max_tickers}", str(max_tickers))
                    user_prompt = f"Here is the active watchlist snapshot:\n\n{snapshot_table}"
                    
                    from app.utils.text_utils import parse_json_response
                    result = await run_agent(
                        agent_name=AGENT_NAME,
                        ticker="WATCHLIST",
                        cycle_id=cycle_id,
                        bot_id="cycle-backend",
                        system_prompt=system_prompt,
                        user_prompt=user_prompt,
                        enable_tools=True,
                    )
                    
                    final_text = result.get("response", "{}")
                    parsed = parse_json_response(final_text)
                    if not parsed:
                        parsed = {}
                        
                    selected = parsed.get("selected_tickers", [])
                    rationale = parsed.get("rationale", "")
                    
                    if selected:
                        tickers = selected
                        logger.info("[PipelineService] Gatekeeper selected: %s. Rationale: %s", tickers, rationale)
                    else:
                        logger.info("[PipelineService] Gatekeeper chose 0 tickers. Ending cycle early. Rationale: %s", rationale)
                        cls._state.update({"status": "completed", "progress": "Gatekeeper bypassed."})
                        cls.save_state()
                        return {"status": "skipped", "message": "Gatekeeper found no compelling setups"}
            except Exception as e:
                logger.error("[PipelineService] Portfolio screener failed, falling back to AAPL: %s", e)
                tickers = ["AAPL"]

        cls._state.update({
            "status": "running",
            "cycle_id": cycle_id,
            "tickers": tickers,
            "progress": f"Starting V3 cycle for {len(tickers)} tickers",
            "phase": "running",
            "started_at": datetime.now(timezone.utc).isoformat(),
            "finished_at": None,
            "error": None
        })
        cls.save_state()
        cls._stop_requested = False

        # Reset the VLLM client kill switch so requests can flow on the new cycle
        try:
            from app.services.vllm_client import llm
            llm.reset_kill_switch()
        except Exception as e:
            logger.error("[PipelineService] Failed to reset VLLM kill switch: %s", e)

        cls._cycle_task = asyncio.create_task(cls._run_all_v3(cycle_id, tickers))
        return {"status": "starting", "cycle_id": cycle_id, "message": "V3 pipeline started"}

    @classmethod
    async def _run_all_v3(cls, cycle_id: str, tickers: list[str]):
        try:
            def emit_cb(phase: str, step: str, detail: str, **kwargs):
                event = {
                    "ts": datetime.now(timezone.utc).isoformat(),
                    "phase": phase,
                    "step": step,
                    "detail": detail,
                    "status": kwargs.get("status", "ok"),
                    "data": kwargs.get("data", {}),
                    "elapsed_ms": kwargs.get("elapsed_ms", 0),
                }
                PipelineStateDB.append_events(cycle_id, [event])

            from app.services.adaptive_concurrency import concurrency_controller
            
            cls._state["progress"] = f"Processing {len(tickers)} tickers concurrently"
            cls.save_state()

            async def _process_ticker(i: int, ticker_name: str):
                if cls._stop_requested:
                    logger.info("[PipelineService] V3 Cycle stopped by user request (ticker=%s).", ticker_name)
                    return
                
                result = await run_v3_pipeline(ticker=ticker_name, cycle_id=cycle_id, emit=emit_cb)
                
                # Save verdict to DB
                from app.services.result_saver import save_analysis_result
                save_analysis_result(ticker_name, cycle_id, result)
                
                # Execute Trade — gated by confidence threshold
                action = result.get("action", "HOLD")
                confidence = result.get("confidence", 0)
                
                try:
                    from app.config import settings as _cfg
                    from app.trading.paper_trader import buy, sell

                    if confidence is None:
                        logger.warning(
                            "[PipelineService] %s: confidence is None — defaulting to 0, skipping trade",
                            ticker_name,
                        )
                        confidence = 0

                    if action in ("BUY", "SELL") and confidence < _cfg.ANALYSIS_CONFIDENCE_THRESHOLD:
                        logger.warning(
                            "[PipelineService] %s: %s blocked — confidence %d%% < threshold %d%%",
                            ticker_name, action, confidence, _cfg.ANALYSIS_CONFIDENCE_THRESHOLD,
                        )
                    elif action == "BUY":
                        size_pct = max(0.02, min(0.10, confidence / 100.0 * 0.10))
                        await buy(bot_id="cycle-backend", ticker=ticker_name, size_pct=size_pct, cycle_id=cycle_id)
                    elif action == "SELL":
                        await sell(bot_id="cycle-backend", ticker=ticker_name, cycle_id=cycle_id, qty_pct=1.0)
                        
                    # Handle Triggers (limit orders)
                    decision = result.get("estimate", {})
                    stop_loss = decision.get("stop_loss")
                    take_profit = decision.get("take_profit")
                    if stop_loss or take_profit:
                        from app.trading.order_triggers import create_trigger
                        if stop_loss:
                            await create_trigger(bot_id="cycle-backend", ticker=ticker_name, trigger_type="stop_loss", trigger_price=float(stop_loss), action="SELL", qty_pct=1.0, created_by="pipeline")
                        if take_profit:
                            await create_trigger(bot_id="cycle-backend", ticker=ticker_name, trigger_type="take_profit", trigger_price=float(take_profit), action="SELL", qty_pct=1.0, created_by="pipeline")
                except Exception as e:
                    logger.error("[PipelineService] Trade execution failed for %s: %s", ticker_name, e)

            # Build tasks and execute concurrently via adaptive concurrency
            tasks = [_process_ticker(i, t) for i, t in enumerate(tickers)]
            await concurrency_controller.gather(tasks, label="v3_pipeline")


            from app.v3.debate_coordinator import run_battle_royale
            await run_battle_royale(cycle_id=cycle_id, bot_id="cycle-backend")

            cls._state.update({
                "status": "done",
                "progress": "V3 cycle complete",
                "finished_at": datetime.now(timezone.utc).isoformat()
            })
        except asyncio.CancelledError:
            logger.info("[PipelineService] V3 Cycle CANCELLED — killing active VLLM connections")
            # Force-close all TCP connections to VLLM endpoints so GPUs stop inference
            try:
                from app.services.vllm_client import llm
                await llm.abort_active_requests()
            except Exception as abort_err:
                logger.error("[PipelineService] abort_active_requests failed: %s", abort_err)
            cls._state.update({
                "status": "stopped",
                "progress": "Cycle stopped by user",
                "finished_at": datetime.now(timezone.utc).isoformat()
            })
            # Do NOT re-raise — let the finally block clean up and let
            # stop_cycle() see the task as done.
        except Exception as e:
            logger.error("[PipelineService] V3 Cycle failed: %s", e)
            cls._state.update({
                "status": "error",
                "error": str(e),
                "finished_at": datetime.now(timezone.utc).isoformat()
            })
        finally:
            cls.save_state()
            cls._cycle_task = None

    @classmethod
    def request_stop(cls):
        cls._stop_requested = True
        cls._state.update({"status": "stopping", "progress": "Stopping V3 cycle..."})
        cls.save_state()
        if cls._cycle_task and not cls._cycle_task.done():
            cls._cycle_task.cancel()
        return {"status": "stopping"}

    @classmethod
    async def stop_cycle(cls, _stop_t1=None):
        cls.request_stop()
        if cls._cycle_task and not cls._cycle_task.done():
            try:
                await asyncio.wait_for(cls._cycle_task, timeout=5.0)
            except (Exception, asyncio.CancelledError):
                pass
        # Nuclear kill: force-close all TCP connections to VLLM endpoints
        # so GPUs immediately stop inference even if the task didn't clean up
        try:
            from app.services.vllm_client import llm
            await llm.abort_active_requests()
        except Exception as abort_err:
            logger.error("[PipelineService] abort_active_requests in stop_cycle failed: %s", abort_err)
        cls._state.update({
            "status": "stopped",
            "progress": "Cycle stopped by user",
            "finished_at": datetime.now(timezone.utc).isoformat()
        })
        cls.save_state()
        return {"status": "stopped"}

    @classmethod
    def pause_cycle(cls):
        return {"status": "error", "message": "Pause not supported in V3"}

    @classmethod
    async def resume_cycle(cls):
        return {"status": "error", "message": "Resume not supported in V3"}
        
    @classmethod
    async def resume_interrupted_cycle(cls):
        return {"status": "error", "message": "Resume not supported in V3"}

    @classmethod
    def discard_checkpoint(cls):
        return {"status": "ok"}
        
    @classmethod
    def force_save_checkpoint(cls):
        pass

pipeline_service = PipelineService()
