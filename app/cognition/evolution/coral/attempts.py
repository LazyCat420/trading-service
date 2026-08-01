"""The failure log: what broke in a cycle, and whether it was patchable.

This used to be the shared state of an autonomous repair loop — a queue the
host runner drained, plus a leaderboard and plateau detector so successive LLM
proposers could avoid re-trying an idea that had already scored 0.

The loop is gone (2026-07-31). It produced two top-scored patches in its life
and both were wrong the same way: they re-added a tool to the analyst
whitelists, directly above the comment explaining why a human had removed it,
because a stale test still demanded it. Ranking by "the suite goes green" cannot
ask whether the test is still right, and that question was load-bearing both
times. The grading survives as `scripts/grade_patch.py`, which a human points at
a patch they wrote.

So what is left here is a LOG, not a queue. The watchdog still records which
cycle failures landed in patchable scope, because that is genuinely useful
telemetry — it answers "what keeps breaking" — and it costs one INSERT. Nothing
drains it and nothing proposes a patch from it.
"""
from __future__ import annotations

import logging
import uuid

from app.db.connection import get_db
from app.cognition.evolution.coral.types import RepairJob

logger = logging.getLogger(__name__)


def enqueue_job(
    *,
    cycle_id: str,
    error_message: str,
    traceback_text: str,
    target_path: str | None = None,
    target_symbol: str | None = None,
    repro_test: str | None = None,
) -> str | None:
    """Record a failure. Returns the row id, or None if one is already open.

    The unique partial index on (target_path, target_symbol) WHERE status is
    open does the dedup: the watchdog runs hourly and would otherwise log the
    same traceback until the table was one bug repeated.
    """
    job_id = str(uuid.uuid4())
    with get_db() as db:
        row = db.execute(
            """
            INSERT INTO evolution_repair_queue
                (id, cycle_id, error_message, traceback_text,
                 target_path, target_symbol, repro_test, status)
            VALUES (%s, %s, %s, %s, %s, %s, %s, 'queued')
            ON CONFLICT DO NOTHING
            RETURNING id
            """,
            [job_id, cycle_id, error_message, traceback_text,
             target_path, target_symbol, repro_test],
        ).fetchone()
    if not row:
        logger.info(
            "[FAILURE-LOG] %s::%s already has an open entry — not re-logged",
            target_path, target_symbol,
        )
        return None
    logger.info(
        "[FAILURE-LOG] logged %s for %s::%s", job_id[:8], target_path, target_symbol
    )
    return job_id


def finish_job(job_id: str, status: str, note: str = "") -> None:
    """Close an entry. Anything outside ('queued','running') drops it out of
    ``open_jobs``; the runner's old vocabulary was done/failed/skipped."""
    with get_db() as db:
        db.execute(
            "UPDATE evolution_repair_queue SET status = %s, last_error = %s, "
            "finished_at = CURRENT_TIMESTAMP WHERE id = %s",
            [status, note[:2000], job_id],
        )


def open_jobs() -> list[RepairJob]:
    """Failures logged and not yet closed."""
    with get_db() as db:
        rows = db.execute(
            "SELECT id, cycle_id, error_message, traceback_text, target_path, "
            "target_symbol, status, attempts FROM evolution_repair_queue "
            "WHERE status IN ('queued','running') ORDER BY created_at"
        ).fetchall()
    return [
        RepairJob(id=r[0], cycle_id=r[1] or "", error_message=r[2] or "",
                  traceback_text=r[3] or "", target_path=r[4], target_symbol=r[5],
                  status=r[6] or "", attempts=r[7] or 0)
        for r in rows
    ]
