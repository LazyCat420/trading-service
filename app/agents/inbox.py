import collections
import datetime
import logging
from typing import Dict, List, Any, Optional

logger = logging.getLogger(__name__)

class AgentInboxManager:
    MAX_MESSAGES_PER_INBOX = 100  # prevent unbounded memory growth

    def __init__(self):
        # Maps agent_name (lowercase) -> list of message dicts
        self._inboxes: Dict[str, List[Dict[str, Any]]] = collections.defaultdict(list)
        # Maps instance_id -> info dict of active agents
        self._active_instances: Dict[str, Dict[str, Any]] = {}

    def register_instance(self, instance_id: str, agent_name: str, ticker: str):
        """Register a running agent instance."""
        self._active_instances[instance_id] = {
            "agent_name": agent_name,
            "ticker": ticker,
            "status": "running",
            "registered_at": datetime.datetime.now(datetime.timezone.utc).isoformat()
        }
        logger.info(
            "[INBOX] Registered active instance: %s for %s (%s)",
            instance_id, agent_name, ticker
        )

    def unregister_instance(self, instance_id: str):
        """Unregister a running agent instance when it completes."""
        if instance_id in self._active_instances:
            info = self._active_instances[instance_id]
            logger.info(
                "[INBOX] Unregistered active instance: %s for %s (%s)",
                instance_id, info["agent_name"], info["ticker"]
            )
            del self._active_instances[instance_id]

    def add_message(self, agent_name: str, message: str, ticker: Optional[str] = None):
        """Insert a steering message into the agent's queue."""
        agent_key = agent_name.lower().strip()
        msg_payload = {
            "message": message,
            "ticker": ticker.upper().strip() if ticker else None,
            "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "consumed": False
        }
        inbox = self._inboxes[agent_key]
        inbox.append(msg_payload)
        # Evict oldest messages if cap exceeded
        if len(inbox) > self.MAX_MESSAGES_PER_INBOX:
            evicted = len(inbox) - self.MAX_MESSAGES_PER_INBOX
            self._inboxes[agent_key] = inbox[-self.MAX_MESSAGES_PER_INBOX:]
            logger.warning(
                "[INBOX] Evicted %d oldest messages for @%s (cap=%d)",
                evicted, agent_name, self.MAX_MESSAGES_PER_INBOX,
            )
        logger.info(
            "[INBOX] Added steering message for @%s (ticker: %s): %s",
            agent_name, ticker or "all", message
        )

    def get_messages(self, agent_name: str, ticker: Optional[str] = None) -> List[str]:
        """Fetch and mark as consumed all unconsumed messages for an agent."""
        agent_key = agent_name.lower().strip()
        msgs = []
        if agent_key not in self._inboxes:
            return msgs

        for msg in self._inboxes[agent_key]:
            if not msg["consumed"]:
                # If ticker is specified on the message, it must match.
                # If no ticker is specified on the message, it applies to any ticker (broadcast mode/general).
                msg_ticker = msg.get("ticker")
                if not msg_ticker or not ticker or msg_ticker.upper() == ticker.upper():
                    msg["consumed"] = True
                    msgs.append(msg["message"])

        if msgs:
            logger.info(
                "[INBOX] Consumed %d messages for agent '%s' (ticker: %s)",
                len(msgs), agent_name, ticker or "none"
            )
        return msgs

    def clear_all(self):
        """Clear all inboxes and active instances for a clean cycle reset."""
        inbox_count = sum(len(v) for v in self._inboxes.values())
        instance_count = len(self._active_instances)
        self._inboxes.clear()
        self._active_instances.clear()
        logger.info(
            "[INBOX] Cleared all inboxes (%d messages, %d instances)",
            inbox_count, instance_count,
        )

    def get_active_instances(self) -> List[Dict[str, Any]]:
        """List currently active running agent instances (pruning stale ones)."""
        now = datetime.datetime.now(datetime.timezone.utc)
        stale_ids = []
        for inst_id, info in list(self._active_instances.items()):
            reg_at_str = info.get("registered_at")
            if not reg_at_str:
                stale_ids.append(inst_id)
                continue
            try:
                reg_time = datetime.datetime.fromisoformat(reg_at_str)
                if (now - reg_time).total_seconds() > 300:
                    stale_ids.append(inst_id)
            except Exception:
                stale_ids.append(inst_id)

        for inst_id in stale_ids:
            try:
                del self._active_instances[inst_id]
            except KeyError:
                pass
        return list(self._active_instances.values())

# Global singleton
inbox_manager = AgentInboxManager()
