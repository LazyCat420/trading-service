"""Boot Service — Orchestrates application startup and shutdown.

Startup Sequence:
1. DB Connection & Schema (Required)
2. Vector Store Indexes (Required)
3. Reset Application State (Required)
4. Restore Stable Fixes (Optional)
5. Crash Recovery Scan (Optional)
6. Scheduler Start / Embedding Warmup (Optional)
7. Prism V3 Agent Registration (Optional)
   (lazy-tool-service's own MCP registration is NOT done here — it self-registers)
8. Background Tasks (FRED, SP500, Market Regime, Audit Worker)

Shutdown Sequence (Reverse Order):
1. Cancel Running Trading Cycle
2. Close vLLM HTTP Client
3. Stop Audit Worker
4. Close PostgreSQL Connection Pool
"""
import asyncio
import logging
import time

logger = logging.getLogger(__name__)


class BootService:
    @classmethod
    async def startup(cls):
        """Main startup sequence coordinator."""
        # --- Configure SDK client routing ---
        from app.config.config import settings
        from lazycat.llm import prism_client
        if settings.PRISM_ENABLED:
            prism_client.url = settings.PRISM_URL
        else:
            # lazy-tool-service's external (host-mapped) port is 5591
            prism_client.url = f"http://{settings.DEFAULT_HOST}:5591"
        logger.info("[Boot] Configured prism_client.url: %s (PRISM_ENABLED=%s)", prism_client.url, settings.PRISM_ENABLED)

        logger.info("[Boot] Starting application boot sequence...")

        # --- Required Boot Stages ---
        cls._run_stage("DB Connection & Schema", cls._init_database, required=True)
        cls._run_stage("Vector Store Indexes", cls._init_vector_indices, required=True)
        cls._run_stage("Reset Application State", cls._reset_app_state, required=True)
        cls._run_stage("Restore Stable Fixes", cls._restore_stable_fixes, required=False)

        # --- Crash Recovery Detection ---
        cls._run_stage("Crash Recovery Scan", cls._detect_crashed_cycles, required=False)

        # --- Optional / Degraded Boot Stages ---
        cls._run_stage("Scheduler Start", cls._start_scheduler, required=False)
        cls._run_stage("Embedding Warmup", cls._warmup_models, required=False)
        # NOTE: lazy-tool-service's MCP registration used to happen here — this
        # service wrote Prism's `mcp_servers` collection directly, for three
        # scopes including html-notes-client. That made unrelated apps' tool
        # sets depend on the trading bot booting, and nothing re-connected the
        # SSE link when lazy-tool-service itself redeployed. It now registers
        # itself over Prism's REST API on its own boot
        # (lazy-tool-service/src/services/PrismRegistrationService.ts).
        cls._run_stage("Register V3 Prism Agents", cls._register_v3_agents, required=False)

        # --- Absorbed scraper: shared httpx session ---
        # The folded-in scraper engines/collectors (app.scraper) share one httpx
        # client. Initialize it here for proxy/UA config + clean teardown; the
        # session self-initializes lazily on first use if this stage is skipped.
        try:
            from app.scraper.core.session_manager import session_manager
            await session_manager.startup()
            logger.info("[Boot] Scraper shared httpx session initialized.")
        except Exception as e:
            logger.warning("[Boot] Scraper session init failed (non-fatal, will lazy-init): %s", e)

        # --- Background Tasks ---
        # Spawns a background, non-blocking task for long-running startup data refreshes
        asyncio.create_task(cls._start_background_tasks())

        logger.info("[Boot] Application boot sequence completed successfully.")

    @classmethod
    def _restore_stable_fixes(cls):
        """Load and restore all evolved stable fixes from stable_harnesses to disk."""
        try:
            from app.db.connection import get_db
            from app.cognition.evolution.target_map import resolve_target
            from pathlib import Path

            logger.info("[Boot] Restoring evolved stable fixes from stable_harnesses...")
            with get_db() as db:
                rows = db.execute(
                    "SELECT target_type, target_name, stable_content FROM stable_harnesses"
                ).fetchall()

                restored_count = 0
                for r_type, r_name, content in rows:
                    target_info = resolve_target(r_type, r_name)
                    file_path = target_info.get("file_path")
                    if file_path:
                        path = Path(file_path)
                        # Read current disk content if exists
                        current_disk_content = ""
                        if path.exists():
                            try:
                                current_disk_content = path.read_text(encoding="utf-8")
                            except Exception:
                                pass

                        if current_disk_content != content:
                            # Ensure parent directories exist
                            path.parent.mkdir(parents=True, exist_ok=True)
                            path.write_text(content, encoding="utf-8")
                            logger.info("[Boot] Restored stable fix for %s/%s to %s", r_type, r_name, file_path)
                            restored_count += 1
                
                logger.info("[Boot] Restored %d stable fixes.", restored_count)
        except Exception as e:
            logger.warning("[Boot] Failed to restore stable fixes (non-fatal): %s", e)

    @classmethod
    async def shutdown(cls):
        """Main shutdown sequence coordinator."""
        logger.info("[Boot] Shutting down...")

        # Cancel any running trading cycle
        try:
            from app.services.pipeline_service import PipelineService

            await PipelineService.stop_cycle()
        except Exception as e:
            logger.warning("[Boot] Cycle cancellation on shutdown: %s", e)

        # Stop cycle scheduler (removed in V3)

        # Close the vLLM HTTP client
        try:
            from app.services.prism_agent_caller import llm

            await llm.close()
        except Exception as e:
            logger.warning("[Boot] vLLM client close: %s", e)

        # Close the absorbed scraper's shared httpx session
        try:
            from app.scraper.core.session_manager import session_manager

            await session_manager.shutdown()
        except Exception as e:
            logger.warning("[Boot] Scraper session close: %s", e)

        # Stop audit worker
        try:
            from app.monitoring.audit_worker import stop_audit_worker
            await stop_audit_worker()
        except Exception as e:
            logger.warning("[Boot] Audit worker shutdown: %s", e)

        # Close PostgreSQL connection pool
        try:
            from app.db.connection import close_db

            close_db()
            logger.info("[Boot] PostgreSQL connection pool closed.")
        except Exception as e:
            logger.warning("[Boot] PostgreSQL close: %s", e)

        logger.info("[Boot] Shutdown complete.")

    @classmethod
    def _run_stage(cls, name: str, stage_func, required: bool = True):
        t0 = time.perf_counter()
        try:
            stage_func()
            elapsed_ms = (time.perf_counter() - t0) * 1000
            logger.info(f"[Boot] Stage '{name}' completed in {elapsed_ms:.1f}ms")
        except Exception as e:
            elapsed_ms = (time.perf_counter() - t0) * 1000
            if required:
                logger.error(
                    f"[Boot] Stage '{name}' FAILED in {elapsed_ms:.1f}ms: {e}. Aborting boot."
                )
                raise e
            else:
                logger.warning(
                    f"[Boot] Stage '{name}' FAILED in {elapsed_ms:.1f}ms: {e}. Proceeding in degraded mode."
                )

    # -------------------------------------------------------------------------
    # INDIVIDUAL STAGES
    # -------------------------------------------------------------------------

    @classmethod
    def _register_v3_agents(cls):
        from app.v3.prism_registration import register_v3_agents
        import asyncio
        loop = asyncio.get_event_loop()
        if loop.is_running():
            asyncio.create_task(register_v3_agents())
        else:
            loop.run_until_complete(register_v3_agents())

    @classmethod
    def _init_database(cls):
        from app.db.connection import get_db
        from app.db.mongo import init_mongo_schema

        get_db()
        init_mongo_schema()

    @classmethod
    def _init_vector_indices(cls):
        # pgvector HNSW + FTS indexes are created in schema_pg.sql
        pass

    @classmethod
    def _reset_app_state(cls):
        from app.db.connection import get_db
        try:
            with get_db() as db:
                db.execute(
                    "UPDATE pipeline_state SET status = 'error', error = 'Container restarted unexpectedly' "
                    "WHERE singleton_id = 'current' AND status IN ('running', 'blocked', 'starting')"
                )
                db.execute(
                    "UPDATE v3_system_commands SET status = 'error', error_message = 'Container restarted unexpectedly' "
                    "WHERE status IN ('running', 'pending')"
                )
                db.execute(
                    "UPDATE system_commands SET status = 'error', error_message = 'Container restarted unexpectedly' "
                    "WHERE status IN ('running', 'pending')"
                )
        except Exception as e:
            logger.error("[Boot] Failed to reset stuck pipeline state on boot: %s", e)

        # Reset any zombie-state pruned tools from the ToolOptimizer.
        # Prism-routed agents never reported tool usage, causing all tools to
        # get pruned after 4+ cycles. This clears that state on every boot.
        try:
            from app.services.tool_optimizer import reset_all_pruned
            reset_all_pruned()
        except Exception as e:
            logger.warning("[Boot] Failed to reset pruned tools (non-fatal): %s", e)

        # Start the system PAUSED by default on boot.
        # This prevents all scheduled LLM tasks (morning briefing, flash briefing,
        # janitor, eval worker, etc.) from firing until the user explicitly starts
        # a trading run or resumes via the UI.
        # Override with START_PAUSED=false in env to auto-start.
        import os
        start_paused = os.getenv("START_PAUSED", "true").lower() in ("true", "1", "yes")
        if start_paused:
            logger.info("[Boot] System starts PAUSED — LLM tasks gated until user resumes or starts a cycle.")

    @classmethod
    def _detect_crashed_cycles(cls):
        """Scan cycle logs for incomplete cycles from previous container runs."""
        from app.log_manager import log_manager

        crashed = log_manager.detect_and_log_crashed_cycles(max_age_hours=48)
        if crashed:
            logger.warning(
                "[Boot] CRASH RECOVERY: Found %d interrupted cycle(s) from previous runs:",
                len(crashed),
            )
            for c in crashed:
                logger.warning(
                    "[Boot]   → %s: last_step=%s, last_ticker=%s, "
                    "%d/%d tickers abandoned",
                    c["cycle_id"],
                    c["last_step"],
                    c.get("last_ticker", "?"),
                    len(c.get("abandoned", [])),
                    c.get("total_tickers", 0),
                )
        else:
            logger.info("[Boot] No crashed cycles detected from previous runs.")

        # Clean up old log files (>14 days) to prevent unbounded disk growth
        from app.config import settings
        max_days = getattr(settings, "AUDIT_LOG_TTL_DAYS", 14)
        cleanup = log_manager.cleanup_old_logs(max_age_days=max_days)
        if cleanup["cycle_logs"] or cleanup["audit_logs"]:
            logger.info(
                "[Boot] Log cleanup: removed %d cycle + %d audit files (%.1f KB freed)",
                cleanup["cycle_logs"], cleanup["audit_logs"],
                cleanup["bytes_freed"] / 1024,
            )

    @classmethod
    def _start_scheduler(cls):
        # Revive the APScheduler engine. This runs inside the async boot
        # sequence (BootService.startup is awaited from cycle_main.run_worker),
        # so the AsyncIOScheduler has a live event loop to attach to. Only the
        # cycle backend process calls BootService.startup(), so the scheduler
        # runs in exactly one process — the same one that consumes the
        # v3_system_commands queue it enqueues into.
        from app.services.cycle_scheduler import SchedulerService
        SchedulerService.start()

    @classmethod
    def _warmup_models(cls):
        from app.services.embedding_service import embedder

        embedder.embed_text("warmup")
        logger.info("[Boot] Embedding model loaded.")

    @classmethod
    async def _start_background_tasks(cls):
        """Run all startup data tasks sequentially.

        Tasks are run in sequence to avoid overwhelming external APIs
        during startup.
        """
        # Run vLLM model discovery first so that endpoints and models are resolved
        try:
            from app.services.startup_tasks import startup_vllm_discovery
            await startup_vllm_discovery()
        except Exception as e:
            logger.warning("[startup] vLLM model discovery failed: %s", e)

        try:
            await cls._startup_fred_refresh()
        except Exception as e:
            logger.warning("[startup] FRED task failed: %s", e)
        try:
            await cls._startup_market_collect()
        except Exception as e:
            logger.warning("[startup] Market task failed: %s", e)
        try:
            await cls._startup_sp500_seed()
        except Exception as e:
            logger.warning("[startup] SP500 task failed: %s", e)

        # Index recent news/analysis rows that lack an embedding so the
        # dense/hybrid retrievers have a corpus to search (idempotent, off-thread).
        try:
            from app.services.startup_tasks import startup_embedding_backfill

            await startup_embedding_backfill(lambda: False)
        except Exception as e:
            logger.warning("[startup] Embedding backfill task failed: %s", e)

        # Recurring full S&P 500 refresh — the seed above only ever runs once
        # (when price_history is empty). Without this, only the active
        # trading cycle's small watchlist gets new price_history rows, so
        # the market map's newest date silently degrades to a handful of
        # tickers instead of the full ~500. Runs forever; does not block
        # the rest of startup.
        asyncio.create_task(cls._sp500_daily_refresh_loop())

        # --- Agent Audit Worker ---
        try:
            from app.monitoring.audit_worker import start_audit_worker
            await start_audit_worker()
        except Exception as e:
            logger.warning("[startup] Audit worker failed to start (non-fatal): %s", e)

    @classmethod
    async def _startup_fred_refresh(cls):
        # Delegates to the canonical collector (batch executemany + DO UPDATE
        # for FRED revisions) — this used to carry a row-by-row DO NOTHING copy.
        # The daily 5 PM PT scheduler job is the primary refresh; boot only
        # backfills when the data is actually stale.
        await asyncio.sleep(3)  # let server fully boot first
        from app.services.startup_tasks import _is_data_fresh

        if (_is_data_fresh("macro_indicators", "source = 'fred'", 2)
                and _is_data_fresh(
                    "macro_indicators",
                    "source = 'fred' AND indicator = 'CPI'", 45)):
            logger.info("[startup] FRED data already fresh, skipping refresh")
            return
        logger.info("[startup] Refreshing FRED macro indicators (background thread)...")
        try:
            from app.collectors.fred_collector import sync_collect_fred
            total = await asyncio.to_thread(sync_collect_fred, lambda: False)
            logger.info("[startup] FRED refresh complete: %d total rows", total)
        except Exception as e:
            logger.warning("[startup] FRED refresh failed (non-fatal): %s", e)

    @classmethod
    async def _startup_market_collect(cls):
        """Background: collect market regime data (indexes, VIX, yields, ETFs)."""
        from app.db.connection import get_db

        with get_db() as db:
            # Skip if we already have recent data
            recent = db.execute(
                "SELECT COUNT(*) FROM asset_prices WHERE date >= CURRENT_DATE - INTERVAL '1 day'"
            ).fetchone()[0]
            if recent > 50:
                logger.info(
                    "[startup] Market data already fresh (%d recent rows), skipping",
                    recent,
                )
                return
            logger.info("[startup] Collecting market regime data (background)...")
            try:
                from app.collectors.market_regime_collector import collect_market_data

                result = await collect_market_data(period="6mo")
                logger.info(
                    "[startup] Market data collected: %s", result.get("total", 0)
                )
                # Compute regime + breadth
                from app.data.market_regime_engine import (
                    compute_market_regime,
                    compute_sector_breadth,
                )

                await compute_market_regime()
                await compute_sector_breadth()
            except Exception as e:
                logger.warning("[startup] Market collect failed (non-fatal): %s", e)

    @classmethod
    async def _startup_sp500_seed(cls):
        """Background: seed SP500 universe + prices if DB is empty."""
        from app.db.connection import get_db

        with get_db() as db:
            sp500_count = db.execute(
                "SELECT COUNT(*) FROM ticker_metadata WHERE sp500=TRUE"
            ).fetchone()[0]
            if sp500_count > 400:
                logger.info(
                    "[startup] SP500 universe already loaded (%d tickers)", sp500_count
                )
                # Check if price data exists
                price_count = db.execute(
                    "SELECT COUNT(*) FROM price_history"
                ).fetchone()[0]
                if price_count == 0:
                    logger.info(
                        "[startup] No price data — collecting SP500 prices (background)..."
                    )
                    try:
                        from app.data.sp500_price_collector import collect_sp500_prices

                        price_result = await collect_sp500_prices(period="6mo")
                        logger.info(
                            "[startup] SP500 prices collected: %s",
                            price_result.get("total", 0),
                        )
                    except Exception as e:
                        logger.warning("[startup] Price collection failed: %s", e)
                # Compute sector analytics if missing
                perf_count = db.execute(
                    "SELECT COUNT(*) FROM sector_performance"
                ).fetchone()[0]
                if perf_count == 0:
                    logger.info("[startup] Computing sector analytics...")
                    try:
                        from app.data.sector_aggregator import (
                            compute_sector_performance,
                            backfill_sector_performance,
                        )

                        await backfill_sector_performance()
                        await compute_sector_performance()
                    except Exception as e:
                        logger.warning("[startup] Sector compute failed: %s", e)
                return
            logger.info("[startup] Seeding SP500 universe (background)...")
            try:
                from app.data.sp500_universe import load_sp500_universe

                result = await load_sp500_universe(enrich=False)
                logger.info("[startup] SP500 universe loaded: %s", result)
                # Collect prices in background
                from app.data.sp500_price_collector import collect_sp500_prices

                price_result = await collect_sp500_prices(period="6mo")
                logger.info(
                    "[startup] SP500 prices collected: %s", price_result.get("total", 0)
                )
                # Compute sector analytics
                from app.data.sector_aggregator import (
                    compute_sector_performance,
                    backfill_sector_performance,
                )

                await backfill_sector_performance()
                await compute_sector_performance()
            except Exception as e:
                logger.warning("[startup] SP500 seed failed (non-fatal): %s", e)

    @classmethod
    async def _sp500_full_refresh(cls, period: str):
        """One shot: top up price_history for all S&P 500 tickers + recompute sector aggregates."""
        from app.data.sp500_price_collector import collect_sp500_prices
        from app.data.sector_aggregator import (
            compute_sector_performance,
            backfill_sector_performance,
        )

        price_result = await collect_sp500_prices(period=period)
        logger.info(
            "[sp500-refresh] Prices refreshed: %s rows", (price_result or {}).get("total", 0)
        )
        await backfill_sector_performance()
        await compute_sector_performance()

    @classmethod
    async def _sp500_daily_refresh_loop(cls):
        """Recurring background task: keep the full S&P 500 price_history fresh.

        _startup_sp500_seed only ever runs once (when price_history is empty
        at boot). After that, only the active trading cycle's small watchlist
        writes new rows, so most of the S&P 500 universe silently goes stale.
        This loop tops up ALL sp500 tickers once after boot, then again daily
        after market close.
        """
        from app.db.connection import get_db
        from app.services.market_calendar import MarketCalendar

        await asyncio.sleep(10)  # let boot settle first

        try:
            with get_db() as db:
                today_count = db.execute(
                    "SELECT COUNT(*) FROM price_history WHERE date = CURRENT_DATE"
                ).fetchone()[0]
            if today_count < 400:
                logger.info(
                    "[sp500-refresh] Only %d price_history rows for today — running immediate top-up",
                    today_count,
                )
                await cls._sp500_full_refresh(period="5d")
        except Exception as e:
            logger.warning("[sp500-refresh] Immediate top-up failed (non-fatal): %s", e)

        while True:
            try:
                next_run = MarketCalendar.get_next_window("post_close")
                now = MarketCalendar._to_et()
                sleep_seconds = max(60.0, (next_run - now).total_seconds())
                logger.info(
                    "[sp500-refresh] Next full refresh at %s ET (in %.1f hours)",
                    next_run.isoformat(), sleep_seconds / 3600,
                )
                await asyncio.sleep(sleep_seconds)
                await cls._sp500_full_refresh(period="5d")
            except Exception as e:
                logger.warning("[sp500-refresh] Daily refresh failed (will retry next cycle): %s", e)
                await asyncio.sleep(3600)  # back off an hour before recomputing the next window
