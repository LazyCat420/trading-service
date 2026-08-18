import uuid
import logging
from datetime import datetime, timezone
from app.db import mongo_query, mongo_store

logger = logging.getLogger(__name__)


class SemanticMemoryStore:
    """
    Stores durable, ticker-specific facts that don't change cycle to cycle.
    Used to inject specific thresholds, rules, or historical facts into Working Memory.
    """

    def write_semantic(
        self,
        ticker: str,
        mem_type: str,
        content: str,
        confidence: float = 0.5,
        source_agent: str = "manual",
    ) -> str:
        """Store a new piece of semantic memory."""
        mem_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc).isoformat()

        mongo_store.insert_docs('semantic_memory', [{'id': mem_id, 'ticker': ticker, 'type': mem_type, 'content': content, 'confidence': confidence, 'source_agent': source_agent, 'created_at': now, 'last_accessed_at': now, 'access_count': 0}])

        logger.info(
            f"[SEMANTIC] Wrote '{mem_type}' for {ticker}: {content[:50]}..."
        )
        return mem_id

    def retrieve(self, ticker: str, limit: int = 6) -> list[dict]:
        """Query by ticker, ranked by confidence and last accessed."""
        now = datetime.now(timezone.utc).isoformat()

        # Pull ticker-specific AND global rules
        rows = mongo_query.find_rows('semantic_memory', {'$or': [{'ticker': ticker}, {'ticker': 'global'}]}, ['id', 'ticker', 'type', 'content', 'confidence'], sort=[('confidence', -1), ('last_accessed_at', -1)], limit=limit)

        results = []
        if rows:
            # Update access tracking to show frequency/recency of use
            ids = [r[0] for r in rows]
            mongo_store.update_docs(
                'semantic_memory', {'id': {'$in': ids}},
                {'$inc': {'access_count': 1}, '$set': {'last_accessed_at': now}})

            for r in rows:
                results.append(
                    {
                        "id": r[0],
                        "ticker": r[1],
                        "type": r[2],
                        "content": r[3],
                        "confidence": r[4],
                    }
                )

        return results

    def remove(self, mem_id: str) -> bool:
        """Delete an outdated semantic memory."""
        return mongo_store.delete_docs('semantic_memory', {'id': mem_id}) > 0


# Singleton instance
semantic_memory_store = SemanticMemoryStore()
