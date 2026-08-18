import uuid
import logging
from app.db import mongo_store
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)


class ProspectiveMemoryStore:
    """
    Stores intentions that should trigger in the future (reminders).
    """

    def write_prospective(
        self,
        ticker: str,
        intention: str,
        trigger_condition: str,
        priority: str = "medium",
        trigger_at: str = None,
        context: str = "",
    ) -> str:
        """Store a new prospective memory (future trigger/reminder)."""
        mem_id = str(uuid.uuid4())

        mongo_store.insert_docs('prospective_memory', [{'id': mem_id, 'ticker': ticker, 'intention': intention, 'trigger_condition': trigger_condition, 'priority': priority, 'status': 'pending', 'trigger_at': trigger_at, 'context': context}])

        logger.info(f"[PROSPECTIVE] Wrote reminder for {ticker}: {intention}")
        return mem_id

    def add_reminder(
        self,
        ticker: str,
        condition: str,
        intended_action: str,
        expires_in_days: int = 7,
        priority: str = "medium",
    ) -> str:
        """Compatibility wrapper for the `add_reminder` RLM tool.

        The tool contract (rlm_tools.add_reminder) speaks in
        (condition, intended_action, expires_in_days); map that onto
        write_prospective's (trigger_condition, intention, trigger_at).
        Previously this method did not exist, so every add_reminder tool
        call raised AttributeError (swallowed) and no reminder was stored.
        """
        from datetime import datetime, timedelta, timezone

        trigger_at = None
        context = ""
        try:
            if expires_in_days and expires_in_days > 0:
                expiry = datetime.now(timezone.utc) + timedelta(days=expires_in_days)
                trigger_at = expiry.isoformat()
                context = f"expires_in_days={expires_in_days}"
        except Exception:
            trigger_at = None

        return self.write_prospective(
            ticker=ticker,
            intention=intended_action,
            trigger_condition=condition,
            priority=priority,
            trigger_at=trigger_at,
            context=context,
        )

    def retrieve_pending(self, ticker: str) -> list[dict]:
        """Query pending items for a ticker that should be evaluated."""
        # SQL ordered by a CASE over `priority`; Mongo has no such collation,
        # so the rank is materialised as a computed field and sorted on.
        rank = {'critical': 1, 'high': 2, 'medium': 3, 'low': 4}
        docs = mongo_store.aggregate('prospective_memory', [
            {'$match': {'$or': [{'ticker': ticker}, {'ticker': 'global'}],
                        'status': 'pending'}},
            {'$addFields': {'_prio': {'$switch': {
                'branches': [{'case': {'$eq': ['$priority', p]}, 'then': n}
                             for p, n in rank.items()],
                'default': 5}}}},
            {'$sort': {'_prio': 1}},
            {'$limit': 3},
        ])

        results = []
        for d in docs:
            results.append(
                {
                    "id": d.get("id"),
                    "ticker": d.get("ticker"),
                    "intention": d.get("intention"),
                    "trigger_condition": d.get("trigger_condition"),
                    "priority": d.get("priority"),
                    "context": d.get("context"),
                }
            )
        return results

    def mark_triggered(self, mem_id: str):
        """Mark an item as triggered so it's no longer pending."""
        mongo_store.update_docs('prospective_memory', {'id': mem_id}, {'$set': {'status': 'triggered'}})


# Singleton instance
prospective_memory_store = ProspectiveMemoryStore()
