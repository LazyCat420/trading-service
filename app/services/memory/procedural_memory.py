import uuid
import logging
from app.db import mongo_query, mongo_store

logger = logging.getLogger(__name__)


class ProceduralMemoryStore:
    """
    Stores learned step-by-step patterns and strategies.
    Tracks quantitative success/failure rates when strategies are triggered.
    """

    def write_procedure(
        self,
        ticker: str,
        trigger_pattern: str,
        procedure: str,  # JSON string representation of steps
        created_by_agent: str = "manual",
    ) -> str:
        """Store a new procedural pattern."""
        from app.db.connection import get_db

        with get_db() as db:
            mem_id = str(uuid.uuid4())

            mongo_store.insert_docs('procedural_memory', [{'id': mem_id, 'ticker': ticker, 'trigger_pattern': trigger_pattern, 'procedure': procedure, 'success_count': 0, 'failure_count': 0, 'success_rate': 0.0, 'created_by_agent': created_by_agent}])

            logger.info(
                f"[PROCEDURAL] Wrote new pattern for {ticker}: {trigger_pattern[:50]}..."
            )
            return mem_id

    def write_procedure_if_new(
        self,
        ticker: str,
        trigger_pattern: str,
        procedure: str,
        created_by_agent: str = "consolidator",
    ) -> str:
        """Insert a procedure only if (ticker, trigger_pattern) doesn't already
        exist. Prevents the consolidator from re-inserting the same pattern on
        every run (there is no unique constraint on the table)."""
        from app.db.connection import get_db

        with get_db() as db:
            existing = mongo_query.find_row('procedural_memory', {'ticker': ticker, 'trigger_pattern': trigger_pattern}, ['id'])
            if existing:
                return existing[0]

        return self.write_procedure(ticker, trigger_pattern, procedure, created_by_agent)

    def record_outcome(self, mem_id: str, success: bool):
        """Update success/failure counts after a pattern was followed and outcome resolved."""
        from app.db.connection import get_db

        with get_db() as db:
            if success:
                db.execute(
                    "UPDATE procedural_memory SET success_count = success_count + 1 WHERE id = %s",
                    [mem_id],
                )
            else:
                db.execute(
                    "UPDATE procedural_memory SET failure_count = failure_count + 1 WHERE id = %s",
                    [mem_id],
                )

            # Recompute success_rate
            db.execute(
                """
                UPDATE procedural_memory
                SET success_rate = CAST(success_count AS DOUBLE PRECISION) / (success_count + failure_count)
                WHERE id = %s AND (success_count + failure_count) > 0
            """,
                [mem_id],
            )

    def retrieve(self, ticker: str, limit: int = 3) -> list[dict]:
        """Query top proven patterns for a ticker, ordered by success rate."""
        from app.db.connection import get_db

        with get_db() as db:
            # We need a small minimum sample size, say 3, if not we sort by success_rate
            rows = db.execute(
                """
                SELECT id, ticker, trigger_pattern, procedure, success_rate, (success_count + failure_count) as total_uses
                FROM procedural_memory
                WHERE ticker = %s OR ticker = 'global'
                ORDER BY success_rate DESC
                LIMIT %s
            """,
                [ticker, limit],
            ).fetchall()

            results = []
            for r in rows:
                results.append(
                    {
                        "id": r[0],
                        "ticker": r[1],
                        "trigger_pattern": r[2],
                        "procedure": r[3],
                        "success_rate": r[4],
                        "total_uses": r[5],
                    }
                )
            return results


# Singleton instance
procedural_memory_store = ProceduralMemoryStore()
