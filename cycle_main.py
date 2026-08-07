"""
Trading Cycle Backend — Standalone cycle worker.
Uses the V3 Agentic Linear Pipeline.
"""

import asyncio
import argparse
import logging
import os
import signal
import sys
import time
import json
import uvicorn
from fastapi import FastAPI

local_dir = os.path.dirname(os.path.abspath(__file__))
if local_dir not in sys.path:
    sys.path.insert(0, local_dir)

logger = logging.getLogger("cycle_backend")


def _worker_identity() -> str:
    """Which instance is this? Stamped on every command claim.

    Any process pointed at the shared database is an equal claimant for queued
    cycles (`FOR UPDATE SKIP LOCKED` is atomic but instance-blind). On
    2026-08-05 a stale local container six weeks behind master silently took
    two scheduled cycles from the NAS and killed both; the logs named no
    instance, so answering "who ran this cycle" meant diffing container code
    against master by hand. Cheap identity here is the whole diagnosis.
    """
    import socket
    return f"{os.environ.get('WORKER_NAME') or socket.gethostname()}/{os.environ.get('GIT_SHA', 'unknown-build')}"


WORKER_ID = _worker_identity()

async def run_single_cycle(
    tickers: list[str] | None = None,
    cycle_id: str = "",
    bot_id: str = "cycle-backend",
) -> dict:
    from app.services.boot_service import BootService
    from app.services.pipeline_service import PipelineService
    
    if not cycle_id:
        cycle_id = f"cycle-v3-{int(time.time())}"

    await BootService.startup()
    
    if not tickers:
        tickers = ["AAPL"] # fallback

    logger.info("[cycle_backend] Starting V3 cycle %s | tickers=%s", cycle_id, tickers)
    t0 = time.monotonic()

    try:
        await PipelineService.start_cycle(tickers=tickers, cycle_id=cycle_id)
        if PipelineService._cycle_task:
            await PipelineService._cycle_task
            
        elapsed = time.monotonic() - t0
        logger.info("[cycle_backend] V3 Cycle %s completed in %.1fs", cycle_id, elapsed)
        return {"status": "done", "cycle_id": cycle_id}
    except Exception as e:
        elapsed = time.monotonic() - t0
        logger.error("[cycle_backend] Cycle %s failed after %.1fs: %s", cycle_id, elapsed, e)
        return {"cycle_id": cycle_id, "status": "error", "error": str(e)}
    finally:
        await BootService.shutdown()

_background_tasks = set()

def track_task(coro):
    task = asyncio.create_task(coro)
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
    return task

def drain_schedule_refreshes():
    """Consume pending REFRESH_SCHEDULE commands from system_commands.

    The schedule CRUD (trading-client) and agent scheduling tools enqueue
    REFRESH_SCHEDULE after mutating cycle_schedules. The scheduler engine
    lives in this process, so this is the only place that can re-register
    jobs — without this drain, schedules created after boot stay dormant
    until the next restart.
    """
    from app.db.connection import get_db
    from app.services.cycle_scheduler import SchedulerService

    rows = []
    with get_db() as db:
        with db.transaction():
            rows = db.execute(
                "SELECT id, payload FROM system_commands "
                "WHERE status = 'pending' AND command_type = 'REFRESH_SCHEDULE' "
                "ORDER BY created_at ASC LIMIT 20 FOR UPDATE SKIP LOCKED"
            ).fetchall()
            for cmd_id, _ in rows:
                db.execute(
                    "UPDATE system_commands SET status = 'completed', completed_at = CURRENT_TIMESTAMP WHERE id = %s",
                    [cmd_id],
                )

    for cmd_id, payload_val in rows:
        try:
            payload = json.loads(payload_val) if isinstance(payload_val, str) else (payload_val or {})
            job_id = payload.get("job_id")
            if job_id:
                SchedulerService.refresh_job(job_id)
                logger.info("[cycle_backend] Refreshed schedule %s (cmd %s)", job_id, cmd_id)
            else:
                SchedulerService.load_all_schedules()
                logger.info("[cycle_backend] Reloaded all schedules (cmd %s)", cmd_id)
        except Exception as e:
            logger.error("[cycle_backend] REFRESH_SCHEDULE %s failed: %s", cmd_id, e)


async def poll_system_commands(shutdown: asyncio.Event):
    from app.db.connection import get_db
    logger.info("[cycle_backend] Started system commands poller for V3. worker=%s", WORKER_ID)
    
    while not shutdown.is_set():
        try:
            job_id, cmd_type, payload_val = None, None, None
            with get_db() as db:
                with db.transaction():
                    row = db.execute(
                        "SELECT id, command_type, payload FROM v3_system_commands "
                        "WHERE status = 'pending' "
                        "ORDER BY created_at ASC "
                        "LIMIT 1 FOR UPDATE SKIP LOCKED"
                    ).fetchone()
                    
                    if row:
                        job_id, cmd_type, payload_val = row
                        db.execute(
                            "UPDATE v3_system_commands SET status = 'running', started_at = CURRENT_TIMESTAMP WHERE id = %s", 
                            [job_id]
                        )
            
            if job_id:
                try:
                    payload = json.loads(payload_val) if isinstance(payload_val, str) else (payload_val or {})
                    result = None
                    logger.info(
                        "[cycle_backend] Processing command %s (%s) | claimed by worker=%s",
                        cmd_type, job_id, WORKER_ID,
                    )
                    
                    if cmd_type in ("START_CYCLE", "START_V3_CYCLE"):
                        from app.services.pipeline_service import PipelineService
                        from lazycat.llm import prism_client
                        prism_client.reset_kill_switch()
                        
                        kwargs = {k: v for k, v in payload.items() if k not in ("tickers", "cycle_id")}
                        result = await PipelineService.start_cycle(
                            tickers=payload.get("tickers", []),
                            cycle_id=payload.get("cycle_id"),
                            **kwargs
                        )
                    elif cmd_type == "STOP_CYCLE":
                        from app.services.pipeline_service import PipelineService
                        if payload.get("fast"):
                            # Non-blocking: set flag + cancel task, return immediately
                            result = PipelineService.request_stop()
                        else:
                            # Blocking: wait up to 5s for task to finish
                            result = await PipelineService.stop_cycle()
                    elif cmd_type == "FORCE_RESET":
                        from app.services.pipeline_service import PipelineService
                        result = await PipelineService.force_reset()
                    elif cmd_type in ("PAUSE_CYCLE", "RESUME_CYCLE"):
                        # V3 pipeline doesn't support pause/resume — complete the
                        # command cleanly instead of leaving it as 'running' forever.
                        result = {"status": "not_supported", "message": f"{cmd_type} not supported in V3"}
                    elif cmd_type == "FLASH_BRIEFING":
                        from app.services.flash_briefing import generate_flash_briefing
                        await generate_flash_briefing()
                        result = {"status": "ok"}
                    elif cmd_type == "DISCARD_CHECKPOINT":
                        # intentional no-op
                        result = {"status": "ok", "message": "No checkpoint system active"}
                    elif cmd_type == "FORCE_CHECKPOINT":
                        # intentional no-op
                        result = {"status": "ok", "message": "No checkpoint system active"}
                    else:
                        logger.error("[cycle_backend] Ignored legacy command type '%s'", cmd_type)
                        result = {"status": "ignored", "message": "Legacy command ignored in V3"}

                    # Status-truth: 'completed' must mean the command DID its
                    # work. A START_CYCLE bounced off an already-running cycle
                    # (or stuck state) used to be silently marked completed —
                    # the requested cycle never ran and nothing recorded that.
                    cmd_status = "completed"
                    cmd_note = None
                    if isinstance(result, dict) and result.get("status") in ("deduplicated", "error", "ignored"):
                        cmd_status = "skipped"
                        cmd_note = str(result.get("message") or result.get("status"))[:300]
                        logger.warning(
                            "[cycle_backend] Command %s SKIPPED (not executed): %s",
                            job_id, cmd_note,
                        )
                    with get_db() as db:
                        db.execute(
                            "UPDATE v3_system_commands SET status = %s, completed_at = CURRENT_TIMESTAMP, "
                            "result = %s, error_message = %s WHERE id = %s",
                            [cmd_status, json.dumps(result), cmd_note, job_id]
                        )
                except asyncio.CancelledError as e:
                    if shutdown.is_set():
                        raise
                    logger.error("[cycle_backend] Command %s cancelled internally: %s", job_id, e)
                    with get_db() as db:
                        db.execute(
                            "UPDATE v3_system_commands SET status = 'error', completed_at = CURRENT_TIMESTAMP, error_message = %s WHERE id = %s", 
                            [f"Cancelled internally: {e}", job_id]
                        )
                except BaseException as e:
                    logger.error("[cycle_backend] Command %s failed: %s", job_id, e)
                    with get_db() as db:
                        db.execute(
                            "UPDATE v3_system_commands SET status = 'error', completed_at = CURRENT_TIMESTAMP, error_message = %s WHERE id = %s", 
                            [str(e), job_id]
                        )
        except BaseException as e:
            if isinstance(e, asyncio.CancelledError):
                raise
            logger.exception("[cycle_backend] Unexpected error in command poller loop")

        try:
            drain_schedule_refreshes()
        except Exception:
            logger.exception("[cycle_backend] Schedule refresh drain failed")

        try:
            await asyncio.wait_for(shutdown.wait(), timeout=1.0)
        except asyncio.TimeoutError:
            pass

async def run_worker(tickers: list[str] | None = None, shutdown_event: asyncio.Event | None = None):
    from app.services.boot_service import BootService
    
    if shutdown_event is None:
        shutdown = asyncio.Event()
        def _request_shutdown():
            shutdown.set()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, _request_shutdown)
            except NotImplementedError:
                signal.signal(sig, lambda s, f: _request_shutdown())
    else:
        shutdown = shutdown_event

    await BootService.startup()
    poller_task = asyncio.create_task(poll_system_commands(shutdown))
    
    # Run autoresearch poller task concurrently
    autoresearch_task = None
    try:
        from app.autoresearch.eval_worker import poll_system_commands as poll_autoresearch_commands
        autoresearch_task = asyncio.create_task(poll_autoresearch_commands())
        logger.info("[cycle_backend] Started Autoresearch poller task.")
    except Exception as e:
        logger.error("[cycle_backend] Failed to start Autoresearch poller: %s", e)

    await shutdown.wait()
    
    from app.services.pipeline_service import PipelineService
    if PipelineService._cycle_task and not PipelineService._cycle_task.done():
        logger.info("[cycle_backend] Shutting down: stopping active cycle...")
        try:
            await asyncio.wait_for(PipelineService.stop_cycle(), timeout=5.0)
        except Exception as e:
            logger.error("[cycle_backend] Error stopping cycle on shutdown: %s", e)

    poller_task.cancel()
    if autoresearch_task:
        autoresearch_task.cancel()
        
    try:
        await poller_task
    except asyncio.CancelledError:
        pass
        
    if autoresearch_task:
        try:
            await autoresearch_task
        except asyncio.CancelledError:
            pass

    await BootService.shutdown()

async def start_health_server(shutdown_event: asyncio.Event):
    from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
    from fastapi import Depends, HTTPException
    from app.config import settings

    security = HTTPBearer()

    def verify_api_key(credentials: HTTPAuthorizationCredentials = Depends(security)):
        if credentials.credentials != settings.API_SERVER_KEY:
            raise HTTPException(status_code=403, detail="Invalid API Server Key")
        return credentials.credentials

    app = FastAPI(title="Trading Cycle Backend Health")
    @app.get("/health")
    def health():
        return {"status": "ok", "service": "trading-service", "version": "v3"}

    @app.get("/status")
    def status(summary_only: bool = False, token: str = Depends(verify_api_key)):
        from app.services.pipeline_service import PipelineService
        return PipelineService.get_current_state(summary_only=summary_only)

    try:
        from app.routers.vllm_router import router as vllm_router
        from app.routers.agent_persona_router import router as agent_persona_router
        from app.routers.agent_tools_router import router as agent_tools_router
        from app.routers.chat_router import router as chat_router
        from app.routers.debug_router import router as debug_router
        from app.routers.diagnostics_router import router as diagnostics_router
        from app.routers.node_health_router import router as node_health_router
        from app.routers.verdict_router import router as verdict_router
        from app.routers.chart_router import router as chart_router
        from app.routers.market_router import router as market_router
        from app.routers.cycle_replay_router import router as cycle_replay_router
        from app.routers.challenger_router import router as challenger_router
        from app.routers.eval_trust_router import router as eval_trust_router
        from app.routers.component_health_router import router as component_health_router
        from app.routers.research_firm_router import router as research_firm_router
        # The scraper was extracted back into the standalone scraper-service (:8001);
        # trading-service no longer SERVES /scrape, /collect, /stream. Its own
        # scraping now goes out over HTTP via app.services.scraper_client. The
        # app.scraper source still lives here only so scraper-service can build-copy
        # it — it is not imported or run in this process anymore.

        app.include_router(vllm_router)
        app.include_router(agent_persona_router)
        app.include_router(agent_tools_router)
        app.include_router(chat_router)
        app.include_router(debug_router)
        app.include_router(diagnostics_router)
        app.include_router(node_health_router)
        app.include_router(verdict_router)
        app.include_router(chart_router)
        app.include_router(market_router)
        app.include_router(cycle_replay_router)
        app.include_router(challenger_router)
        app.include_router(eval_trust_router)
        app.include_router(component_health_router)
        app.include_router(research_firm_router)
    except Exception as e:
        logger.error(f"Failed to include routers: {e}")

    config = uvicorn.Config(app, host="0.0.0.0", port=8080, log_level="error")
    server = uvicorn.Server(config)
    
    async def _serve():
        try:
            await server.serve()
        except asyncio.CancelledError:
            pass

    task = asyncio.create_task(_serve())
    await shutdown_event.wait()
    server.should_exit = True
    await task

def setup_error_capture():
    """Attach the DB error logger to the root logger, or die loudly.

    Must run AFTER logging.basicConfig(force=True) — force=True strips every
    existing root handler, so an earlier registration would be silently undone.
    Until 2026-08-02 registration only happened as a side effect of a lazy
    HTTP-path import; every container since the 07-31 deploy ran with zero
    error capture (execution_errors/cycle_audit_log empty while 795 WARN/ERR
    lines went to stdout in 30h).
    """
    from app.services.logging import setup_db_logger
    from app.services.logging.unified_logger import DbLoggingHandler

    setup_db_logger()
    if not any(isinstance(h, DbLoggingHandler) for h in logging.getLogger().handlers):
        raise RuntimeError("DB error logger failed to register on the root logger")
    # Canary row: one WARNING per boot proves the capture path end-to-end.
    logger.warning("db-logger-canary: DB error capture registered at boot")


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
        force=True,
        handlers=[logging.StreamHandler(sys.stdout)],
    )
    sys.stdout.reconfigure(line_buffering=True)
    setup_error_capture()
    logger.info("Trading Cycle Backend starting (V3 pipeline)...")

    ap = argparse.ArgumentParser(description="Trading Cycle Backend")
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--tickers", type=str)
    args = ap.parse_args()

    tickers = [t.strip() for t in args.tickers.split(",")] if args.tickers else None

    if args.once:
        result = asyncio.run(run_single_cycle(tickers=tickers))
        print(f"Result: {result}")
    else:
        async def _run_all():
            shutdown = asyncio.Event()
            def _request_shutdown():
                shutdown.set()
            loop = asyncio.get_running_loop()
            for sig in (signal.SIGINT, signal.SIGTERM):
                try:
                    loop.add_signal_handler(sig, _request_shutdown)
                except NotImplementedError:
                    pass
            worker_task = asyncio.create_task(run_worker(tickers=tickers, shutdown_event=shutdown))
            health_task = asyncio.create_task(start_health_server(shutdown))
            await asyncio.gather(worker_task, health_task)
        try:
            asyncio.run(_run_all())
        except KeyboardInterrupt:
            pass

if __name__ == "__main__":
    main()
