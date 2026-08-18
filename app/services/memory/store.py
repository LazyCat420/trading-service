"""
Memory Store (DAL)
Data Access Layer handling the central brain memory storage.

DB TABLE OWNERSHIP:
- episodic_observations
- canonical_memories
"""

import json
import uuid
import logging
from datetime import datetime, timezone
from app.services.memory.repository import MemoryRepository
from app.db import mongo_query, mongo_store

logger = logging.getLogger(__name__)


class MemoryStore:
    def add_episodic_observation(self, observation: dict) -> str:
        """
        Inserts a new raw episodic observation candidate.
        Expects:
          cycle_id, ticker(opt), sector(opt), source_type, observation_text, rationale_excerpt(opt),
          confidence_at_creation(opt), outcome_label(opt), outcome_score(opt)
        """
        obs_id = observation.get("id") or str(uuid.uuid4())

        # Use existing timestamp or generate now
        created_at = observation.get("created_at")
        if not created_at:
            created_at = datetime.now(timezone.utc).isoformat()

        # Dedup guard: a retried/re-entered cycle must not double-write the
        # same observation (there is no unique constraint on the table).
        # On a dup, REFRESH the row — a re-run can legitimately land on a
        # different decision, and the latest one is the truth for this cycle.
        cycle_id = observation.get("cycle_id")
        ticker = observation.get("ticker")
        source_type = observation.get("source_type")
        if cycle_id and ticker and source_type:
            dup = mongo_query.find_row('episodic_observations', {'cycle_id': cycle_id, 'ticker': ticker, 'source_type': source_type}, ['id'])
            if dup:
                mongo_store.update_docs('episodic_observations', {'id': dup[0]}, {'$set': {'observation_text': observation["observation_text"], 'confidence_at_creation': observation.get("confidence_at_creation"), 'outcome_label': observation.get("outcome_label"), 'outcome_score': observation.get("outcome_score")}})
                logger.info(
                    "[MemoryStore] Duplicate observation refreshed (%s/%s/%s)",
                    cycle_id, ticker, source_type,
                )
                return dup[0]

        mongo_store.insert_docs('episodic_observations', [{'id': obs_id, 'created_at': created_at, 'cycle_id': observation["cycle_id"], 'ticker': observation.get("ticker"), 'sector': observation.get("sector"), 'source_type': observation["source_type"], 'observation_text': observation["observation_text"], 'rationale_excerpt': observation.get("rationale_excerpt"), 'confidence_at_creation': observation.get("confidence_at_creation"), 'outcome_label': observation.get("outcome_label"), 'outcome_score': observation.get("outcome_score"), 'promoted_to_memory': observation.get("promoted_to_memory", False)}])
        return obs_id

    def get_unpromoted_observations(self, limit: int = 100) -> list[dict]:
        """
        Retrieves recent candidate observations that haven't yet been promoted to canonical memory.
        """
        cols = ['id', 'created_at', 'cycle_id', 'ticker', 'sector', 'source_type',
                'observation_text', 'rationale_excerpt', 'confidence_at_creation',
                'outcome_label', 'outcome_score', 'promoted_to_memory']
        rows = mongo_query.find_rows('episodic_observations', {'promoted_to_memory': False}, cols, sort=[('created_at', 1)], limit=limit)
        return [dict(zip(cols, row)) for row in rows]

    def mark_observation_promoted(self, obs_id: str):
        """Marks observation as having triggered or supplemented a canonical memory."""
        mongo_store.update_docs('episodic_observations', {'id': obs_id}, {'$set': {'promoted_to_memory': True}})

    def delete_promoted_observations_older_than(self, days: int) -> int:
        """Retention: drop observations already distilled into canonical
        memories. Unpromoted rows are the consolidator's pending inbox and
        are never deleted here. Returns rows deleted."""
        from datetime import timedelta
        cutoff = datetime.now(timezone.utc) - timedelta(days=int(days))
        # created_at is stored as an ISO-8601 string by add_episodic_observation,
        # and ISO-8601 UTC strings compare lexicographically in timestamp order.
        return mongo_store.delete_docs(
            'episodic_observations',
            {'promoted_to_memory': True, 'created_at': {'$lt': cutoff.isoformat()}},
        )

    def add_canonical_memory(self, memory: dict) -> str:
        """
        Inserts a new canonical memory.
        Expects:
          type, ticker(opt), sector(opt), summary, tags, confidence_score, evidence_count(opt), status(opt)
        """
        mem_id = memory.get("id") or str(uuid.uuid4())

        tags = memory.get("tags") or []
        tags_json = json.dumps(tags)

        now = datetime.now(timezone.utc).isoformat()

        mongo_store.insert_docs('canonical_memories', [{'id': mem_id, 'type': memory["type"], 'ticker': memory.get("ticker"), 'sector': memory.get("sector"), 'summary': memory["summary"], 'tags': tags_json, 'confidence_score': memory["confidence_score"], 'evidence_count': memory.get("evidence_count", 1), 'status': memory.get("status", "tentative"), 'last_used_at': memory.get("last_used_at"), 'last_validated_at': memory.get("last_validated_at"), 'created_at': memory.get("created_at", now), 'updated_at': memory.get("updated_at", now)}])
        
        try:
            from app.services.embedding_service import embedder
            from app.db.vector_store import vector_store
            emb = embedder.embed_text(memory["summary"])
            vector_store.store_embedding(
                source_table="canonical_memories",
                source_id=mem_id,
                ticker=memory.get("ticker"),
                content_preview=memory["summary"],
                embedding=emb
            )
        except Exception as e:
            logger.error(f"Failed to embed and store canonical memory {mem_id}: {e}")

        return mem_id

    def get_memories_by_ticker(
        self, ticker: str, active_only: bool = True
    ) -> list[dict]:
        """Fetches memory rules pertinent to a specific ticker."""
        return MemoryRepository.get_memories_by_ticker(ticker, active_only)

    def update_memory_status(
        self,
        mem_id: str,
        new_confidence: float,
        new_status: str,
        validated_at: str = None,
    ):
        """
        Update the confidence decay and status flags for a canonical memory.
        """
        if not validated_at:
            validated_at = datetime.now(timezone.utc).isoformat()

        now = datetime.now(timezone.utc).isoformat()
        mongo_store.update_docs('canonical_memories', {'id': mem_id}, {'$set': {'confidence_score': new_confidence, 'status': new_status, 'last_validated_at': validated_at, 'updated_at': now}})

    def record_memory_usage(self, mem_id: str):
        """
        Touch the 'last_used_at' column when extracted for RAG context.
        """
        now = datetime.now(timezone.utc).isoformat()
        mongo_store.update_docs('canonical_memories', {'id': mem_id}, {'$set': {'last_used_at': now}})
