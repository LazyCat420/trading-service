"""Transactional Outbox Persistence and Queue Operations."""

from __future__ import annotations

import datetime
import logging
from typing import Any, Optional

from app.db import mongo_store

logger = logging.getLogger(__name__)

COLL_EXECUTION_OUTBOX = "execution_outbox"


def insert_outbox_event(event_doc: dict[str, Any], session: Any = None) -> str:
    """Inserts a new outbox event record within a transaction."""
    db = mongo_store.get_doc_db()
    db[COLL_EXECUTION_OUTBOX].insert_one(event_doc, session=session)
    return str(event_doc.get("event_id"))


def claim_pending_outbox_events(
    batch_size: int = 10,
    lock_timeout_seconds: int = 60,
    session: Any = None,
) -> list[dict[str, Any]]:
    """Claims pending outbox events using atomic status transitions.
    Transitions status from PENDING -> PROCESSING with a lease lock.
    """
    db = mongo_store.get_doc_db()
    col = db[COLL_EXECUTION_OUTBOX]
    now = datetime.datetime.now(datetime.timezone.utc)
    lock_cutoff = now - datetime.timedelta(seconds=lock_timeout_seconds)

    claimed: list[dict[str, Any]] = []

    for _ in range(batch_size):
        event = col.find_one_and_update(
            {
                "$or": [
                    {"status": "PENDING", "$or": [{"retry_after": None}, {"retry_after": {"$lte": now}}]},
                    {"status": "PROCESSING", "locked_at": {"$lt": lock_cutoff}},
                ]
            },
            {
                "$set": {
                    "status": "PROCESSING",
                    "locked_at": now,
                }
            },
            session=session,
        )
        if not event:
            break
        claimed.append(event)

    return claimed


def mark_outbox_event_completed(
    event_id: str,
    result_payload: Optional[dict[str, Any]] = None,
    session: Any = None,
) -> bool:
    """Marks an outbox event as COMPLETED."""
    db = mongo_store.get_doc_db()
    col = db[COLL_EXECUTION_OUTBOX]
    now = datetime.datetime.now(datetime.timezone.utc)

    update_fields: dict[str, Any] = {
        "status": "COMPLETED",
        "processed_at": now,
        "last_error": None,
    }
    if result_payload:
        update_fields["result"] = result_payload

    res = col.update_one(
        {"event_id": event_id},
        {"$set": update_fields},
        session=session,
    )
    return res.modified_count > 0


def mark_outbox_event_failed(
    event_id: str,
    error_msg: str,
    max_attempts: int = 5,
    session: Any = None,
) -> bool:
    """Records an attempt failure. Applies exponential backoff or moves to FAILED."""
    db = mongo_store.get_doc_db()
    col = db[COLL_EXECUTION_OUTBOX]
    now = datetime.datetime.now(datetime.timezone.utc)

    doc = col.find_one({"event_id": event_id}, session=session)
    if not doc:
        return False

    attempts = int(doc.get("attempts", 0)) + 1
    if attempts >= max_attempts:
        # Poison queue state
        new_status = "FAILED"
        retry_after = None
        logger.error("[Outbox] Event %s failed permanently after %d attempts: %s", event_id, attempts, error_msg)
    else:
        new_status = "PENDING"
        backoff_seconds = min(2 ** attempts * 5, 300)
        retry_after = now + datetime.timedelta(seconds=backoff_seconds)
        logger.warning("[Outbox] Event %s failed attempt %d: %s. Retrying after %ds", event_id, attempts, error_msg, backoff_seconds)

    res = col.update_one(
        {"event_id": event_id},
        {
            "$set": {
                "status": new_status,
                "attempts": attempts,
                "last_error": error_msg,
                "retry_after": retry_after,
                "locked_at": None,
            }
        },
        session=session,
    )
    return res.modified_count > 0


def get_outbox_metrics() -> dict[str, Any]:
    """Exposes outbox queue operational telemetry: pending, oldest age, retries, poison count."""
    db = mongo_store.get_doc_db()
    col = db[COLL_EXECUTION_OUTBOX]
    now = datetime.datetime.now(datetime.timezone.utc)

    pending_count = col.count_documents({"status": "PENDING"})
    processing_count = col.count_documents({"status": "PROCESSING"})
    poison_count = col.count_documents({"status": "FAILED"})
    completed_count = col.count_documents({"status": "COMPLETED"})

    oldest = col.find_one({"status": "PENDING"}, sort=[("created_at", 1)])
    oldest_pending_age_seconds = 0.0
    if oldest and oldest.get("created_at"):
        created = oldest["created_at"]
        if created.tzinfo is None:
            created = created.replace(tzinfo=datetime.timezone.utc)
        oldest_pending_age_seconds = max(0.0, (now - created).total_seconds())

    return {
        "pending": pending_count,
        "processing": processing_count,
        "poison_failed": poison_count,
        "completed": completed_count,
        "oldest_pending_age_seconds": round(oldest_pending_age_seconds, 2),
    }


def replay_poison_outbox_event(event_id: str) -> bool:
    """Safe operator replay preserving event identity. Resets a FAILED event to PENDING."""
    db = mongo_store.get_doc_db()
    col = db[COLL_EXECUTION_OUTBOX]
    now = datetime.datetime.now(datetime.timezone.utc)
    res = col.update_one(
        {"event_id": event_id, "status": "FAILED"},
        {
            "$set": {
                "status": "PENDING",
                "retry_after": now,
                "locked_at": None,
                "replayed_at": now,
            }
        },
    )
    return res.modified_count > 0

