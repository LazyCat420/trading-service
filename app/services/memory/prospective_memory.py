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

        with get_db() as db:
            # We also might want to pull 'global' triggers
            rows = db.execute(
                """
                SELECT id, ticker, intention, trigger_condition, priority, context
                FROM prospective_memory
                WHERE (ticker = %s OR ticker = 'global') AND status = 'pending'
                ORDER BY 
                    CASE priority
                        WHEN 'critical' THEN 1
                        WHEN 'high' THEN 2
                        WHEN 'medium' THEN 3
                        WHEN 'low' THEN 4
                        ELSE 5
                    END
                LIMIT 3
            """,
                [ticker],
            ).fetchall()

            results = []
            for r in rows:
                results.append(
                    {
                        "id": r[0],
                        "ticker": r[1],
                        "intention": r[2],
                        "trigger_condition": r[3],
                        "priority": r[4],
                        "context": r[5],
                    }
                )
            return results

    def mark_triggered(self, mem_id: str):
        """Mark an item as triggered so it's no longer pending."""

        mongo_store.update_docs('prospective_memory', {'id': mem_id}, {'$set': {'status': 'triggered'}})


# Singleton instance
prospective_memory_store = ProspectiveMemoryStore()
