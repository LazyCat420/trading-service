"""
Memory Repository — Canonical memories, episodic observations, and consolidation reports.

Pure MongoDB implementation for canonical_memories, episodic_observations,
consolidation_reports, and memory_usage_logs collections.
"""

import json
import logging
from typing import Any, Dict, List
from datetime import datetime, timezone

from app.db import mongo_store

logger = logging.getLogger(__name__)


def get_unpromoted_observations(ticker: str) -> List[Dict[str, Any]]:
    """Fetch unpromoted episodic observations for a ticker from MongoDB."""
    return mongo_store.find_docs(
        'episodic_observations',
        {'ticker': ticker, 'promoted_to_memory': False},
        sort=[('created_at', 1)]
    )


def get_active_canonical_memories(ticker: str) -> List[Dict[str, Any]]:
    """Fetch active canonical memories for a ticker from MongoDB."""
    results = mongo_store.find_docs(
        'canonical_memories',
        {'ticker': ticker, 'status': {'$ne': 'deprecated'}}
    )
    for row in results:
        if row.get("tags") and isinstance(row["tags"], str):
            try:
                row["tags"] = json.loads(row["tags"])
            except json.JSONDecodeError:
                row["tags"] = []
    return results


def upsert_canonical_memories(memories: List[Dict[str, Any]]) -> None:
    """Upsert canonical memories into MongoDB and generate vector embeddings."""
    if not memories:
        return
    now_str = datetime.now(timezone.utc).isoformat()

    for m in memories:
        tags_str = json.dumps(m.get("tags", [])) if not isinstance(m.get("tags"), str) else m.get("tags")
        mongo_store.update_docs(
            'canonical_memories',
            {'id': m["id"]},
            {
                '$set': {
                    'type': m.get("type"),
                    'ticker': m.get("ticker"),
                    'sector': m.get("sector"),
                    'summary': m.get("summary"),
                    'tags': tags_str,
                    'confidence_score': m.get("confidence_score"),
                    'evidence_count': m.get("evidence_count"),
                    'status': m.get("status", "active"),
                    'last_used_at': m.get("last_used_at"),
                    'last_validated_at': m.get("last_validated_at"),
                    'updated_at': now_str,
                },
                '$setOnInsert': {'created_at': m.get("created_at", now_str)}
            },
            upsert=True
        )

        try:
            from app.services.embedding_service import embedder
            from app.db.vector_store import vector_store
            emb = embedder.embed_text(m["summary"])
            vector_store.store_embedding(
                source_table="canonical_memories",
                source_id=m["id"],
                ticker=m.get("ticker"),
                content_preview=m["summary"],
                embedding=emb
            )
        except Exception as e:
            logger.error(f"Failed to embed canonical memory {m['id']}: {e}")

    logger.info(f"Upserted {len(memories)} canonical memories.")


def deprecate_canonical_memories(memory_ids: List[str]) -> None:
    """Mark canonical memories as deprecated in MongoDB."""
    if not memory_ids:
        return
    now_str = datetime.now(timezone.utc).isoformat()
    for mid in memory_ids:
        mongo_store.update_docs(
            'canonical_memories',
            {'id': mid},
            {'$set': {'status': 'deprecated', 'updated_at': now_str}}
        )
    logger.info(f"Deprecated {len(memory_ids)} canonical memories.")


def mark_observations_promoted(observation_ids: List[str]) -> None:
    """Mark episodic observations as promoted in MongoDB."""
    if not observation_ids:
        return
    for oid in observation_ids:
        mongo_store.update_docs(
            'episodic_observations',
            {'id': oid},
            {'$set': {'promoted_to_memory': True}}
        )
    logger.info(f"Marked {len(observation_ids)} observations as promoted.")


def log_consolidation_run(record: Dict[str, Any]) -> None:
    """Log a consolidation run into MongoDB."""
    mongo_store.insert_docs('consolidation_reports', [{
        'id': record.get("id"),
        'run_at': record.get("run_at", datetime.now(timezone.utc).isoformat()),
        'ticker': record.get("ticker"),
        'observations_consumed': record.get("observations_consumed", 0),
        'memories_created': record.get("memories_created", 0),
        'memories_deprecated': record.get("memories_deprecated", 0),
    }])


def update_memory_validation_stats(
    memory_id: str, new_confidence: float, new_evidence_count: int, new_status: str
) -> None:
    """Specific targeted update for memory validation pipeline."""
    now_str = datetime.now(timezone.utc).isoformat()
    mongo_store.update_docs(
        'canonical_memories',
        {'id': memory_id},
        {'$set': {
            'confidence_score': new_confidence,
            'evidence_count': new_evidence_count,
            'status': new_status,
            'last_validated_at': now_str,
            'updated_at': now_str,
        }}
    )


def _ensure_schema() -> None:
    """Compat no-op. Schema creation was a Postgres concept; Mongo collections
    are created on first write and indexes live in app/db/mongo.py.

    Kept because app/services/memory/repository.py imports it function-locally
    and scripts/init_test_db.py references it by name. Its silent absence
    (deleted in b6b29d3, 2026-08-18) made every MemoryRetriever.retrieve raise
    ImportError, which orchestrator.py swallowed — no memory reached any agent
    prompt for 13 days. Guarded by tests/unit/test_memory_injection_seam.py.
    """
    return None
