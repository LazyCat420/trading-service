import json
import logging
from typing import Any, Dict, List
from datetime import datetime, timezone
from app.db.connection import get_db
from app.db import mongo_store

logger = logging.getLogger(__name__)


def _ensure_schema():
    """Ensure this repo's memory tables exist.

    episodic_observations is owned by app/db/migrations.py — do not
    duplicate its DDL here (the definitions drifted once already).
    """
    with get_db() as db:
        db.execute("""
        CREATE TABLE IF NOT EXISTS canonical_memories (
            id TEXT PRIMARY KEY,
            type TEXT,
            ticker TEXT,
            sector TEXT,
            summary TEXT,
            tags TEXT,
            confidence_score DOUBLE PRECISION,
            evidence_count INTEGER,
            status TEXT,
            last_used_at TIMESTAMPTZ,
            last_validated_at TIMESTAMPTZ,
            created_at TIMESTAMPTZ,
            updated_at TIMESTAMPTZ
        );
        """)

        db.execute("""
        CREATE TABLE IF NOT EXISTS consolidation_reports (
            id TEXT PRIMARY KEY,
            run_at TIMESTAMPTZ,
            ticker TEXT,
            observations_consumed INTEGER,
            memories_created INTEGER,
            memories_deprecated INTEGER
        );
        """)

        db.execute("""
        CREATE TABLE IF NOT EXISTS memory_usage_logs (
            id TEXT PRIMARY KEY,
            cycle_id TEXT,
            ticker TEXT,
            memory_id TEXT,
            budget_used_chars INTEGER,
            created_at TIMESTAMPTZ
        );
        """)


def _rows_to_dicts(cursor) -> List[Dict[str, Any]]:
    columns = [desc[0] for desc in cursor.description]
    return [dict(zip(columns, row)) for row in cursor.fetchall()]


def get_unpromoted_observations(ticker: str) -> List[Dict[str, Any]]:
    _ensure_schema()
    with get_db() as db:
        db.execute(
            """
            SELECT * FROM episodic_observations 
            WHERE ticker = %s AND promoted_to_memory = FALSE
            ORDER BY created_at ASC
        """,
            [ticker],
        )
        return _rows_to_dicts(db)


def get_active_canonical_memories(ticker: str) -> List[Dict[str, Any]]:
    _ensure_schema()
    with get_db() as db:
        db.execute(
            """
            SELECT * FROM canonical_memories 
            WHERE ticker = %s AND status != 'deprecated'
        """,
            [ticker],
        )

        results = _rows_to_dicts(db)
        # Deserialize tags
        for row in results:
            if row.get("tags") and isinstance(row["tags"], str):
                try:
                    row["tags"] = json.loads(row["tags"])
                except json.JSONDecodeError:
                    row["tags"] = []
        return results


def upsert_canonical_memories(memories: List[Dict[str, Any]]):
    if not memories:
        return
    _ensure_schema()
    with get_db() as db:
        now_str = datetime.now(timezone.utc).isoformat()

        for m in memories:
            tags_str = json.dumps(m.get("tags", []))
            # PostgreSQL ON CONFLICT upsert — clean single statement
            mongo_store.update_docs('canonical_memories', {'id': m["id"]}, {'$set': {'type': m.get("type"), 'ticker': m.get("ticker"), 'sector': m.get("sector"), 'summary': m.get("summary"), 'tags': tags_str, 'confidence_score': m.get("confidence_score"), 'evidence_count': m.get("evidence_count"), 'status': m.get("status", "active"), 'last_used_at': m.get("last_used_at"), 'last_validated_at': m.get("last_validated_at"), 'updated_at': now_str}, '$setOnInsert': {'created_at': m.get("created_at", now_str)}}, upsert=True)
            
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


def deprecate_canonical_memories(memory_ids: List[str]):
    if not memory_ids:
        return
    _ensure_schema()
    with get_db() as db:
        now_str = datetime.now(timezone.utc).isoformat()
        for mid in memory_ids:
            mongo_store.update_docs('canonical_memories', {'id': mid}, {'$set': {'status': 'deprecated', 'updated_at': now_str}})
        logger.info(f"Deprecated {len(memory_ids)} canonical memories.")


def mark_observations_promoted(observation_ids: List[str]):
    if not observation_ids:
        return
    _ensure_schema()
    with get_db() as db:
        for oid in observation_ids:
            mongo_store.update_docs('episodic_observations', {'id': oid}, {'$set': {'promoted_to_memory': True}})
        logger.info(f"Marked {len(observation_ids)} observations as promoted.")


def log_consolidation_run(record: Dict[str, Any]):
    _ensure_schema()
    with get_db() as db:
        mongo_store.insert_docs('consolidation_reports', [{'id': record.get("id"), 'run_at': record.get("run_at", datetime.now(timezone.utc).isoformat()), 'ticker': record.get("ticker"), 'observations_consumed': record.get("observations_consumed", 0), 'memories_created': record.get("memories_created", 0), 'memories_deprecated': record.get("memories_deprecated", 0)}])


def update_memory_validation_stats(
    memory_id: str, new_confidence: float, new_evidence_count: int, new_status: str
):
    """Specific targeted update for validation pipeline."""
    _ensure_schema()
    with get_db() as db:
        now_str = datetime.now(timezone.utc).isoformat()
        mongo_store.update_docs('canonical_memories', {'id': memory_id}, {'$set': {'confidence_score': new_confidence, 'evidence_count': new_evidence_count, 'status': new_status, 'last_validated_at': now_str, 'updated_at': now_str}})
