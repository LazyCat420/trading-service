"""Attempts and the repair queue — CORAL's shared state, as two tables.

CORAL keeps ``.coral/public/attempts/<commit>.json`` and a leaderboard, and the
heartbeat prompts tell agents to read it (``coral log -n 10``) before deciding
what to try next. That record is what makes a plateau visible and stops the team
re-running an idea that already scored 0.

The old loop had ``pending_evolution_fixes``, which stored proposals with no
measured score at all — 96 rows, none of them comparable to any other. These
tables store the ScoreBundle, so "did this target get better" is a query rather
than a reading exercise.
"""
from __future__ import annotations

import json
import logging
import uuid
from typing import Iterable

from app.db.connection import get_db, safe_jsonb
from app.cognition.evolution.coral.types import Attempt, RepairJob

logger = logging.getLogger(__name__)

# CORAL's pivot heartbeat fires on a plateau. Ours is blunter because each
# attempt costs a full suite run: after this many graded attempts on one target
# with nothing green, the target is parked for a human instead of ground on.
PLATEAU_ATTEMPTS = 6


# ── Queue (written by the container watchdog, drained by the host runner) ────


def enqueue_job(
    *,
    cycle_id: str,
    error_message: str,
    traceback_text: str,
    target_path: str | None = None,
    target_symbol: str | None = None,
    repro_test: str | None = None,
) -> str | None:
    """Queue a failure for repair. Returns the job id, or None if already open.

    The unique partial index on (target_path, target_symbol) WHERE status is
    open does the dedup: the watchdog runs hourly and would otherwise queue the
    same traceback until the queue was one bug repeated.
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
            "[CORAL-QUEUE] %s::%s already has an open job — not re-queued",
            target_path, target_symbol,
        )
        return None
    logger.info("[CORAL-QUEUE] queued %s for %s::%s", job_id[:8], target_path, target_symbol)
    return job_id


# A claim older than this with no `finished_at` is assumed dead. Sized against
# the work: a repair proposes on two boxes and runs pytest, which is minutes,
# not hours — but generously, because reclaiming a job that is merely slow would
# run two graders against one target.
_STALE_CLAIM_HOURS = 3


def claim_next_job() -> RepairJob | None:
    """Atomically take the oldest queued job. Returns None when the queue is empty.

    Also reclaims stale `running` rows. Measured 2026-07-28: a runner killed
    mid-job left `status='running', attempts=1, finished_at=NULL` and the next
    invocation reported "queue is empty" — the job was invisible forever
    because the claim only looked at `status='queued'`. The host runner is a
    manually-launched process, so being killed is the NORMAL case, not an edge
    one, and a queue that silently drops work on it is worse than no queue.

    `attempts` still increments across reclaims, so a job that repeatedly kills
    its runner hits PLATEAU_ATTEMPTS and gets parked for a human rather than
    looping forever.
    """
    with get_db() as db:
        row = db.execute(
            """
            UPDATE evolution_repair_queue SET status = 'running',
                   claimed_at = CURRENT_TIMESTAMP, attempts = attempts + 1
            WHERE id = (
                SELECT id FROM evolution_repair_queue
                WHERE status = 'queued'
                   OR (status = 'running'
                       AND finished_at IS NULL
                       AND claimed_at < now() - (%s || ' hours')::interval)
                ORDER BY created_at
                FOR UPDATE SKIP LOCKED
                LIMIT 1
            )
            RETURNING id, cycle_id, error_message, traceback_text,
                      target_path, target_symbol, status, attempts, repro_test
            """,
            [str(_STALE_CLAIM_HOURS)],
        ).fetchone()
    if not row:
        return None
    return RepairJob(
        id=row[0], cycle_id=row[1] or "", error_message=row[2] or "",
        traceback_text=row[3] or "", target_path=row[4], target_symbol=row[5],
        status=row[6] or "running", attempts=row[7] or 0, repro_test=row[8],
    )


def finish_job(job_id: str, status: str, note: str = "") -> None:
    with get_db() as db:
        db.execute(
            "UPDATE evolution_repair_queue SET status = %s, last_error = %s, "
            "finished_at = CURRENT_TIMESTAMP WHERE id = %s",
            [status, note[:2000], job_id],
        )


def open_jobs() -> list[RepairJob]:
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


# ── Attempts ────────────────────────────────────────────────────────────────


def record_attempt(attempt: Attempt) -> None:
    with get_db() as db:
        db.execute(
            """
            INSERT INTO evolution_attempts
                (id, job_id, target_path, target_symbol, island, model,
                 diff, rationale, score, bundle, commit_hash, branch)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            [attempt.id, attempt.job_id, attempt.target_path, attempt.target_symbol,
             attempt.island, attempt.model, attempt.diff, attempt.rationale[:4000],
             attempt.score, json.dumps(attempt.bundle.to_dict()),
             attempt.commit_hash, attempt.branch],
        )
    logger.info(
        "[CORAL-ATTEMPT] %s %s scored %.2f — %s",
        attempt.id[:8], attempt.island, attempt.score, attempt.bundle.summary(),
    )


def leaderboard(target_path: str, limit: int = 10) -> list[dict]:
    """Best-scoring attempts for a target, newest first within a score."""
    with get_db() as db:
        rows = db.execute(
            "SELECT id, island, model, score, bundle, created_at, commit_hash "
            "FROM evolution_attempts WHERE target_path = %s "
            "ORDER BY score DESC, created_at DESC LIMIT %s",
            [target_path, limit],
        ).fetchall()
    return [
        {"id": r[0], "island": r[1], "model": r[2], "score": r[3],
         "bundle": safe_jsonb(r[4]) or {}, "created_at": str(r[5]),
         "commit_hash": r[6]}
        for r in rows
    ]


def prior_failures(target_path: str, limit: int = 6) -> list[dict]:
    """Non-green attempts, for the "what has already been tried" prompt block."""
    with get_db() as db:
        rows = db.execute(
            "SELECT diff, score, bundle FROM evolution_attempts "
            "WHERE target_path = %s AND score < 1.0 "
            "ORDER BY created_at DESC LIMIT %s",
            [target_path, limit],
        ).fetchall()
    out = []
    for diff, score, bundle in rows:
        b = safe_jsonb(bundle) or {}
        out.append({
            "diff": diff or "",
            "score": score,
            "why": b.get("detail") or "no detail recorded",
        })
    return out


def render_prior_failures(failures: Iterable[dict], *, max_diff_lines: int = 12) -> str:
    """Prompt block: what was tried on this target and exactly why it scored badly.

    CORAL's pivot prompt makes the same distinction this block is designed to
    preserve — an approach ruled out by *evidence* is different from one ruled
    out by reluctance. Every line here is a measured result.
    """
    items = list(failures)
    if not items:
        return ""
    lines = ["── ALREADY TRIED ON THIS TARGET (do not repeat) ──"]
    for i, f in enumerate(items, 1):
        head = "\n".join((f["diff"] or "").splitlines()[:max_diff_lines])
        lines.append(f"\nAttempt {i} scored {f['score']:.2f} — {f['why']}")
        if head:
            lines.append(f"```diff\n{head}\n```")
    return "\n".join(lines)


def is_plateaued(target_path: str) -> tuple[bool, str]:
    """True when this target has burned its attempt budget without a green run."""
    with get_db() as db:
        row = db.execute(
            "SELECT COUNT(*), COALESCE(MAX(score), 0) FROM evolution_attempts "
            "WHERE target_path = %s",
            [target_path],
        ).fetchone()
    count, best = (row[0] or 0), (row[1] or 0.0)
    if best >= 1.0:
        return False, f"already has a green attempt (best {best:.2f})"
    if count >= PLATEAU_ATTEMPTS:
        return True, (
            f"{count} graded attempts, best score {best:.2f} — parked for a human "
            f"rather than ground on"
        )
    return False, f"{count}/{PLATEAU_ATTEMPTS} attempts used, best {best:.2f}"
