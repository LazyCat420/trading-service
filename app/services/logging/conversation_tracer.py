"""
Conversation Tracer — Inter-agent communication transcript logger.

Captures every agent-to-agent message, delegation, finding post,
and investigation — creating a transcript that reads like a real
trading floor conversation log.

Usage:
    from app.services.logging.conversation_tracer import conversation_tracer
    conversation_tracer.log_turn(cycle_id, ticker, turn_data)
    transcript = conversation_tracer.get_transcript(cycle_id, ticker)
"""

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class ConversationTurn:
    """A single turn in an agent conversation."""

    turn_number: int
    speaker: str
    listener: str  # target agent or "all"
    message_type: str  # "finding", "delegation", "challenge", "response", "stance"
    content: str
    tools_used: list[str] = field(default_factory=list)
    stance: str = ""  # "BULLISH", "BEARISH", "NEUTRAL"
    confidence: int = 0
    conviction: str = ""  # "WATCH", "LOW", "MODERATE", "HIGH", "EXTREME"
    token_cost: int = 0
    timestamp: float = field(default_factory=time.monotonic)


class ConversationTracer:
    """Central transcript logger for inter-agent conversations.

    Thread-safe via simple dict append pattern. Each transcript is
    scoped to a (cycle_id, ticker) pair.
    """

    def __init__(self):
        self._transcripts: dict[str, list[ConversationTurn]] = {}

    def _key(self, cycle_id: str, ticker: str) -> str:
        return f"{cycle_id}:{ticker}"

    def log_turn(
        self,
        cycle_id: str,
        ticker: str,
        speaker: str,
        listener: str,
        message_type: str,
        content: str,
        tools_used: list[str] | None = None,
        stance: str = "",
        confidence: int = 0,
        conviction: str = "",
        token_cost: int = 0,
    ) -> int:
        """Log a conversation turn. Returns the turn number."""
        key = self._key(cycle_id, ticker)
        if key not in self._transcripts:
            self._transcripts[key] = []

        turn_number = len(self._transcripts[key]) + 1
        turn = ConversationTurn(
            turn_number=turn_number,
            speaker=speaker,
            listener=listener,
            message_type=message_type,
            content=content[:2000],  # Cap content to prevent memory bloat
            tools_used=tools_used or [],
            stance=stance,
            confidence=confidence,
            conviction=conviction,
            token_cost=token_cost,
        )
        self._transcripts[key].append(turn)

        logger.info(
            "[ConvTrace] Turn %d: %s → %s (%s): %s",
            turn_number, speaker, listener, message_type,
            content[:100],
        )
        return turn_number

    def get_transcript(
        self,
        cycle_id: str,
        ticker: str,
    ) -> list[dict]:
        """Get the conversation transcript as a list of dicts."""
        key = self._key(cycle_id, ticker)
        turns = self._transcripts.get(key, [])
        return [
            {
                "turn": t.turn_number,
                "speaker": t.speaker,
                "listener": t.listener,
                "type": t.message_type,
                "content": t.content,
                "tools": t.tools_used,
                "stance": t.stance,
                "confidence": t.confidence,
                "conviction": t.conviction,
                "tokens": t.token_cost,
                "time": datetime.fromtimestamp(
                    t.timestamp, tz=timezone.utc
                ).isoformat() if t.timestamp > 1e9 else f"+{t.timestamp:.1f}s",
            }
            for t in turns
        ]

    def get_readable_transcript(
        self,
        cycle_id: str,
        ticker: str,
    ) -> str:
        """Get a human-readable conversation transcript."""
        key = self._key(cycle_id, ticker)
        turns = self._transcripts.get(key, [])
        if not turns:
            return f"No conversation recorded for {ticker} in cycle {cycle_id}."

        lines = [f"=== Conversation Transcript: {ticker} (Cycle: {cycle_id}) ===\n"]
        for t in turns:
            stance_str = f" [{t.stance} {t.confidence}%]" if t.stance else ""
            tools_str = f" (tools: {', '.join(t.tools_used)})" if t.tools_used else ""
            lines.append(
                f"[Turn {t.turn_number}] {t.speaker} → {t.listener} "
                f"({t.message_type}){stance_str}{tools_str}:\n"
                f"  {t.content}\n"
            )
        return "\n".join(lines)

    def get_stats(
        self,
        cycle_id: str,
        ticker: str,
    ) -> dict:
        """Get conversation statistics."""
        key = self._key(cycle_id, ticker)
        turns = self._transcripts.get(key, [])
        if not turns:
            return {"total_turns": 0, "speakers": [], "total_tokens": 0}

        speakers = list({t.speaker for t in turns})
        return {
            "total_turns": len(turns),
            "speakers": speakers,
            "total_tokens": sum(t.token_cost for t in turns),
            "delegations": sum(1 for t in turns if t.message_type == "delegation"),
            "challenges": sum(1 for t in turns if t.message_type == "challenge"),
            "findings": sum(1 for t in turns if t.message_type == "finding"),
            "stances": {
                t.speaker: {"stance": t.stance, "confidence": t.confidence}
                for t in turns if t.stance
            },
        }

    def clear(self, cycle_id: str, ticker: str) -> None:
        """Clear the transcript for a completed conversation."""
        key = self._key(cycle_id, ticker)
        self._transcripts.pop(key, None)

    def clear_cycle(self, cycle_id: str) -> int:
        """Clear all transcripts for a completed cycle. Returns count cleared."""
        prefix = f"{cycle_id}:"
        keys_to_remove = [k for k in self._transcripts if k.startswith(prefix)]
        for k in keys_to_remove:
            del self._transcripts[k]
        return len(keys_to_remove)


# Global singleton
conversation_tracer = ConversationTracer()
