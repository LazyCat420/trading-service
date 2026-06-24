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

async def poll_system_commands(shutdown: asyncio.Event):
    from app.db.connection import get_db
    logger.info("[cycle_backend] Started system commands poller for V3.")
    
    while not shutdown.is_set():
        try:
            job_id, cmd_type, payload_val = None, None, None
            with get_db() as db:
                with db.transaction():
                    row = db.execute(
                        "SELECT id, command_type, payload FROM system_commands "
                        "WHERE status = 'pending' "
                        "ORDER BY created_at ASC "
                        "LIMIT 1 FOR UPDATE SKIP LOCKED"
                    ).fetchone()
                    
                    if row:
                        job_id, cmd_type, payload_val = row
                        db.execute(
                            "UPDATE system_commands SET status = 'running', started_at = CURRENT_TIMESTAMP WHERE id = %s", 
                            [job_id]
                        )
            
            if job_id:
                try:
                    payload = json.loads(payload_val) if isinstance(payload_val, str) else (payload_val or {})
                    result = None
                    logger.info("[cycle_backend] Processing command %s (%s)", cmd_type, job_id)
                    
                    if cmd_type in ("START_CYCLE", "START_V3_CYCLE"):
                        from app.services.pipeline_service import PipelineService
                        result = await PipelineService.start_cycle(
                            tickers=payload.get("tickers", []),
                            cycle_id=payload.get("cycle_id"),
                        )
                    elif cmd_type == "STOP_CYCLE":
                        from app.services.pipeline_service import PipelineService
                        result = await PipelineService.stop_cycle()
                    else:
                        logger.error("[cycle_backend] Ignored legacy command type '%s'", cmd_type)
                        result = {"status": "ignored", "message": "Legacy command ignored in V3"}

                    with get_db() as db:
                        db.execute(
                            "UPDATE system_commands SET status = 'completed', completed_at = CURRENT_TIMESTAMP, result = %s WHERE id = %s", 
                            [json.dumps(result), job_id]
                        )
                except asyncio.CancelledError:
                    raise
                except BaseException as e:
                    logger.error("[cycle_backend] Command %s failed: %s", job_id, e)
                    with get_db() as db:
                        db.execute(
                            "UPDATE system_commands SET status = 'error', completed_at = CURRENT_TIMESTAMP, error_message = %s WHERE id = %s", 
                            [str(e), job_id]
                        )
        except BaseException as e:
            if isinstance(e, asyncio.CancelledError):
                raise
            
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
    await shutdown.wait()
    
    poller_task.cancel()
    try:
        await poller_task
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

def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
        force=True,
        handlers=[logging.StreamHandler(sys.stdout)],
    )
    sys.stdout.reconfigure(line_buffering=True)
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
