"""
Checkpoint Manager — Cycle state persistence for crash recovery.

Pure MongoDB implementation for cycle_checkpoints collection.
"""

import logging
from datetime import datetime, timezone
from typing import Any

from app.db import mongo_store

logger = logging.getLogger(__name__)


class CheckpointManager:
    """Manages cycle checkpoint persistence in MongoDB.

    Checkpoints are upserted so that re-running the same step overwrites
    the previous checkpoint.
    """

    def save(
        self,
        cycle_id: str,
        step_name: str,
        ticker: str = "",
        state: dict | None = None,
    ) -> bool:
        """Save a checkpoint after a step completes successfully."""
        try:
            now = datetime.now(timezone.utc)
            mongo_store.update_docs(
                'cycle_checkpoints',
                {'cycle_id': cycle_id, 'step_name': step_name, 'ticker': ticker or ""},
                {'$set': {
                    'state_blob': state or {},
                    'completed_at': now,
                }},
                upsert=True
            )
            logger.debug(
                "[CHECKPOINT] Saved: %s/%s/%s",
                cycle_id[:12],
                step_name,
                ticker or "*",
            )
            return True
        except Exception as e:
            logger.warning("[CHECKPOINT] Save failed for %s/%s: %s", cycle_id, step_name, e)
            return False

    def has_completed(
        self,
        cycle_id: str,
        step_name: str,
        ticker: str = "",
    ) -> bool:
        """Check if a step was already completed in a previous run."""
        try:
            docs = mongo_store.find_docs(
                'cycle_checkpoints',
                {'cycle_id': cycle_id, 'step_name': step_name, 'ticker': ticker or ""},
                limit=1
            )
            return len(docs) > 0
        except Exception as e:
            logger.warning("[CHECKPOINT] Check failed: %s", e)
            return False

    def load_state(
        self,
        cycle_id: str,
        step_name: str,
        ticker: str = "",
    ) -> dict | None:
        """Load the state blob for a specific checkpoint."""
        try:
            docs = mongo_store.find_docs(
                'cycle_checkpoints',
                {'cycle_id': cycle_id, 'step_name': step_name, 'ticker': ticker or ""},
                limit=1
            )
            if docs:
                return docs[0].get('state_blob')
            return None
        except Exception as e:
            logger.warning("[CHECKPOINT] Load failed: %s", e)
            return None

    def load_latest(self, cycle_id: str) -> dict | None:
        """Load the most recent checkpoint for a cycle."""
        try:
            docs = mongo_store.find_docs(
                'cycle_checkpoints',
                {'cycle_id': cycle_id},
                sort=[('completed_at', -1)],
                limit=1
            )
            if docs:
                d = docs[0]
                return {
                    "step_name": d.get("step_name"),
                    "ticker": d.get("ticker"),
                    "state": d.get("state_blob") or {},
                    "completed_at": str(d.get("completed_at")),
                }
            return None
        except Exception as e:
            logger.warning("[CHECKPOINT] Load latest failed: %s", e)
            return None

    def get_completed_steps(self, cycle_id: str) -> list[dict]:
        """Get all completed steps for a cycle (for resume logic)."""
        try:
            docs = mongo_store.find_docs(
                'cycle_checkpoints',
                {'cycle_id': cycle_id},
                sort=[('completed_at', 1)]
            )
            return [
                {
                    "step_name": d.get("step_name"),
                    "ticker": d.get("ticker"),
                    "completed_at": str(d.get("completed_at")),
                }
                for d in docs
            ]
        except Exception as e:
            logger.warning("[CHECKPOINT] Get steps failed: %s", e)
            return []

    def clear_cycle(self, cycle_id: str) -> int:
        """Delete all checkpoints for a completed cycle."""
        try:
            count = mongo_store.delete_docs('cycle_checkpoints', {'cycle_id': cycle_id})
            logger.info(
                "[CHECKPOINT] Cleared %d checkpoints for cycle %s",
                count,
                cycle_id[:12],
            )
            return count
        except Exception as e:
            logger.warning("[CHECKPOINT] Clear failed for %s: %s", cycle_id, e)
            return 0

    def get_stats(self) -> dict:
        """Get checkpoint statistics for the monitoring dashboard."""
        try:
            docs = mongo_store.find_docs('cycle_checkpoints', {})
            total = len(docs)
            active_cycles = len(set(d.get("cycle_id") for d in docs if d.get("cycle_id")))
            last_cp = max((d.get("completed_at") for d in docs if d.get("completed_at")), default=None)
            return {
                "total_checkpoints": total,
                "active_cycles": active_cycles,
                "last_checkpoint": str(last_cp) if last_cp else None,
            }
        except Exception as e:
            logger.warning("[CHECKPOINT] Stats failed: %s", e)
            return {"error": str(e)}


def ensure_checkpoints_table() -> None:
    """No-op kept for backwards compatibility."""
    pass


# Singleton
checkpoint_manager = CheckpointManager()
