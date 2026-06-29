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
        # Read from DB for dedup — in-memory _state can be stale after
        # force-reset or container restart.
        db_state = PipelineStateDB.get_state(summary_only=True)
        db_status = db_state.get("status", "idle")
        if db_status in ("running", "starting", "stopping"):
            return {"status": "deduplicated", "message": f"Cycle already {db_status}"}
        # Also check in-memory task to catch race where DB was reset but task is still running
        if cls._cycle_task and not cls._cycle_task.done():
            return {"status": "deduplicated", "message": "Cycle task still running"}

        # Reset the SDK kill switch so requests can flow on the new cycle
        try:
            from lazycat.llm import PrismClient
            # Assuming trading-service uses a singleton prism_client from somewhere
            # Let's import it from prism_agent_caller
            from app.services.prism_agent_caller import prism_client
            prism_client.reset_kill_switch()
        except Exception as e:
            logger.error("[PipelineService] Failed to reset VLLM kill switch: %s", e)

        cycle_id = kwargs.get("cycle_id") or f"cycle-v3-{int(time.time())}"
        max_tickers = kwargs.get("max_tickers") or 5
        
        cls._state.update({
            "status": "starting",
            "cycle_id": cycle_id,
            "progress": f"Screening watchlist for top {max_tickers} setups..."
        })
        cls.save_state()
        cls._stop_requested = False

        clean_kwargs = {k: v for k, v in kwargs.items() if k not in ("cycle_id", "tickers", "max_tickers")}
        cls._cycle_task = asyncio.create_task(cls._run_all_v3(cycle_id, tickers, max_tickers, **clean_kwargs))
        return {"status": "starting", "cycle_id": cycle_id, "message": "V3 pipeline started"}

    @classmethod
    async def _run_all_v3(cls, cycle_id: str, tickers: list[str], max_tickers: int = 5, **kwargs):
        try:
            # 1. Run Gatekeeper
            try:
                from app.trading.watchlist import get_active
                from app.utils.batch_screener import get_watchlist_snapshots
                from app.agents.base_agent import run_agent
                from app.v3.agents.portfolio_manager import SYSTEM_PROMPT, AGENT_NAME
                import json
                
                if tickers:
                    base_tickers = tickers
                else:
                    base_tickers = [t["ticker"] for t in get_active()]
                    
                # --- DISCOVERY ENGINE ---
                active_ticker_dicts = []
                
                # Dynamic scraper run at the start of auto-discovery
                if not tickers:
                    def discovery_emit(step: str, detail: str, status: str = "running"):
                        event = {
                            "ts": datetime.now(timezone.utc).isoformat(),
                            "phase": "discovery",
                            "step": step,
                            "detail": detail,
                            "status": status,
                            "data": {}
                        }
                        logger.info(f"[{cycle_id}][discovery][{step}] {detail}")
                        PipelineStateDB.append_events(cycle_id, [event])
                        
                    discovery_emit("scraper_start", "📡 Starting news scraper sweep to collect trending catalysts...", "running")
                    try:
                        from app.collectors.news_collector import collect_all
                        total_scraped = await collect_all(limit_feeds=10, emit_cb=discovery_emit)
                        discovery_emit("scraper_done", f"✅ News scraper sweep complete: collected {total_scraped} articles", "ok")
                    except Exception as e:
                        logger.error(f"[PipelineService] Discovery scraping failed: {e}")
                        discovery_emit("scraper_err", f"❌ Scraper sweep failed: {e}", "error")
                # Find trending tickers from the last 24h (News, Reddit, YouTube) that aren't in the static watchlist
                try:
                    from app.db.connection import get_db
                    from app.processors.ticker_extractor import FALSE_TICKERS
                    with get_db() as db:
                        # 1. Pull Trending from each source independently
                        news_trends = db.execute("""
                            SELECT ticker, COUNT(*) as mentions FROM news_articles 
                            WHERE ticker IS NOT NULL AND published_at > NOW() - INTERVAL '24 hours'
                            GROUP BY ticker ORDER BY COUNT(*) DESC LIMIT 10
                        """).fetchall()
                        reddit_trends = db.execute("""
                            SELECT ticker, COUNT(*) as mentions FROM reddit_posts 
                            WHERE ticker IS NOT NULL AND created_utc > NOW() - INTERVAL '24 hours'
                            GROUP BY ticker ORDER BY COUNT(*) DESC LIMIT 10
                        """).fetchall()
                        youtube_trends = db.execute("""
                            SELECT ticker, COUNT(*) as mentions FROM youtube_transcripts 
                            WHERE ticker IS NOT NULL AND published_at > NOW() - INTERVAL '24 hours'
                            GROUP BY ticker ORDER BY COUNT(*) DESC LIMIT 5
                        """).fetchall()
                        
                        # 2. Phase 4A: Cross-reference — track source count per ticker
                        source_tracker: dict[str, dict] = {}  # ticker -> {"sources": set, "mentions": int}
                        for rows, source_label in [
                            (news_trends, "News"),
                            (reddit_trends, "Reddit"),
                            (youtube_trends, "YouTube"),
                        ]:
                            for row in rows:
                                tkr = row[0].upper().strip()
                                if not tkr or tkr in base_tickers:
                                    continue
                                # Phase 4A: FALSE_TICKERS pre-filter
                                if tkr in FALSE_TICKERS:
                                    logger.debug("[PipelineService] Filtered out FALSE_TICKER: %s from %s", tkr, source_label)
                                    continue
                                if tkr not in source_tracker:
                                    source_tracker[tkr] = {"sources": set(), "mentions": 0}
                                source_tracker[tkr]["sources"].add(source_label)
                                source_tracker[tkr]["mentions"] += row[1] if len(row) > 1 else 1
                        
                        # 3. Phase 4A: Build trending_discovered with source counts
                        trending_discovered = {}
                        for tkr, info in source_tracker.items():
                            source_count = len(info["sources"])
                            source_label = f"Trending {'+'.join(sorted(info['sources']))}"
                            if source_count >= 2:
                                source_label += f" ({source_count} sources)"
                            trending_discovered[tkr] = {
                                "label": source_label,
                                "source_count": source_count,
                                "total_mentions": info["mentions"],
                            }
                        
                        all_pool = {t: {"label": "Watchlist", "source_count": 0, "total_mentions": 0} for t in base_tickers}
                        all_pool.update(trending_discovered)
                        
                        # 4. Fetch Last Analysis Date for all
                        if all_pool:
                            placeholders = ','.join(['%s'] * len(all_pool))
                            last_analysis_rows = db.execute(f"""
                                SELECT ticker, MAX(created_at) as last_date 
                                FROM analysis_results 
                                WHERE ticker IN ({placeholders}) 
                                GROUP BY ticker
                            """, list(all_pool.keys())).fetchall()
                            
                            last_analysis_map = {r[0]: r[1] for r in last_analysis_rows}
                        else:
                            last_analysis_map = {}
                            
                        # 5. Construct dictionary structure
                        for tkr, info in all_pool.items():
                            last_date = last_analysis_map.get(tkr)
                            if last_date:
                                if last_date.tzinfo is None:
                                    last_date = last_date.replace(tzinfo=timezone.utc)
                                days_ago = (datetime.now(timezone.utc) - last_date).days
                                dsa_str = f"{days_ago} days ago" if days_ago > 0 else "Today"
                            else:
                                dsa_str = "Never"
                                
                            active_ticker_dicts.append({
                                "ticker": tkr,
                                "source": info["label"],
                                "days_since_analysis": dsa_str,
                                "source_count": info["source_count"],
                                "total_mentions": info["total_mentions"],
                            })
                            
                        if trending_discovered:
                            multi_source = [t for t, i in trending_discovered.items() if i["source_count"] >= 2]
                            logger.info(
                                "[PipelineService] Discovery Engine: %d trending leads (%d multi-source: %s)",
                                len(trending_discovered), len(multi_source), multi_source[:5],
                            )
                except Exception as e:
                    logger.error(f"[PipelineService] Discovery Engine failed to fetch trends: {e}")
                # ------------------------

                if not active_ticker_dicts:
                    logger.warning("[PipelineService] Watchlist is empty, falling back to default.")
                    tickers = ["AAPL"]
                else:
                    _, raw_results = await get_watchlist_snapshots(active_ticker_dicts)
                    
                    if not raw_results:
                        logger.warning("[PipelineService] No valid data returned from yfinance screener.")
                        tickers = ["AAPL"]
                    else:
                        # --- SCORING ENGINE ---
                        scored_results = []
                        # Build a lookup for source_count from active_ticker_dicts
                        source_count_map = {d["ticker"]: d.get("source_count", 0) for d in active_ticker_dicts}
                        # raw_results format: (t, px, chg, rvol, sma, rsi, src, dsa)
                        for t, px, chg, rvol, sma, rsi, src, dsa in raw_results:
                            score = rvol * 10.0
                            
                            if "Trending" in src:
                                score += 15.0
                            
                            # Phase 4A: Multi-source cross-reference boost
                            sc = source_count_map.get(t, 0)
                            if sc >= 2:
                                score += (sc - 1) * 10.0  # +10 per additional source
                                
                            scored_results.append({
                                "ticker": t, "price": px, "chg": chg, "rvol": rvol, 
                                "sma": sma, "rsi": rsi, "src": src, "dsa": dsa, "score": score
                            })
                            
                        # Sort by score descending and take top 20
                        scored_results.sort(key=lambda x: x["score"], reverse=True)
                        top_scorers = scored_results[:20]
                        
                        logger.info(f"[PipelineService] Scoring Engine top picks: {[s['ticker'] for s in top_scorers]}")
                        
                        # Rebuild markdown table for Gatekeeper
                        md_lines = [
                            "| Ticker | Score | Source | Days Since Analysis | Price | Change % | Rel Volume | SMA-20 | RSI (14) |",
                            "|--------|-------|--------|---------------------|-------|----------|------------|--------|----------|"
                        ]
                        for s in top_scorers:
                            sma_rel = ((s["price"] - s["sma"]) / s["sma"]) * 100 if s["sma"] > 0 else 0
                            md_lines.append(f"| {s['ticker']} | {s['score']:.1f} | {s['src']} | {s['dsa']} | ${s['price']:.2f} | {s['chg']:+.2f}% | {s['rvol']:.2f}x | {sma_rel:+.2f}% | {s['rsi']:.1f} |")
                            
                        snapshot_table = "\n".join(md_lines)
                        # -----------------------
                    
                    min_tickers = 5
                    max_tickers = 15
                    system_prompt = SYSTEM_PROMPT.replace("{min_tickers}", str(min_tickers)).replace("{max_tickers}", str(max_tickers))
                    user_prompt = f"Here is the active watchlist snapshot (Top 20):\n\n{snapshot_table}\n\nIMPORTANT: You must output ONLY a valid JSON object. Do NOT output any conversational text or formatting blocks. Your response must begin with {{ and end with }}."
                    
                    from app.services.bot_manager import get_active_bot_id
                    active_bot_id = get_active_bot_id()
                    
                    from app.utils.text_utils import parse_json_response
                    result = await run_agent(
                        agent_name=AGENT_NAME,
                        ticker="WATCHLIST",
                        cycle_id=cycle_id,
                        bot_id=active_bot_id,
                        system_prompt=system_prompt,
                        user_prompt=user_prompt,
                        enable_tools=False, # DISABLED tools so it strictly outputs JSON!
                        harness_provider=kwargs.get("harness_provider", "local"),
                    )
                    
                    final_text = result.get("response", "{}")
                    logger.info("[PipelineService] Raw gatekeeper response: %s", final_text)
                    parsed = parse_json_response(final_text)
                    logger.info("[PipelineService] Parsed gatekeeper JSON: %s", parsed)
                    if not parsed:
                        parsed = {}
                        
                    selected = parsed.get("selected_tickers", [])
                    rationale = parsed.get("rationale", "")
                    
                    if selected:
                        tickers = selected
                        logger.info("[PipelineService] Gatekeeper selected: %s. Rationale: %s", tickers, rationale)
                    else:
                        logger.info("[PipelineService] Gatekeeper chose 0 tickers. Ending cycle early. Rationale: %s", rationale)
                        PipelineStateDB.append_events(cycle_id, [{
                            "ts": datetime.now(timezone.utc).isoformat(),
                            "phase": "gatekeeper",
                            "step": "GATEKEEPER_SKIPPED",
                            "detail": f"Gatekeeper found no compelling setups. {rationale}",
                            "status": "skipped",
                            "data": {"rationale": rationale}
                        }])
                        cls._state.update({"status": "idle", "progress": "Gatekeeper bypassed."})
                        cls.save_state()
                        return
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error("[PipelineService] Portfolio screener failed, falling back to AAPL: %s", e)
                tickers = ["AAPL"]

            # Set status to running now that gatekeeper is done
            cls._state.update({
                "status": "running",
                "tickers": tickers,
                "progress": f"Starting V3 cycle for {len(tickers)} tickers",
                "phase": "running",
                "started_at": datetime.now(timezone.utc).isoformat(),
                "finished_at": None,
                "error": None
            })
            cls.save_state()

            if cls._stop_requested:
                raise asyncio.CancelledError()

            def emit_cb(phase: str, step: str, detail: str, **kwargs):
                event = {
                    "ts": datetime.now(timezone.utc).isoformat(),
                    "phase": phase,
                    "step": step,
                    "detail": detail,
                    "status": kwargs.pop("status", "running"),
                    "data": kwargs.pop("data", {}),
                    "elapsed_ms": kwargs.pop("elapsed_ms", 0),
                }
                event.update(kwargs)
                logger.info(f"[{cycle_id}][{phase}][{step}] {detail}")
                PipelineStateDB.append_events(cycle_id, [event])

            from app.services.adaptive_concurrency import concurrency_controller
            
            cls._state["progress"] = f"Processing {len(tickers)} tickers concurrently"
            cls.save_state()

            async def _process_ticker(i: int, ticker_name: str):
                if cls._stop_requested:
                    logger.info("[PipelineService] V3 Cycle stopped by user request (ticker=%s).", ticker_name)
                    return
                
                harness_provider = kwargs.get("harness_provider", "local") if "kwargs" in locals() else "local"
                result = await run_v3_pipeline(ticker=ticker_name, cycle_id=cycle_id, emit=emit_cb, harness_provider=harness_provider)
                
                # Save verdict to DB
                from app.services.result_saver import save_analysis_result
                save_analysis_result(ticker_name, cycle_id, result)
                
                # Execute Trade — gated by confidence threshold
                action = result.get("action", "HOLD")
                confidence = result.get("confidence", 0)
                
                try:
                    from app.config import settings as _cfg
                    from app.trading.paper_trader import buy, sell
                    from app.services.bot_manager import get_active_bot_id
                    active_bot_id = get_active_bot_id()

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
                        await buy(bot_id=active_bot_id, ticker=ticker_name, size_pct=size_pct, cycle_id=cycle_id)
                    elif action == "SELL":
                        await sell(bot_id=active_bot_id, ticker=ticker_name, cycle_id=cycle_id, qty_pct=1.0)
                        
                    # Handle Triggers (limit orders)
                    decision = result.get("estimate", {})
                    stop_loss = decision.get("stop_loss")
                    take_profit = decision.get("take_profit")
                    if stop_loss or take_profit:
                        from app.trading.order_triggers import create_trigger
                        if stop_loss:
                            await create_trigger(bot_id=active_bot_id, ticker=ticker_name, trigger_type="stop_loss", trigger_price=float(stop_loss), action="SELL", qty_pct=1.0, created_by="pipeline")
                        if take_profit:
                            await create_trigger(bot_id=active_bot_id, ticker=ticker_name, trigger_type="take_profit", trigger_price=float(take_profit), action="SELL", qty_pct=1.0, created_by="pipeline")
                except Exception as e:
                    logger.error("[PipelineService] Trade execution failed for %s: %s", ticker_name, e)

            # Build tasks and execute concurrently via adaptive concurrency
            tasks = [_process_ticker(i, t) for i, t in enumerate(tickers)]
            await concurrency_controller.gather(tasks, label="v3_pipeline")

            if cls._stop_requested:
                raise asyncio.CancelledError("Cycle stopped by user")

            from app.services.bot_manager import get_active_bot_id
            active_bot_id = get_active_bot_id()

            from app.v3.debate_coordinator import run_battle_royale
            await run_battle_royale(cycle_id=cycle_id, bot_id=active_bot_id)

            cls._state.update({
                "status": "done",
                "progress": "V3 cycle complete",
                "finished_at": datetime.now(timezone.utc).isoformat()
            })
        except asyncio.CancelledError:
            logger.info("[PipelineService] V3 Cycle CANCELLED — pipeline aborted")

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
        
        # Arm kill switch to instantly abort any running HTTP streams
        try:
            from app.services.prism_agent_caller import prism_client
            prism_client.arm_kill_switch()
        except Exception as e:
            logger.error("[PipelineService] Failed to arm kill switch: %s", e)
            
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

        cls._state.update({
            "status": "stopped",
            "progress": "Cycle stopped by user",
            "finished_at": datetime.now(timezone.utc).isoformat()
        })
        cls.save_state()
        return {"status": "stopped"}

    @classmethod
    async def force_reset(cls):
        """Nuclear reset: cancel everything and return to idle.

        Called by FORCE_RESET command. Unlike stop_cycle() which sets
        status to 'stopped', this resets to 'idle' so a new cycle can
        start immediately without the frontend needing another action.
        """
        logger.warning("[PipelineService] FORCE_RESET — cancelling task and resetting to idle")
        cls._stop_requested = True
        if cls._cycle_task and not cls._cycle_task.done():
            cls._cycle_task.cancel()
            try:
                await asyncio.wait_for(cls._cycle_task, timeout=3.0)
            except (Exception, asyncio.CancelledError):
                pass
        # Nuclear kill: force-close all TCP connections to VLLM endpoints

        # Reset all in-memory state
        cls._cycle_task = None
        cls._stop_requested = False
        cls._state = PipelineStateDB.default_state()
        cls.save_state()
        return {"status": "idle"}


pipeline_service = PipelineService()
