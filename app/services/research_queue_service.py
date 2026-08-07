"""
Research Queue Service — Autonomous Worklist Scheduling plane.

Manages explicit queues (lead_queue, deep_dive_queue, monitor_queue, exit_review_queue)
and constructs balanced cycle worklists for agent processing.
"""

import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from app.db.connection import get_db
from app.schemas.dossier_schemas import QueueItem, QueueType

logger = logging.getLogger(__name__)


class ResearchQueueService:

    @classmethod
    def enqueue_item(
        cls,
        ticker: str,
        queue_type: QueueType,
        reason: str,
        source_agent: str,
        priority: int = 0,
        payload: Optional[Dict[str, Any]] = None,
    ) -> str:
        """Enqueues a ticker item. Returns item ID. Deduplicates pending items for same ticker and queue."""
        ticker = ticker.upper().strip()

        with get_db() as db:
            # Dedupe check
            existing = db.execute(
                "SELECT id FROM v3_research_queues "
                "WHERE ticker = %s AND queue_type = %s AND status = 'pending'",
                [ticker, queue_type.value],
            ).fetchone()
            if existing:
                logger.info("[queue] Ticker %s already pending in %s, skipping dedupe", ticker, queue_type.value)
                return existing[0]

            item_id = f"qitem-{uuid.uuid4().hex[:12]}"
            now = datetime.now(timezone.utc).isoformat()
            payload_json = json.dumps(payload or {})

            db.execute(
                """
                INSERT INTO v3_research_queues (
                    id, ticker, queue_type, priority, reason, source_agent, status, payload, created_at, updated_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                [
                    item_id,
                    ticker,
                    queue_type.value,
                    priority,
                    reason,
                    source_agent,
                    "pending",
                    payload_json,
                    now,
                    now,
                ],
            )
        logger.info("[queue] Enqueued %s into %s by %s (reason: %s)", ticker, queue_type.value, source_agent, reason)
        return item_id

    @classmethod
    def _select(cls, db, budget: int) -> List[Dict[str, Any]]:
        """Balanced selection across the four queues. Reads only.

        Shared by `peek_worklist` and `pop_worklist` so the shadow comparison
        cannot drift from what a live pop would actually take. A shadow that
        re-implements the selection is measuring a second implementation, not
        the one that would run.
        """
        worklist: List[Dict[str, Any]] = []

        # Queue allocation targets
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
            rows = db.execute(
                "SELECT id, ticker, queue_type, priority, reason, source_agent, payload "
                "FROM v3_research_queues "
                "WHERE queue_type = %s AND status = 'pending' "
                "ORDER BY priority DESC, created_at ASC LIMIT %s",
                [queue_type.value, needed * 2],
            ).fetchall()

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
        """The worklist a pop WOULD return, without claiming anything.

        `pop_worklist` moves every row it returns to `processing`, so calling it
        to compute a shadow comparison would drain the queue into a state no
        worker is serving yet — the shadow would consume the thing it is
        measuring. Peek exists so the comparison can run before the consumer
        does. Never raises; an empty list means "no queue", which is the honest
        reading while nothing is enqueuing.
        """
        try:
            with get_db() as db:
                return cls._select(db, budget)
        except Exception as e:
            logger.warning("[queue] peek_worklist failed: %s", e)
            return []

    @classmethod
    def pop_worklist(cls, budget: int = 6) -> List[Dict[str, Any]]:
        """
        Pulls a balanced worklist from active queues up to the total ticker budget.
        Priority order: exit_review_queue > monitor_queue > deep_dive_queue > lead_queue.

        Claims what it returns: every returned row moves to `processing`. Use
        `peek_worklist` for anything observational.
        """
        with get_db() as db:
            worklist = cls._select(db, budget)
            for item in worklist:
                db.execute(
                    "UPDATE v3_research_queues SET status = 'processing', updated_at = NOW() WHERE id = %s",
                    [item["id"]],
                )
        return worklist

    @classmethod
    def complete_item(cls, item_id: str) -> None:
        """Marks a queue item as completed."""
        with get_db() as db:
            db.execute(
                "UPDATE v3_research_queues SET status = 'completed', updated_at = NOW() WHERE id = %s",
                [item_id],
            )

    @classmethod
    def get_queue_summary(cls) -> Dict[str, int]:
        """Returns count of pending items per queue type."""
        with get_db() as db:
            rows = db.execute(
                "SELECT queue_type, COUNT(*) FROM v3_research_queues WHERE status = 'pending' GROUP BY queue_type"
            ).fetchall()
        summary = {qt.value: 0 for qt in QueueType}
        for q_type, count in rows:
            summary[q_type] = count
        return summary
