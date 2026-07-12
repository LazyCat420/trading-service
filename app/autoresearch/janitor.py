"""
Data Janitor — Lifecycle management for AutoResearch data.

Prevents unbounded data growth by:
1. Pruning old report JSON blobs (keep scores, drop large payloads)
2. Hard-deleting expired directives older than 7 days
3. Detecting degenerate scores (same value for 10+ cycles = system bug, not finding)
4. Cleaning stale 'running' reports from crashed cycles
"""

import logging
from datetime import datetime, timezone, timedelta

from app.db.connection import get_db

logger = logging.getLogger(__name__)

# Reports older than this get their JSON blobs stripped
REPORT_FULL_RETENTION_DAYS = 30
# Expired directives older than this get hard-deleted
DIRECTIVE_HARD_DELETE_DAYS = 7
# If a sub-score is 0 for this many consecutive reports, flag as system bug
DEGENERATE_THRESHOLD = 5


def run_janitor() -> dict:
    """Run all janitor tasks. Returns summary of actions taken."""
    results = {}
    results["reports_pruned"] = _prune_old_reports()
    results["directives_deleted"] = _delete_old_directives()
    results["stale_cleaned"] = _clean_stale_reports()
    results["degenerate_flags"] = _detect_degenerate_scores()

    total_actions = sum(v for v in results.values() if isinstance(v, int))
    if total_actions > 0:
        logger.info("[JANITOR] Cleanup complete: %s", results)
    else:
        logger.debug("[JANITOR] No cleanup needed this cycle.")

    return results


def _prune_old_reports() -> int:
    """Strip large JSON blobs from reports older than retention period."""
    pruned = 0
    try:
        cutoff = datetime.now(timezone.utc) - timedelta(days=REPORT_FULL_RETENTION_DAYS)
        with get_db() as db:
            # Only strip reports that still have JSON blobs
            result = db.execute(
                """UPDATE autoresearch_reports
                SET data_gaps = NULL,
                    decision_issues = NULL,
                    llm_issues = NULL,
                    performance_metrics = NULL,
                    reflection = NULL,
                    recovery_stats = NULL
                WHERE created_at < %s
                  AND status = 'done'
                  AND (data_gaps IS NOT NULL
                       OR decision_issues IS NOT NULL
                       OR reflection IS NOT NULL)""",
                [cutoff],
            )
            pruned = result.rowcount if hasattr(result, 'rowcount') else 0
    except Exception as e:
        logger.warning("[JANITOR] Report pruning failed: %s", e)
    return pruned


def _delete_old_directives() -> int:
    """Hard-delete expired directives older than retention period."""
    deleted = 0
    try:
        cutoff = datetime.now(timezone.utc) - timedelta(days=DIRECTIVE_HARD_DELETE_DAYS)
        with get_db() as db:
            result = db.execute(
                """DELETE FROM cycle_directives
                WHERE status IN ('expired', 'actioned')
                  AND resolved_at < %s""",
                [cutoff],
            )
            deleted = result.rowcount if hasattr(result, 'rowcount') else 0
    except Exception as e:
        logger.warning("[JANITOR] Directive cleanup failed: %s", e)
    return deleted


def _clean_stale_reports() -> int:
    """Mark reports stuck in 'running' as stale."""
    cleaned = 0
    try:
        cutoff = datetime.now(timezone.utc) - timedelta(minutes=30)
        with get_db() as db:
            result = db.execute(
                """UPDATE autoresearch_reports
                SET status = 'stale'
                WHERE status = 'running' AND created_at < %s""",
                [cutoff],
            )
            cleaned = result.rowcount if hasattr(result, 'rowcount') else 0
    except Exception as e:
        logger.warning("[JANITOR] Stale report cleanup failed: %s", e)
    return cleaned


def _detect_degenerate_scores() -> int:
    """
    Check if any sub-score has been 0 for DEGENERATE_THRESHOLD consecutive reports.
    If so, log a warning — this is a system bug, not a data finding.
    """
    flagged = 0
    try:
        with get_db() as db:
            rows = db.execute(
                """SELECT decision_quality_score, data_quality_score, llm_performance_score
                FROM autoresearch_reports
                WHERE status = 'done'
                ORDER BY created_at DESC
                LIMIT %s""",
                [DEGENERATE_THRESHOLD],
            ).fetchall()

            if len(rows) < DEGENERATE_THRESHOLD:
                return 0

            score_names = ["decision_quality", "data_quality", "llm_performance"]
            for col_idx, name in enumerate(score_names):
                scores = [r[col_idx] for r in rows]
                if all(s is not None and s == 0 for s in scores):
                    logger.warning(
                        "[JANITOR] DEGENERATE SCORE: %s has been 0 for %d consecutive reports — "
                        "this is likely a system bug, not a data issue.",
                        name, DEGENERATE_THRESHOLD,
                    )
                    flagged += 1
    except Exception as e:
        logger.warning("[JANITOR] Degenerate score detection failed: %s", e)
    return flagged
