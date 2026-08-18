import uuid
import logging
from datetime import datetime, timezone
from app.db import mongo_query, mongo_store

logger = logging.getLogger(__name__)


class EpisodicMemoryStore:
    """
    Stores compressed summaries of completed trading cycles.
    Each episode captures what the agent observed, decided, and the outcome.
    """

    def write_episode(
        self,
        cycle_id: str,
        ticker: str,
        summary: str,
        key_decisions: str = "[]",
        outcome: str = "neutral",
        outcome_score: float = 0.0,
        agents_involved: str = "[]",
    ) -> str:
        """Store a new episode summarize a completed cycle."""
        from app.db.connection import get_db

        mem_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc).isoformat()

        mongo_store.insert_docs('episodic_memory', [{'id': mem_id, 'cycle_id': cycle_id, 'ticker': ticker, 'timestamp': now, 'summary': summary, 'key_decisions': key_decisions, 'outcome': outcome, 'outcome_score': outcome_score, 'agents_involved': agents_involved}])

        logger.info(f"[EPISODIC] Wrote episode for {ticker} (Cycle {cycle_id})")
        return mem_id

    def record_outcome(
        self, cycle_id: str, ticker: str, outcome: str, outcome_score: float
    ) -> int:
        """Resolve the episode(s) this cycle wrote for this ticker.

        Every episode was written with ``outcome="pending"`` and
        ``outcome_score=0.0`` and nothing ever revised it — 572 pending rows
        (plus 3,080 at the ``neutral`` default) against 2,394 resolved
        ``decision_outcomes`` on 2026-08-12. So ``retrieve()``'s ranking read a
        column where every row tied at 0, and the "Relevant Past Cycles" block
        that every agent prompt carries showed a permanent ``Outcome Score:
        0.0``.

        Only rows still carrying an unresolved marker are updated. Both
        resolvers are re-entrant — a batch can revisit a cycle, and a position
        exit can land on a ticker whose 7-day claim already resolved — so an
        already-graded episode must not be re-graded by a later event.

        Returns rows updated.
        """
        from app.db.connection import get_db

        with get_db() as db:
            result = db.execute(
                """
                UPDATE episodic_memory
                SET outcome = %s, outcome_score = %s
                WHERE cycle_id = %s AND ticker = %s
                  AND (outcome IS NULL OR outcome IN ('pending', 'neutral'))
                """,
                [outcome, outcome_score, cycle_id, ticker],
            )
            # PooledCursor doesn't proxy rowcount — read the real cursor's
            # (same accessor as the retention delete in memory/store.py).
            rc = getattr(result, "rowcount", None)
            if rc is None:
                rc = getattr(getattr(result, "_cursor", None), "rowcount", 0)
            return rc if rc and rc > 0 else 0

    def retrieve(self, ticker: str, limit: int = 4) -> list[dict]:
        """Query past episodes by ticker, ranked by most successful outcomes."""
        from app.db.connection import get_db

        rows = mongo_query.find_rows('episodic_memory', {'ticker': ticker}, ['id', 'cycle_id', 'timestamp', 'summary', 'outcome_score', 'key_decisions', 'outcome'], sort=[('timestamp', -1)], limit=limit)

        results = []
        for r in rows:
            results.append(
                {
                    "id": r[0],
                    "cycle_id": r[1],
                    "timestamp": r[2],
                    "summary": r[3],
                    "outcome_score": r[4],
                    "key_decisions": r[5],
                    "outcome": r[6],
                }
            )
        return results


# Singleton instance
episodic_memory_store = EpisodicMemoryStore()
