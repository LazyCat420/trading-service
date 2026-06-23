"""
AgentBudget — Governor for agent execution costs and loops.
Ensures agents don't get stuck in infinite tool-calling loops.
"""

from dataclasses import dataclass
import logging

logger = logging.getLogger(__name__)


@dataclass
class AgentBudget:
    max_turns: int = 9999
    current_turns: int = 0
    max_tokens: int = 9999999
    current_tokens: int = 0
    max_usd: float = 9999.0
    current_usd: float = 0.0

    def consume_turn(self) -> bool:
        """Consume a turn. Returns False if budget exhausted."""
        self.current_turns += 1
        return self.current_turns <= self.max_turns

    def consume_tokens(self, tokens: int, cost_per_1k: float = 0.001) -> bool:
        """Consume tokens and track estimated cost. Returns False if exhausted."""
        self.current_tokens += tokens
        self.current_usd += (tokens / 1000.0) * cost_per_1k
        return self.current_tokens <= self.max_tokens and self.current_usd <= self.max_usd

    def is_exhausted(self) -> bool:
        return (self.current_turns > self.max_turns or
                self.current_tokens > self.max_tokens or
                self.current_usd > self.max_usd)

