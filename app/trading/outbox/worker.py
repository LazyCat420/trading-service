"""Outbox Worker for Asynchronous Measured Reconciliation and Attribution."""

from __future__ import annotations

import asyncio
import datetime
import logging
from typing import Any, Optional

from app.db import mongo_store
from app.trading.attribution.models import ExecutionIntent, OrderAttempt
from app.trading.attribution.reconciliation import reconcile_execution
from app.trading.attribution.repository import (
    COLL_EXECUTION_INTENTS,
    COLL_ORDER_ATTEMPTS,
    save_execution_reconciliation,
)
from app.trading.outbox.repository import (
    claim_pending_outbox_events,
    mark_outbox_event_completed,
    mark_outbox_event_failed,
)

logger = logging.getLogger(__name__)


def process_outbox_event(event: dict[str, Any]) -> dict[str, Any]:
    """Processes a single outbox event, executing reconciliation and attribution."""
    event_id = event["event_id"]
    payload = event.get("payload", {})
    intent_id = payload.get("intent_id")

    if not intent_id:
        raise ValueError(f"Outbox event {event_id} payload missing intent_id")

    # 1. Fetch ExecutionIntent
    intent_docs = mongo_store.find_docs(COLL_EXECUTION_INTENTS, {"execution_intent_id": intent_id}, limit=1)
    if not intent_docs:
        raise ValueError(f"Execution intent {intent_id} not found for outbox event {event_id}")

    intent = ExecutionIntent.model_validate(intent_docs[0])

    # 2. Fetch Order Attempts
    attempt_docs = mongo_store.find_docs(COLL_ORDER_ATTEMPTS, {"execution_intent_id": intent_id})
    attempts = [OrderAttempt.model_validate(d) for d in attempt_docs]

    # 3. Fetch Orders & Fills
    order_id = payload.get("order_id")
    order_docs = mongo_store.find_docs("orders", {"order_id": order_id}, limit=1)
    order = order_docs[0] if order_docs else {"order_id": order_id}

    fills = mongo_store.find_docs("trade_fills", {"execution_intent_id": intent_id})
    if not fills and order_id:
        fills = mongo_store.find_docs("trade_fills", {"order_id": order_id})

    # If fills still empty, fallback to payload values
    if not fills:
        fills = [
            {
                "qty": payload.get("fill_qty", 0.0),
                "price": payload.get("fill_price", 0.0),
                "fees": payload.get("fees", 0.0),
                "filled_at": payload.get("executed_at"),
            }
        ]

    # 4. Pure Measured Reconciliation
    rec = reconcile_execution(
        intent=intent,
        attempts=attempts,
        order=order,
        fills=fills,
        reference_quote={"price": payload.get("reference_price")},
    )

    # 5. Persist Reconciliation Record
    save_execution_reconciliation(rec)

    logger.info(
        "[OutboxWorker] Event %s processed: intent=%s verdict=%s slippage=%.1fbps",
        event_id, intent_id, rec.verdict.value, rec.realized_slippage_bps,
    )
    return {"verdict": rec.verdict.value, "reconciliation_id": rec.reconciliation_id}


def run_outbox_worker_iteration(batch_size: int = 10) -> int:
    """Runs a single iteration of the outbox worker loop."""
    try:
        db = mongo_store.get_doc_db()
        db["worker_heartbeats"].update_one(
            {"worker": "outbox_worker"},
            {"$set": {"last_heartbeat": datetime.datetime.now(datetime.timezone.utc), "status": "RUNNING"}},
            upsert=True,
        )
    except Exception:
        pass

    events = claim_pending_outbox_events(batch_size=batch_size)
    if not events:
        return 0

    processed_count = 0
    for event in events:
        event_id = event["event_id"]
        worker_token = event.get("locked_by")
        try:
            res = process_outbox_event(event)
            mark_outbox_event_completed(event_id, result_payload=res, worker_token=worker_token)
            processed_count += 1
        except Exception as exc:
            logger.exception("[OutboxWorker] Failed processing event %s: %s", event_id, exc)
            mark_outbox_event_failed(event_id, error_msg=str(exc), worker_token=worker_token)

    return processed_count


async def start_outbox_worker_loop(poll_interval_seconds: float = 2.0) -> None:
    """Async background worker loop meant to run within service lifespan."""
    logger.info("[OutboxWorker] Starting background outbox processing loop...")
    while True:
        try:
            count = run_outbox_worker_iteration(batch_size=10)
            if count == 0:
                await asyncio.sleep(poll_interval_seconds)
            else:
                # If we processed a batch, yield control then poll immediately for more
                await asyncio.sleep(0.1)
        except asyncio.CancelledError:
            logger.info("[OutboxWorker] Worker loop received cancellation — stopping gracefully")
            break
        except Exception as e:
            logger.error("[OutboxWorker] Error in worker loop: %s", e)
            await asyncio.sleep(poll_interval_seconds)
