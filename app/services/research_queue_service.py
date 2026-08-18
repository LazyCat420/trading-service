"""
Research Queue Service — Autonomous Worklist Scheduling plane.

Pure MongoDB implementation for v3_research_queues collection.
"""

import json
import logging
import uuid
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional

from app.schemas.dossier_schemas import QueueItem, QueueType
from app.db import mongo_query, mongo_store

logger = logging.getLogger(__name__)

RECLAIM_AFTER_SECONDS = 1800
MAX_ATTEMPTS = 3


class ResearchQueueService:

    @classmethod
    def enqueue_item(
        cls,
        ticker: str,
        queue_type: QueueType,
        priority: int = 50,
        reason: str = "",
        source_agent: str = "",
        payload: Optional[Dict[str, Any]] = None,
    ) -> str:
        """Enqueues a ticker item into the specified queue."""
        ticker = ticker.upper().strip()

        existing = mongo_query.find_row(
            'v3_research_queues',
            {'ticker': ticker, 'queue_type': queue_type.value, 'status': {'$in': ['pending', 'processing']}},
            ['id', 'status']
        )
        if existing:
            logger.info("[queue] Ticker %s already %s in %s, skipping dedupe",
                        ticker, existing[1], queue_type.value)
            return existing[0]

        item_id = f"qitem-{uuid.uuid4().hex[:12]}"
        now = datetime.now(timezone.utc)
        payload_json = json.dumps(payload or {})

        mongo_store.insert_docs('v3_research_queues', [{
            'id': item_id,
            'ticker': ticker,
            'queue_type': queue_type.value,
            'priority': priority,
            'reason': reason,
            'source_agent': source_agent,
            'status': "pending",
            'payload': payload_json,
            'attempts': 0,
            'created_at': now,
            'updated_at': now,
        }])
        logger.info("[queue] Enqueued %s into %s by %s (reason: %s)", ticker, queue_type.value, source_agent, reason)
        return item_id

    @classmethod
    def _select(cls, budget: int) -> List[Dict[str, Any]]:
        """Balanced selection across the four queues. Reads only."""
        worklist: List[Dict[str, Any]] = []

        targets = [
            (QueueType.EXIT_REVIEW_QUEUE, max(1, budget // 4)),
            (QueueType.MONITOR_QUEUE, max(1, budget // 3)),
            (QueueType.DEEP_DIVE_QUEUE, max(1, budget // 3)),
            (QueueType.LEAD_QUEUE, max(1, budget // 4)),
        ]

        seen_tickers = set()

        for queue_type, target_count in targets:
            if len(worklist) >= budget:
                break
            needed = min(target_count, budget - len(worklist))
            rows = mongo_query.find_rows(
                'v3_research_queues',
                {'queue_type': queue_type.value, 'status': 'pending'},
                ['id', 'ticker', 'queue_type', 'priority', 'reason', 'source_agent', 'payload'],
                sort=[('priority', -1), ('created_at', 1)],
                limit=needed * 2
            )

            for r in rows:
                item_id, tkr, q_type, prio, reason, src_agent, payload_raw = r
                if tkr in seen_tickers:
                    continue
                seen_tickers.add(tkr)

                worklist.append({
                    "id": item_id,
                    "ticker": tkr,
                    "queue_type": q_type,
                    "priority": prio,
                    "reason": reason,
                    "source_agent": src_agent,
                    "payload": json.loads(payload_raw) if isinstance(payload_raw, str) else (payload_raw or {}),
                })
                if len(worklist) >= budget:
                    break

        return worklist

    @classmethod
    def peek_worklist(cls, budget: int = 6) -> List[Dict[str, Any]]:
        """The worklist a pop WOULD return, without claiming anything."""
        try:
            return cls._select(budget)
        except Exception as e:
            logger.warning("[queue] peek_worklist failed: %s", e)
            return []

    @classmethod
    def pop_worklist(cls, budget: int = 6) -> List[Dict[str, Any]]:
        """Pulls a balanced worklist from active queues up to the total ticker budget."""
        cls.reclaim_stale()

        worklist = cls._select(budget)
        now = datetime.now(timezone.utc)
        for item in worklist:
            mongo_store.update_docs(
                'v3_research_queues',
                {'id': item["id"]},
                {'$set': {'status': 'processing', 'updated_at': now}, '$inc': {'attempts': 1}}
            )
        return worklist

    @classmethod
    def heartbeat(cls, item_id: str) -> bool:
        """Stamps a claim as still live."""
        now = datetime.now(timezone.utc)
        mongo_store.update_docs(
            'v3_research_queues',
            {'id': item_id, 'status': 'processing'},
            {'$set': {'updated_at': now}}
        )
        row = mongo_query.find_row('v3_research_queues', {'id': item_id, 'status': 'processing'}, ['id'])
        return row is not None

    @classmethod
    def reset_item(cls, item_id: str, reason: str = "worker handed it back") -> None:
        """Explicitly returns a claimed item to `pending`."""
        mongo_store.update_docs(
            'v3_research_queues',
            {'id': item_id, 'status': 'processing'},
            {'$set': {'status': 'pending', 'updated_at': datetime.now(timezone.utc)}}
        )
        logger.info("[queue] Reset %s to pending (%s)", item_id, reason)

    @classmethod
    def reclaim_stale(cls, timeout_seconds: int = RECLAIM_AFTER_SECONDS) -> Dict[str, List[str]]:
        """Returns claims nobody is holding, and fails the ones that keep dying."""
        requeued: List[str] = []
        failed: List[str] = []
        try:
            now = datetime.now(timezone.utc)
            cutoff = now - timedelta(seconds=timeout_seconds)

            failed_rows = mongo_query.find_rows(
                'v3_research_queues',
                {'status': 'processing', 'updated_at': {'$lt': cutoff}, 'attempts': {'$gte': MAX_ATTEMPTS}},
                ['id']
            )
            for r in failed_rows:
                mongo_store.update_docs('v3_research_queues', {'id': r[0]}, {'$set': {'status': 'failed', 'updated_at': now}})
                failed.append(r[0])

            requeued_rows = mongo_query.find_rows(
                'v3_research_queues',
                {'status': 'processing', 'updated_at': {'$lt': cutoff}, 'attempts': {'$lt': MAX_ATTEMPTS}},
                ['id']
            )
            for r in requeued_rows:
                mongo_store.update_docs('v3_research_queues', {'id': r[0]}, {'$set': {'status': 'pending', 'updated_at': now}})
                requeued.append(r[0])
        except Exception as e:
            logger.warning("[queue] reclaim_stale failed: %s", e)
            return {"requeued": [], "failed": []}

        if requeued:
            logger.warning("[queue] Reclaimed %d stale claim(s) after %ds: %s",
                           len(requeued), timeout_seconds, requeued)
        if failed:
            logger.error("[queue] Failed %d item(s) after %d attempts: %s",
                         len(failed), MAX_ATTEMPTS, failed)
        return {"requeued": requeued, "failed": failed}

    @classmethod
    def complete_item(cls, item_id: str) -> None:
        """Marks a queue item as completed."""
        mongo_store.update_docs(
            'v3_research_queues',
            {'id': item_id},
            {'$set': {'status': 'completed', 'updated_at': datetime.now(timezone.utc)}}
        )

    @classmethod
    def get_queue_summary(cls) -> Dict[str, int]:
        """Returns count of pending items per queue type."""
        summary = {qt.value: 0 for qt in QueueType}
        try:
            rows = mongo_query.group_rows(
                'v3_research_queues',
                {'status': 'pending'},
                ['queue_type'],
                [('count', None)],
                [('key', 'queue_type'), ('agg', 0)]
            )
            for q_type, count in rows:
                summary[q_type] = count
        except Exception:
            pass
        return summary

    @classmethod
    def get_status_counts(cls) -> Dict[str, Dict[str, int]]:
        """`{queue_type: {status: n}}` across every status."""
        out: Dict[str, Dict[str, int]] = {qt.value: {} for qt in QueueType}
        try:
            rows = mongo_query.group_rows(
                'v3_research_queues',
                {},
                ['queue_type', 'status'],
                [('count', None)],
                [('key', 'queue_type'), ('key', 'status'), ('agg', 0)]
            )
            for q_type, status, count in rows:
                out.setdefault(q_type, {})[status] = count
        except Exception:
            pass
        return out
