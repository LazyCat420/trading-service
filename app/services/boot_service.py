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
from app.db import mongo_query, mongo_store
from datetime import datetime, timezone

logger = logging.getLogger(__name__)


class BootService:
    # Set in shutdown(), read by long-lived background loops. Without it a
    # forever-loop keeps issuing work while the pool it writes to is being
    # closed, and the resulting errors read as failures rather than as a
    # shutdown in progress.
    _shutting_down: bool = False

    @classmethod
    def _is_shutting_down(cls) -> bool:
        return cls._shutting_down

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

        # The SDK is bind-mounted, not installed into the image, so its version
        # is NOT controlled by this service's deploy. Probe the attributes we
        # actually read before anything uses them — a stale mount degrades model
        # attribution silently rather than erroring. Capability only; this must
        # never care which models are loaded.
        cls._run_stage("SDK Capability Check", cls._check_sdk_capabilities, required=False)

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
            from app.cognition.evolution.target_map import resolve_target
            from pathlib import Path

            logger.info("[Boot] Restoring evolved stable fixes from stable_harnesses...")
            rows = mongo_query.find_rows('stable_harnesses', {}, ['target_type', 'target_name', 'stable_content'])

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
        cls._shutting_down = True

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

        # The PostgreSQL pool shutdown that stood here was deleted with
        # `app/db/connection.py` at teardown (2026-08-18), as its own comment
        # instructed. No module under `app/` opens a PG pool any more — the
        # driver lives only in `scripts/migration/`, whose scripts are
        # short-lived processes that close their own pool on exit. There is
        # nothing left for this process to close.

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
    def _check_sdk_capabilities(cls):
        from app.services.sdk_capabilities import assert_sdk_capabilities

        assert_sdk_capabilities()

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
        from app.db.mongo import init_mongo_schema

        init_mongo_schema()

    @classmethod
    def _init_vector_indices(cls):
        # Nothing to do at boot. The pgvector HNSW + FTS indexes this stage
        # documented were created by `schema_pg.sql`, which no longer runs;
        # `VectorStore._mongo_coll()` creates the Mongo equivalents lazily on
        # first use instead. Kept as a named no-op so the boot stage list still
        # reads as the full sequence.
        pass

    @classmethod
    def _reset_app_state(cls):
        try:
            mongo_store.update_docs('pipeline_state', {'singleton_id': 'current', 'status': {'$in': ['running', 'blocked', 'starting']}}, {'$set': {'status': 'error', 'error': 'Container restarted unexpectedly'}})
            mongo_store.update_docs('v3_system_commands', {'status': {'$in': ['running', 'pending']}}, {'$set': {'status': 'error', 'error_message': 'Container restarted unexpectedly'}})
            mongo_store.update_docs('system_commands', {'status': {'$in': ['running', 'pending']}}, {'$set': {'status': 'error', 'error_message': 'Container restarted unexpectedly'}})
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

        # These three live in app/services/startup_tasks.py, which is the one
        # owner. BootService used to carry its own copies; they had drifted --
        # the FRED copy passed SQL WHERE strings to a Mongo helper that takes a
        # dict (swallowed by a bare except, so the "already fresh" skip could
        # never fire and every restart re-collected), and the market copy
        # skipped the correlation/breadth compute entirely.
        from app.services.startup_tasks import (
            startup_fred_refresh,
            startup_market_collect,
            startup_sp500_seed,
        )

        try:
            await startup_fred_refresh(cls._is_shutting_down)
        except Exception as e:
            logger.warning("[startup] FRED task failed: %s", e)
        try:
            await startup_market_collect(cls._is_shutting_down)
        except Exception as e:
            logger.warning("[startup] Market task failed: %s", e)
        try:
            await startup_sp500_seed(cls._is_shutting_down)
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

        # --- News fact-extraction backfill (the Jetson's job) ---
        # The in-cycle extractor is bounded by a 22s per-cycle budget and is
        # outrun by the collection rate, so 95% of eligible articles had never
        # been extracted and were served to agents as raw scrape. This clears
        # that backlog on the spare box. Pinned to the Jetson: if it is down the
        # worker stops rather than failing over onto the cycle's box.
        try:
            from app.services.news_backfill import backfill_loop

            asyncio.create_task(backfill_loop(cls._is_shutting_down))
        except Exception as e:  # noqa: BLE001
            logger.warning("[startup] News backfill failed to start "
                           "(non-fatal): %s", e)

        # --- Agent Audit Worker ---
        try:
            from app.monitoring.audit_worker import start_audit_worker
            await start_audit_worker()
        except Exception as e:
            logger.warning("[startup] Audit worker failed to start (non-fatal): %s", e)

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
        """Recurring background task: keep the ANALYSED universe's prices fresh.

        startup_sp500_seed only ever runs once (when price_history is empty
        at boot). After that, only the active trading cycle's small watchlist
        writes new rows, so most of the universe silently goes stale.
        This loop tops up once after boot, then again daily after market close.

        Despite the name, `collect_sp500_prices` refreshes sp500 UNION watchlist
        UNION positions (2026-07-29): the cycle analyses the watchlist, and 127
        of 199 watchlist tickers are not in the S&P 500. Keeping only the index
        fresh meant the bot reasoned about a different set than it maintained.
        """
        from app.services.market_calendar import MarketCalendar

        await asyncio.sleep(10)  # let boot settle first

        try:
            today_count = mongo_query.agg_row('price_history', {'date': datetime.now(timezone.utc)}, [('count', None)])[0]
            # Threshold is derived from the actual refresh set, not a literal.
            # It was a hardcoded 400 against an sp500-only universe; now that the
            # set is sp500 UNION watchlist UNION positions (~636), a stale run
            # covering only the index would clear a fixed 400 and look healthy.
            # 75% catches a materially incomplete day without firing on the
            # handful of tickers yfinance always misses.
            # SQL's UNION (not UNION ALL) de-duplicates across all three
            # branches, so this is one set, not three counts summed. Nulls
            # cannot appear in a ticker column here, but drop them anyway so a
            # missing field cannot inflate the expected count by one.
            universe = set()
            for coll, q in (("ticker_metadata", {"sp500": True}),
                            ("watchlist", {}),
                            ("positions", {})):
                universe.update(
                    t for t in mongo_store.distinct_values(coll, "ticker", q)
                    if t is not None
                )
            expected = len(universe)
            threshold = int((expected or 500) * 0.75)
            if today_count < threshold:
                logger.info(
                    "[sp500-refresh] Only %d price_history rows for today "
                    "(expected ~%d, threshold %d) — running immediate top-up",
                    today_count, expected, threshold,
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
