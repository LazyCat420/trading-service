"""
Feature Lineage Store — MongoDB Document Persistence.

Persists extracted feature records and model outputs to the `feature_lineage` collection
in the `trading_bot` database. Connects feature outputs to cycle_id, decision_id,
instrument_id, document/window IDs, and model versions for verifiable provenance.
"""

from __future__ import annotations

import datetime
import logging
import uuid
from typing import Any, Optional

from app.db import mongo_store

logger = logging.getLogger(__name__)

COLLECTION_NAME = "feature_lineage"
_INDEXES_INITIALIZED = False


def _ensure_indexes(col) -> None:
    global _INDEXES_INITIALIZED
    if _INDEXES_INITIALIZED:
        return
    try:
        col.create_index([("cycle_id", 1), ("instrument_id", 1)], background=True)
        col.create_index([("model_id", 1), ("created_at", -1)], background=True)
        col.create_index([("input_hash", 1)], background=True)
        col.create_index([("document_id", 1)], background=True)
        _INDEXES_INITIALIZED = True
    except Exception as e:
        logger.debug("[feature_lineage_store] Index creation deferred: %s", e)


def record_feature(
    cycle_id: str,
    instrument_id: str,
    model_id: str,
    payload: dict[str, Any],
    input_hash: str,
    *,
    model_version: str = "v1",
    schema_version: str = "1",
    decision_id: str | None = None,
    document_id: str | None = None,
    market_window_id: str | None = None,
    source_data_timestamp: str | None = None,
    request_id: str | None = None,
    latency_ms: int = 0,
    mode: str = "shadow",
) -> str:
    """
    Persists an immutable feature extraction record into MongoDB.

    Returns the generated feature record ID.
    """
    feature_id = f"feat-{uuid.uuid4().hex[:12]}"
    now_utc = datetime.datetime.now(datetime.timezone.utc)

    doc = {
        "_id": feature_id,
        "feature_id": feature_id,
        "cycle_id": cycle_id or "",
        "decision_id": decision_id or None,
        "instrument_id": instrument_id.upper() if instrument_id else "",
        "document_id": document_id or None,
        "market_window_id": market_window_id or None,
        "model_id": model_id,
        "model_version": model_version,
        "schema_version": schema_version,
        "input_hash": input_hash,
        "source_data_timestamp": source_data_timestamp or now_utc.isoformat(),
        "request_id": request_id or str(uuid.uuid4()),
        "latency_ms": latency_ms,
        "mode": mode,  # "shadow" | "advisory" | "promoted"
        "created_at": now_utc,
        "payload": payload,
    }

    try:
        db = mongo_store.get_doc_db()
        col = db[COLLECTION_NAME]
        _ensure_indexes(col)
        col.insert_one(doc)
        logger.debug(
            "[feature_lineage_store] Recorded feature %s for %s (%s, mode=%s)",
            feature_id, instrument_id, model_id, mode,
        )
    except Exception as e:
        logger.warning(
            "[feature_lineage_store] Failed to record feature for %s (fail-open): %s",
            instrument_id, e,
        )

    return feature_id


def get_features_for_cycle(
    cycle_id: str,
    instrument_id: str | None = None,
    model_id: str | None = None,
) -> list[dict[str, Any]]:
    """Retrieves feature records associated with a cycle."""
    try:
        db = mongo_store.get_doc_db()
        col = db[COLLECTION_NAME]
        query: dict[str, Any] = {"cycle_id": cycle_id}
        if instrument_id:
            query["instrument_id"] = instrument_id.upper()
        if model_id:
            query["model_id"] = model_id
        return list(col.find(query, {"_id": 0}))
    except Exception as e:
        logger.warning("[feature_lineage_store] Query failed for cycle %s: %s", cycle_id, e)
        return []


def get_features_for_document(document_id: str) -> list[dict[str, Any]]:
    """Retrieves feature records associated with a document_id."""
    try:
        db = mongo_store.get_doc_db()
        col = db[COLLECTION_NAME]
        return list(col.find({"document_id": document_id}, {"_id": 0}))
    except Exception as e:
        logger.warning("[feature_lineage_store] Query failed for document %s: %s", document_id, e)
        return []
