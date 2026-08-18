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
        existing = mongo_query.find_row('procedural_memory', {'ticker': ticker, 'trigger_pattern': trigger_pattern}, ['id'])
        if existing:
            return existing[0]

        return self.write_procedure(ticker, trigger_pattern, procedure, created_by_agent)

    def record_outcome(self, mem_id: str, success: bool):
        """Update success/failure counts after a pattern was followed and outcome resolved."""
        field = "success_count" if success else "failure_count"
        mongo_store.update_docs('procedural_memory', {'id': mem_id}, {'$inc': {field: 1}})

        # Recompute success_rate (SQL did this in a second UPDATE guarded on
        # total > 0; the same guard here, computed from the row just bumped).
        row = mongo_query.find_row('procedural_memory', {'id': mem_id},
                                   ['success_count', 'failure_count'])
        if row:
            sc, fc = row[0] or 0, row[1] or 0
            if sc + fc > 0:
                mongo_store.update_docs('procedural_memory', {'id': mem_id},
                                        {'$set': {'success_rate': float(sc) / (sc + fc)}})

    def retrieve(self, ticker: str, limit: int = 3) -> list[dict]:
        """Query top proven patterns for a ticker, ordered by success rate."""
        rows = mongo_query.find_rows(
            'procedural_memory',
            {'$or': [{'ticker': ticker}, {'ticker': 'global'}]},
            ['id', 'ticker', 'trigger_pattern', 'procedure', 'success_rate',
             'success_count', 'failure_count'],
            sort=[('success_rate', -1)], limit=limit)

        results = []
        for r in rows:
            results.append(
                {
                    "id": r[0],
                    "ticker": r[1],
                    "trigger_pattern": r[2],
                    "procedure": r[3],
                    "success_rate": r[4],
                    "total_uses": (r[5] or 0) + (r[6] or 0),
                }
            )
        return results


# Singleton instance
procedural_memory_store = ProceduralMemoryStore()
