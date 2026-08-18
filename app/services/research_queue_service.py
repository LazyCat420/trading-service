"""
Research Queue Service — Autonomous Worklist Scheduling plane.

Manages explicit queues (lead_queue, deep_dive_queue, monitor_queue, exit_review_queue)
and constructs balanced cycle worklists for agent processing.

THE ORPHAN PATH (added 2026-08-08, open item 14). `pop_worklist` moves rows to
`processing` and `complete_item` moves them to `completed`; a worker that dies
between the two used to strand its items at `processing` forever. Worse, the
dedupe in `enqueue_item` only looked at `pending`, so a stranded item did not
block a new one — the failure surfaced as **silent duplicates rather than a
visible stall**, which is the harder of the two to notice.

This is open item 5 in a new place: `pipeline_state` could strand a cycle at
`running`, and the fix there was to judge a real heartbeat rather than a start
time. The same fix applies, with the same caveat made explicit: `updated_at` is
a true heartbeat only for a worker that calls `heartbeat()`. For one that does
not, it is the claim time, and `RECLAIM_AFTER_SECONDS` must then exceed the
longest legitimate run or a live worker's item is requeued underneath it.

**The guard is armed by `pop_worklist`, so a queue nothing pops neither strands
nor reclaims.** That is the state today — the 7 items in `deep_dive_queue` have
no consumer. The first consumer to land gets the orphan path already in place,
which is the whole reason for doing this before it rather than after.

A permanently poisonous item would otherwise requeue forever and silently: past
`MAX_ATTEMPTS` a reclaim moves it to `failed` instead, so an item that cannot be
processed becomes a visible terminal state rather than an invisible loop.
"""

import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from app.db.connection import get_db
from app.schemas.dossier_schemas import QueueItem, QueueType
from app.db import mongo_query, mongo_store

logger = logging.getLogger(__name__)

#: How long a row may sit at `processing` without a write before a reclaim
#: takes it back. Deliberately generous: a research run decomposes, fans out to
#: subagents and synthesizes, and the cost of the two errors is asymmetric — too
#: short requeues an item underneath a live worker and duplicates real work, too
#: long only delays recovery of an item nobody is holding. Workers that call
#: `heartbeat()` are judged on a real heartbeat and can be reclaimed sooner.
RECLAIM_AFTER_SECONDS = 1800

#: After this many claims an item is failed rather than requeued. Without it a
#: worker that dies on one specific item recreates the stall it was meant to
#: fix, one reclaim at a time, with nothing to see in the queue depth.
MAX_ATTEMPTS = 3


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
        """Enqueues a ticker item. Returns item ID.

        Deduplicates against `pending` **and** `processing`. An item a worker is
        holding is still queued from the caller's point of view, and the
        `pending`-only check meant a stranded row produced a silent duplicate
        instead of a visible stall (open item 14).
        """
        ticker = ticker.upper().strip()

        with get_db() as db:
            # Dedupe check
            existing = mongo_query.find_row('v3_research_queues', {'ticker': ticker, 'queue_type': queue_type.value, 'status': {'$in': ['pending', 'processing']}}, ['id', 'status'])
            if existing:
                logger.info("[queue] Ticker %s already %s in %s, skipping dedupe",
                            ticker, existing[1], queue_type.value)
                return existing[0]

            item_id = f"qitem-{uuid.uuid4().hex[:12]}"
            now = datetime.now(timezone.utc).isoformat()
            payload_json = json.dumps(payload or {})

            mongo_store.insert_docs('v3_research_queues', [{'id': item_id, 'ticker': ticker, 'queue_type': queue_type.value, 'priority': priority, 'reason': reason, 'source_agent': source_agent, 'status': "pending", 'payload': payload_json, 'created_at': now, 'updated_at': now}])
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
            rows = mongo_query.find_rows('v3_research_queues', {'queue_type': queue_type.value, 'status': 'pending'}, ['id', 'ticker', 'queue_type', 'priority', 'reason', 'source_agent', 'payload'], sort=[('priority', -1), ('created_at', 1)], limit=needed * 2)

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

        Claims what it returns: every returned row moves to `processing` and its
        `attempts` counter increments. Use `peek_worklist` for anything
        observational.

        **Reclaims stale claims first.** This is where the orphan path is armed:
        a queue nothing pops neither strands nor reclaims, so the guard costs
        nothing until there is a consumer and is already in place when one
        arrives.
        """
        cls.reclaim_stale()

        with get_db() as db:
            worklist = cls._select(db, budget)
            for item in worklist:
                db.execute(
                    "UPDATE v3_research_queues "
                    "SET status = 'processing', attempts = attempts + 1, updated_at = NOW() "
                    "WHERE id = %s",
                    [item["id"]],
                )
        return worklist

    @classmethod
    def heartbeat(cls, item_id: str) -> bool:
        """Stamps a claim as still live. Returns False if the row is no longer
        `processing` — i.e. it was reclaimed or completed underneath the caller.

        A worker that ignores the return value is a worker that keeps working on
        an item somebody else now owns, which is the duplicate this whole path
        exists to prevent. Without a caller, `updated_at` on a `processing` row
        is the claim time, and `RECLAIM_AFTER_SECONDS` is sized for that case.
        """
        with get_db() as db:
            row = db.execute(
                "UPDATE v3_research_queues SET updated_at = NOW() "
                "WHERE id = %s AND status = 'processing' RETURNING id",
                [item_id],
            ).fetchone()
        if row is None:
            logger.warning("[queue] heartbeat for %s found no processing row — "
                           "the claim was reclaimed or completed elsewhere", item_id)
        return row is not None

    @classmethod
    def reset_item(cls, item_id: str, reason: str = "worker handed it back") -> None:
        """Explicitly returns a claimed item to `pending`.

        For the case a worker can see: it caught an exception and knows it will
        not finish. Cheaper and far more legible than waiting out
        `RECLAIM_AFTER_SECONDS`.
        """
        with get_db() as db:
            mongo_store.update_docs('v3_research_queues', {'id': item_id, 'status': 'processing'}, {'$set': {'status': 'pending', 'updated_at': datetime.now(timezone.utc)}})
        logger.info("[queue] Reset %s to pending (%s)", item_id, reason)

    @classmethod
    def reclaim_stale(cls, timeout_seconds: int = RECLAIM_AFTER_SECONDS) -> Dict[str, List[str]]:
        """Returns claims nobody is holding, and fails the ones that keep dying.

        Judged on `updated_at`, never on `created_at`: the same heartbeat
        judgement that closed open item 5, where a `started_at > 30min` rule
        both skipped healthy cycles and force-reset live ones.

        Counts come from `RETURNING`, not `rowcount` — the pooled cursor this
        service uses does not carry one, and reading it silently reports zero
        (`5cec538`).
        """
        requeued: List[str] = []
        failed: List[str] = []
        try:
            with get_db() as db:
                # Past MAX_ATTEMPTS first, so an item at the limit is failed by
                # this pass rather than requeued by it and failed by the next.
                failed = [r[0] for r in db.execute(
                    "UPDATE v3_research_queues SET status = 'failed', updated_at = NOW() "
                    "WHERE status = 'processing' "
                    "AND updated_at < NOW() - make_interval(secs => %s) "
                    "AND attempts >= %s RETURNING id",
                    [timeout_seconds, MAX_ATTEMPTS],
                ).fetchall()]

                requeued = [r[0] for r in db.execute(
                    "UPDATE v3_research_queues SET status = 'pending', updated_at = NOW() "
                    "WHERE status = 'processing' "
                    "AND updated_at < NOW() - make_interval(secs => %s) "
                    "AND attempts < %s RETURNING id",
                    [timeout_seconds, MAX_ATTEMPTS],
                ).fetchall()]
        except Exception as e:  # noqa: BLE001
            # A reclaim that raises must not take the pop with it: the worst
            # case without it is the stall this replaces, and the worst case
            # with it is no worklist at all.
            logger.warning("[queue] reclaim_stale failed: %s", e)
            return {"requeued": [], "failed": []}

        if requeued:
            logger.warning("[queue] Reclaimed %d stale claim(s) after %ds: %s",
                           len(requeued), timeout_seconds, requeued)
        if failed:
            logger.error("[queue] Failed %d item(s) after %d attempts — these were "
                         "claimed and abandoned repeatedly: %s",
                         len(failed), MAX_ATTEMPTS, failed)
        return {"requeued": requeued, "failed": failed}

    @classmethod
    def complete_item(cls, item_id: str) -> None:
        """Marks a queue item as completed."""
        with get_db() as db:
            mongo_store.update_docs('v3_research_queues', {'id': item_id}, {'$set': {'status': 'completed', 'updated_at': datetime.now(timezone.utc)}})

    @classmethod
    def get_queue_summary(cls) -> Dict[str, int]:
        """Returns count of pending items per queue type.

        Pending only — this is the depth a pop would draw from, and
        `worklist_shadow` stores it as `queue_depth`. Use `get_status_counts`
        to see claims and failures; a summary that reported them together would
        make a stalled queue read as a busy one.
        """
        with get_db() as db:
            rows = mongo_query.group_rows('v3_research_queues', {'status': 'pending'}, ['queue_type'], [('count', None)], [('key', 'queue_type'), ('agg', 0)])
        summary = {qt.value: 0 for qt in QueueType}
        for q_type, count in rows:
            summary[q_type] = count
        return summary

    @classmethod
    def get_status_counts(cls) -> Dict[str, Dict[str, int]]:
        """`{queue_type: {status: n}}` across every status.

        The observability half of the orphan path: a growing `processing` count
        with a flat `completed` count is the stall, and nothing could see it
        before.
        """
        with get_db() as db:
            rows = mongo_query.group_rows('v3_research_queues', {}, ['queue_type', 'status'], [('count', None)], [('key', 'queue_type'), ('key', 'status'), ('agg', 0)])
        out: Dict[str, Dict[str, int]] = {qt.value: {} for qt in QueueType}
        for q_type, status, count in rows:
            out.setdefault(q_type, {})[status] = count
        return out
