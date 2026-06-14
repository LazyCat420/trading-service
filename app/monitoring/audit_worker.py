"""
Agent Audit Worker — Background task for continuous agent health monitoring.

Runs every 60 seconds during active trading cycles and checks:
  1. Connection pool health (leak detection)
  2. Memory usage trends (soak test)
  3. Output distribution shift detection (BUY/SELL/HOLD ratios)
  4. Prompt hash drift detection
  5. Stale audit warning aggregation

Wire this into the service startup:
    from app.monitoring.audit_worker import start_audit_worker
    asyncio.create_task(start_audit_worker())
"""

import asyncio
import logging
import os
import time
from collections import Counter
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

# ── Configuration ─────────────────────────────────────────────────────
POLL_INTERVAL_SECONDS = 60
DISTRIBUTION_SHIFT_THRESHOLD = 0.40  # 40% shift = warning
MEMORY_GROWTH_THRESHOLD_MB = 50     # 50MB growth over baseline = warning

# ── State ─────────────────────────────────────────────────────────────
_running = False
_baseline_memory_mb: float = 0.0
_baseline_connections: int = 0
_previous_distribution: dict[str, float] = {}
_worker_task: asyncio.Task | None = None


async def start_audit_worker():
    """Start the background audit worker. Safe to call multiple times."""
    global _worker_task, _running

    if _running:
        logger.debug("[AuditWorker] Already running, skipping duplicate start.")
        return

    _running = True
    _worker_task = asyncio.create_task(_audit_loop())
    logger.info("[AuditWorker] Background audit worker started (interval=%ds)", POLL_INTERVAL_SECONDS)


async def stop_audit_worker():
    """Gracefully stop the audit worker."""
    global _running, _worker_task

    _running = False
    if _worker_task and not _worker_task.done():
        _worker_task.cancel()
        try:
            await _worker_task
        except asyncio.CancelledError:
            pass
    _worker_task = None
    logger.info("[AuditWorker] Background audit worker stopped.")


async def _audit_loop():
    """Main audit loop — runs every POLL_INTERVAL_SECONDS."""
    global _baseline_memory_mb, _baseline_connections

    # Capture baseline on first run
    _baseline_memory_mb = _get_memory_mb()
    _baseline_connections = _get_pool_connection_count()

    logger.info(
        "[AuditWorker] Baseline: memory=%.1fMB, connections=%d",
        _baseline_memory_mb, _baseline_connections,
    )

    while _running:
        try:
            await _run_audit_checks()
        except Exception as e:
            logger.error("[AuditWorker] Audit check failed: %s", e)

        try:
            await asyncio.sleep(POLL_INTERVAL_SECONDS)
        except asyncio.CancelledError:
            break


async def _run_audit_checks():
    """Execute all audit checks in sequence."""
    from app.monitoring.audit_middleware import log_audit_event

    now_iso = datetime.now(timezone.utc).isoformat()

    # ── 1. Memory Soak Check ──────────────────────────────────────────
    current_memory = _get_memory_mb()
    memory_delta = current_memory - _baseline_memory_mb

    if memory_delta > MEMORY_GROWTH_THRESHOLD_MB:
        log_audit_event(
            endpoint="audit_worker",
            agent_name="audit_worker",
            status="warning",
            detail=(
                f"Memory soak warning: {current_memory:.1f}MB "
                f"(+{memory_delta:.1f}MB from baseline {_baseline_memory_mb:.1f}MB)"
            ),
            extra={"current_mb": current_memory, "baseline_mb": _baseline_memory_mb},
        )
    else:
        logger.debug(
            "[AuditWorker] Memory OK: %.1fMB (delta: +%.1fMB)",
            current_memory, memory_delta,
        )

    # ── 2. Connection Pool Leak Check ─────────────────────────────────
    current_connections = _get_pool_connection_count()
    conn_delta = current_connections - _baseline_connections

    if conn_delta > 10:  # More than 10 connections above baseline
        log_audit_event(
            endpoint="audit_worker",
            agent_name="audit_worker",
            status="warning",
            detail=(
                f"Connection leak warning: {current_connections} active "
                f"(+{conn_delta} from baseline {_baseline_connections})"
            ),
            extra={
                "current_connections": current_connections,
                "baseline_connections": _baseline_connections,
            },
        )
    else:
        logger.debug(
            "[AuditWorker] Connections OK: %d (delta: +%d)",
            current_connections, conn_delta,
        )

    # ── 3. Output Distribution Shift Check ────────────────────────────
    await _check_distribution_shift()

    # ── 4. Prompt Hash Drift Check ────────────────────────────────────
    _check_prompt_hash_drift()


async def _check_distribution_shift():
    """Analyze recent audit events for output distribution shifts.

    Looks at the agent_name distribution from the last 50 audit events
    and compares to the previous window. A large shift may indicate
    that certain agents are being called disproportionately or failing.
    """
    global _previous_distribution

    from app.monitoring.audit_middleware import get_audit_buffer

    events = get_audit_buffer(limit=50)
    if len(events) < 10:
        return  # Not enough data to analyze

    # Count by agent_name
    counter = Counter(e.get("agent_name", "unknown") for e in events)
    total = sum(counter.values())
    current_dist = {k: v / total for k, v in counter.items()}

    if _previous_distribution:
        # Compare distributions
        all_keys = set(current_dist.keys()) | set(_previous_distribution.keys())
        max_shift = 0.0
        shifted_agent = ""

        for key in all_keys:
            prev = _previous_distribution.get(key, 0.0)
            curr = current_dist.get(key, 0.0)
            shift = abs(curr - prev)
            if shift > max_shift:
                max_shift = shift
                shifted_agent = key

        if max_shift > DISTRIBUTION_SHIFT_THRESHOLD:
            from app.monitoring.audit_middleware import log_audit_event
            log_audit_event(
                endpoint="audit_worker",
                agent_name="audit_worker",
                status="warning",
                detail=(
                    f"Distribution shift: '{shifted_agent}' shifted by "
                    f"{max_shift:.0%} (threshold: {DISTRIBUTION_SHIFT_THRESHOLD:.0%})"
                ),
                extra={"current": current_dist, "previous": _previous_distribution},
            )

    _previous_distribution = current_dist


def _check_prompt_hash_drift():
    """Check if prompt hashes have changed between audit windows.

    Groups the last 50 events by agent_name and checks if the
    system_prompt_hash has changed for any agent.
    """
    from app.monitoring.audit_middleware import get_audit_buffer

    events = get_audit_buffer(limit=50)
    if len(events) < 5:
        return

    # Group hashes by agent
    agent_hashes: dict[str, set[str]] = {}
    for e in events:
        agent = e.get("agent_name", "")
        prompt_hash = e.get("system_prompt_hash", "")
        if agent and prompt_hash:
            agent_hashes.setdefault(agent, set()).add(prompt_hash)

    for agent, hashes in agent_hashes.items():
        if len(hashes) > 1:
            from app.monitoring.audit_middleware import log_audit_event
            log_audit_event(
                endpoint="audit_worker",
                agent_name="audit_worker",
                status="info",
                detail=(
                    f"Prompt drift detected for '{agent}': "
                    f"{len(hashes)} distinct prompt hashes in last 50 events"
                ),
                extra={"agent": agent, "hash_count": len(hashes)},
            )


def _get_memory_mb() -> float:
    """Get current process memory usage in MB."""
    try:
        import psutil
        process = psutil.Process(os.getpid())
        return process.memory_info().rss / (1024 * 1024)
    except ImportError:
        # psutil not available — fall back to /proc on Linux
        try:
            with open(f"/proc/{os.getpid()}/status", "r") as f:
                for line in f:
                    if line.startswith("VmRSS:"):
                        return int(line.split()[1]) / 1024  # KB -> MB
        except Exception:
            pass
    except Exception:
        pass
    return 0.0


def _get_pool_connection_count() -> int:
    """Get the current number of active connections in the DB pool."""
    try:
        from app.db.connection import _pool
        if _pool is not None:
            stats = _pool.get_stats()
            # psycopg_pool stats: pool_size, pool_available, requests_waiting, etc.
            return stats.get("pool_size", 0) - stats.get("pool_available", 0)
    except Exception:
        pass
    return 0


def get_worker_status() -> dict:
    """Return the current status of the audit worker."""
    return {
        "running": _running,
        "baseline_memory_mb": round(_baseline_memory_mb, 1),
        "current_memory_mb": round(_get_memory_mb(), 1),
        "baseline_connections": _baseline_connections,
        "current_connections": _get_pool_connection_count(),
        "poll_interval_seconds": POLL_INTERVAL_SECONDS,
    }
