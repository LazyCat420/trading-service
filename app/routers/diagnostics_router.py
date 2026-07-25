from fastapi import APIRouter, Depends, HTTPException
from app.log_manager import log_manager
from typing import Optional
from app.db.connection import get_db

router = APIRouter(prefix="/api/diagnostics", tags=["diagnostics"])

@router.get("/cycles")
def list_cycles():
    """List all available cycles."""
    return {"cycles": log_manager.list_all_cycles()}


@router.get("/system-jobs")
def list_system_jobs():
    """Jobs registered directly in the APScheduler engine — including the
    6:30 AM PT weekday trading run.

    These are invisible in the schedules UI because they are registered
    programmatically in `SchedulerService.start()` and never written to
    `cycle_schedules` (which is agent-created schedules only, and deliberately
    so — see `SchedulerService.list_system_jobs` for why merging the two would
    silently throttle the agent research budget).

    Read-only: system jobs are defined in code, so there is nothing here to
    edit or delete. `market_open_cycle` is the answer to "what starts the
    morning run?".
    """
    try:
        from app.services.cycle_scheduler import SchedulerService, scheduler

        if not scheduler.running:
            # A stopped engine and an engine with no jobs look identical in a
            # bare list; say which it is.
            return {"engine_running": False, "count": 0, "jobs": [],
                    "note": "scheduler engine is not running — no system jobs are armed"}
        jobs = SchedulerService.list_system_jobs()
        return {
            "engine_running": True,
            "count": len(jobs),
            "paused_count": sum(1 for j in jobs if j["paused"]),
            "jobs": jobs,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"could not read scheduler engine: {e}")

@router.get("/logs")
def get_cycle_logs(cycle_id: str):
    """Fetch logs for a specific cycle."""
    logs = log_manager.get_cycle_log(cycle_id)
    if not logs:
        raise HTTPException(status_code=404, detail="Cycle logs not found")
    return {"cycle_id": cycle_id, "logs": logs}

@router.get("/errors")
def get_cycle_errors(cycle_id: str):
    """Fetch only error logs for a specific cycle."""
    errors = log_manager.get_cycle_errors(cycle_id)
    return {"cycle_id": cycle_id, "errors": errors}

@router.get("/stats")
def get_cycle_stats(cycle_id: str):
    """Fetch aggregated stats for a cycle."""
    stats = log_manager.get_cycle_stats(cycle_id)
    if stats.get("status") == "not_found":
        raise HTTPException(status_code=404, detail="Cycle stats not found")
    return stats
