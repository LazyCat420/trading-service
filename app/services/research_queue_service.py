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

        pending = mongo_store.find_docs('v3_research_queues',
            {'ticker': ticker, 'queue_type': queue_type.value,
             'status': {'$in': ['pending', 'processing', 'answer_ready']}}, limit=100)
        from app.services.question_ledger import question_hash
        question = (payload or {}).get('question') or reason
        identity = ((payload or {}).get('question_hash') or question_hash(question)) if (payload or {}).get('question') or (payload or {}).get('question_hash') else 'ticker_scope'
        existing = None
        for candidate in pending:
            old_payload = candidate.get('payload') or {}
            if isinstance(old_payload, str):
                try:
                    old_payload = json.loads(old_payload)
                except (ValueError, TypeError):
                    old_payload = {}
            old_identity = (old_payload.get('question_hash') or question_hash(old_payload.get('question'))) if old_payload.get('question') or old_payload.get('question_hash') else 'ticker_scope'
            if old_identity == identity:
                existing = (candidate['id'], candidate['status'])
                break
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
                {'queue_type': queue_type.value, 'status': 'pending',
                 '$or': [{'next_attempt_at': None}, {'next_attempt_at': {'$lte': datetime.now(timezone.utc)}}]},
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
        claimed = []
        now = datetime.now(timezone.utc)
        for item in worklist:
            token = uuid.uuid4().hex
            doc = mongo_store.find_one_and_update('v3_research_queues',
                {'id': item["id"], 'status': 'pending'},
                {'$set': {'status': 'processing', 'updated_at': now, 'lease_token': token},
                 '$inc': {'attempts': 1}})
            if doc:
                claimed.append({**item, 'lease_token': token, 'attempts': doc.get('attempts', 1)})
        return claimed

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
                changed = mongo_store.update_docs('v3_research_queues', {'id': r[0], 'status': 'processing', 'updated_at': {'$lt': cutoff}}, {'$set': {'status': 'failed', 'updated_at': now, 'last_error': 'lease expired after maximum attempts'}})
                if changed:
                    failed.append(r[0])

            requeued_rows = mongo_query.find_rows(
                'v3_research_queues',
                {'status': 'processing', 'updated_at': {'$lt': cutoff}, 'attempts': {'$lt': MAX_ATTEMPTS}},
                ['id']
            )
            for r in requeued_rows:
                changed = mongo_store.update_docs('v3_research_queues', {'id': r[0], 'status': 'processing', 'updated_at': {'$lt': cutoff}}, {'$set': {'status': 'pending', 'updated_at': now, 'last_error': 'expired lease reclaimed'}})
                if changed:
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

    @classmethod
    def claim_for_ticker(cls, ticker: str, cycle_id: str, limit: int = 3) -> List[Dict[str, Any]]:
        """Claim questions only for a ticker this cycle is actually about to run.

        Atomic pending->processing CAS; the lease token prevents an old worker
        from completing a question after it was reclaimed by a newer cycle.
        No new cycle or order is created by this consumer.
        """
        from app.services.cycle_scope import is_synthetic_cycle
        if is_synthetic_cycle(cycle_id):
            return []
        cls.deliver_ready_answers()
        cls.reclaim_stale()
        claimed = []
        now = datetime.now(timezone.utc)
        for _ in range(max(0, min(3, limit))):
            token = uuid.uuid4().hex
            doc = mongo_store.find_one_and_update('v3_research_queues', {
                'ticker': ticker.upper(), 'status': 'pending',
                '$or': [{'next_attempt_at': None}, {'next_attempt_at': {'$lte': now}}],
            }, {'$set': {'status': 'processing', 'owner_cycle_id': cycle_id,
                         'lease_token': token, 'updated_at': now}, '$inc': {'attempts': 1}},
                sort=[('priority', -1), ('created_at', 1)])
            if doc is None:
                break
            payload = doc.get('payload') or {}
            if isinstance(payload, str):
                try:
                    payload = json.loads(payload)
                except (ValueError, TypeError):
                    payload = {}
            doc['payload'] = payload if isinstance(payload, dict) else {}
            claimed.append(doc)
        return claimed

    @staticmethod
    def _claim_filter(item: dict) -> dict:
        return {'id': item['id'], 'status': 'processing',
                'owner_cycle_id': item.get('owner_cycle_id'), 'lease_token': item.get('lease_token')}

    @classmethod
    def heartbeat_claims(cls, items: list[dict]) -> None:
        for item in items:
            mongo_store.update_docs('v3_research_queues', cls._claim_filter(item),
                                    {'$set': {'updated_at': datetime.now(timezone.utc)}})

    @classmethod
    def finish_claim(cls, item: dict, *, answer: dict | None = None,
                     reason: str = 'No evidenced answer was produced') -> bool:
        now = datetime.now(timezone.utc)
        if answer:
            if not answer.get('artifact_ref') or not answer.get('evidence') or not answer.get('answer'):
                return False
            # Durable delivery outbox: a ledger outage cannot lose the answer
            # or falsely mark it delivered. The next consumer retries delivery.
            doc = mongo_store.find_one_and_update('v3_research_queues', cls._claim_filter(item),
                {'$set': {'status': 'answer_ready', 'answer': answer, 'updated_at': now}})
            if not doc:
                return False
            return cls.deliver_ready_answers(item_id=item['id']) == 1
        failed = int(item.get('attempts') or 0) >= MAX_ATTEMPTS
        doc = mongo_store.find_one_and_update('v3_research_queues', cls._claim_filter(item),
            {'$set': {'status': 'failed' if failed else 'pending', 'updated_at': now,
                      'next_attempt_at': now + timedelta(hours=12), 'last_error': reason[:500]}})
        return doc is not None

    @classmethod
    def deliver_ready_answers(cls, item_id: str | None = None) -> int:
        """Idempotently deliver stored answers before marking their items done."""
        query = {'status': 'answer_ready'}
        if item_id:
            query['id'] = item_id
        delivered = 0
        for doc in mongo_store.find_docs('v3_research_queues', query, limit=20):
            answer = doc.get('answer') or {}
            payload = doc.get('payload') or {}
            if isinstance(payload, str):
                payload = json.loads(payload)
            from app.services.question_ledger import question_hash
            question = payload.get('question') or doc.get('reason') or ''
            qhash = payload.get('question_hash') or question_hash(question)
            now = datetime.now(timezone.utc)
            # Idempotent across a crash between this write and the completion CAS.
            mongo_store.upsert_doc('dossier_question_log',
                {'ticker': doc['ticker'], 'question_hash': qhash},
                {'ticker': doc['ticker'], 'question_hash': qhash, 'question': question,
                 'status': 'answered', 'answer': answer['answer'],
                 'evidence_ref': answer['artifact_ref'], 'evidence': answer['evidence'],
                 'question_asked_at': doc.get('created_at'),
                 'resolved_cycle': doc.get('owner_cycle_id'), 'resolved_at': now})
            cls._remove_answered_question(doc['ticker'], qhash)
            delivered += mongo_store.update_docs('v3_research_queues',
                {'id': doc['id'], 'status': 'answer_ready', 'lease_token': doc.get('lease_token')},
                {'$set': {'status': 'completed', 'updated_at': now, 'completed_at': now}})
        return delivered

    @staticmethod
    def _remove_answered_question(ticker: str, qhash: str) -> None:
        """Prune only this answer, preserving concurrent dossier changes."""
        from app.services.question_ledger import question_hash
        for _ in range(3):
            rows = mongo_store.find_docs('ticker_dossiers', {'ticker': ticker},
                                        projection={'open_questions': 1}, limit=1)
            if not rows:
                return
            original = rows[0].get('open_questions') or []
            questions = json.loads(original) if isinstance(original, str) else original
            remaining = [q for q in questions if not isinstance(q, str) or question_hash(q) != qhash]
            if remaining == questions:
                return
            if mongo_store.update_docs('ticker_dossiers',
                    {'ticker': ticker, 'open_questions': original},
                    {'$set': {'open_questions': remaining, 'updated_at': datetime.now(timezone.utc)}}):
                return
        # Keep the durable outbox pending so the next delivery can retry.
        raise RuntimeError('Dossier changed during research-answer delivery')
