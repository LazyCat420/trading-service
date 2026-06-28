"""
V3 Guardrails — Budget enforcement, loop detection, compression, circuit breaker.

These are the orchestrator-level harness rules that wrap Prism agent invocations.
They prevent V1/V2 failure modes: infinite loops, context snowball, empty-data HOLDs.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, field
from typing import Any

from app.v3.shared_desk import PhaseOutcome

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════════════
# 1. V3 Agent Budget — Real limits (unlike V2's max_turns=9999)
# ═══════════════════════════════════════════════════════════════════════════

@dataclass
class V3AgentBudget:
    """Strict budget for V3 agent execution.

    Unlike V2's AgentBudget (which defaults to 9999 turns / infinite),
    V3 enforces real limits to prevent runaway agents.
    """
    max_turns: int = 7
    max_tool_calls: int = 10
    current_turns: int = 0
    current_tool_calls: int = 0
    current_tokens: int = 0
    force_final_on_last_turn: bool = True

    def consume_turn(self) -> bool:
        """Consume a turn. Returns False if budget exhausted."""
        self.current_turns += 1
        return self.current_turns <= self.max_turns

    def consume_tool_call(self) -> bool:
        """Consume a tool call. Returns False if budget exhausted."""
        self.current_tool_calls += 1
        return self.current_tool_calls <= self.max_tool_calls

    def consume_tokens(self, tokens: int) -> None:
        """Track token usage (informational — not a hard limit)."""
        self.current_tokens += tokens

    def is_exhausted(self) -> bool:
        """Check if any budget dimension is exhausted."""
        return (
            self.current_turns >= self.max_turns
            or self.current_tool_calls >= self.max_tool_calls
        )

    def is_last_turn(self) -> bool:
        """Check if this is the last available turn."""
        return self.current_turns >= self.max_turns - 1

    @property
    def remaining_turns(self) -> int:
        return max(0, self.max_turns - self.current_turns)

    @property
    def remaining_tool_calls(self) -> int:
        return max(0, self.max_tool_calls - self.current_tool_calls)


# Default budgets per agent role
AGENT_ROLE_BUDGETS: dict[str, dict[str, int]] = {
    "junior_analyst": {"max_turns": 10, "max_tool_calls": 15},
    "fundamental_analyst": {"max_turns": 12, "max_tool_calls": 20},
    "quant_analyst": {"max_turns": 12, "max_tool_calls": 20},
    "bull_agent": {"max_turns": 3, "max_tool_calls": 0},  # No tools — pure reasoning
    "bear_agent": {"max_turns": 3, "max_tool_calls": 0},  # No tools — pure reasoning
    "regime_engine": {"max_turns": 5, "max_tool_calls": 8},
    "board_of_directors": {"max_turns": 5, "max_tool_calls": 3},  # Phase 2: tools enabled for contextual decisions
}


def get_budget_for_role(role: str) -> V3AgentBudget:
    """Create a V3AgentBudget with role-specific limits."""
    cleaned = role.lower().strip()
    if cleaned.startswith("custom_v3_"):
        cleaned = cleaned[10:]
    elif cleaned.startswith("custom_"):
        cleaned = cleaned[7:]
    
    config = AGENT_ROLE_BUDGETS.get(cleaned, {"max_turns": 7, "max_tool_calls": 10})
    return V3AgentBudget(
        max_turns=config["max_turns"],
        max_tool_calls=config["max_tool_calls"],
    )



# ═══════════════════════════════════════════════════════════════════════════
# 2. (Moved to lazycat-sdk/lazycat/agent.py: ToolLoopDetector)
# ═══════════════════════════════════════════════════════════════════════════

# ═══════════════════════════════════════════════════════════════════════════
# 3. Context Compressor — Prevents context snowball between agents
# ═══════════════════════════════════════════════════════════════════════════

_MAX_SUMMARY_CHARS = 2000


def compress_artifact_for_downstream(artifact: dict) -> str:
    """Compress an artifact to just its summary for downstream agents.

    After each agent finishes, the orchestrator keeps ONLY the final report
    summary and a short machine-readable extract. It completely drops the
    raw JSON from tools and intermediate scratch messages.

    Args:
        artifact: The raw artifact dict from an agent.

    Returns:
        A clean, small narrative string (≤ _MAX_SUMMARY_CHARS).
    """
    if not artifact:
        return "[No artifact produced]"

    summary = artifact.get("summary", "")
    if not summary:
        # Try to extract something useful
        for key in ("reasoning", "rationale", "analysis", "content"):
            if key in artifact and artifact[key]:
                summary = str(artifact[key])
                break

    if not summary:
        summary = json.dumps(
            {k: v for k, v in artifact.items() if not k.startswith("_")},
            default=str,
        )

    # Truncate
    if len(summary) > _MAX_SUMMARY_CHARS:
        summary = summary[: _MAX_SUMMARY_CHARS - 50] + "\n[... truncated ...]"

    return summary


# ═══════════════════════════════════════════════════════════════════════════
# 4. Circuit Breaker — Prevents infinite retries on persistent failures
# ═══════════════════════════════════════════════════════════════════════════

class CircuitBreaker:
    """Per-phase retry logic with strict limits.

    Each phase may be invoked at most once, with at most one explicit retry
    on TOOL_OUTAGE or AGENT_ERROR before the circuit breaker aborts.

    If a cycle aborts, the effective action is NO_OP with a logged reason.
    """

    def __init__(self, max_retries_per_phase: int = 1):
        self.max_retries = max_retries_per_phase
        self._retry_counts: dict[str, int] = {}
        self._outcomes: dict[str, list[PhaseOutcome]] = {}

    def record_outcome(
        self, phase_name: str, outcome: PhaseOutcome
    ) -> None:
        """Record the outcome of a phase execution."""
        if phase_name not in self._outcomes:
            self._outcomes[phase_name] = []
        self._outcomes[phase_name].append(outcome)

    def should_retry(self, phase_name: str, outcome: PhaseOutcome) -> bool:
        """Check if a phase should be retried.

        Only retries on TOOL_OUTAGE or AGENT_ERROR, and only up to
        max_retries times.
        """
        retryable = {PhaseOutcome.TOOL_OUTAGE, PhaseOutcome.AGENT_ERROR}

        if outcome not in retryable:
            return False

        count = self._retry_counts.get(phase_name, 0)
        if count >= self.max_retries:
            logger.warning(
                "[CircuitBreaker] Phase '%s' hit retry limit (%d). Aborting.",
                phase_name,
                self.max_retries,
            )
            return False

        self._retry_counts[phase_name] = count + 1
        logger.info(
            "[CircuitBreaker] Phase '%s' retry %d/%d on %s",
            phase_name,
            count + 1,
            self.max_retries,
            outcome.value,
        )
        return True

    def should_abort(self, phase_name: str, outcome: PhaseOutcome) -> bool:
        """Check if the entire cycle should be aborted.

        Aborts if the outcome is not SUCCESS or DATA_GAP and retries
        are exhausted.

        NOTE: This is a READ-ONLY check. It does NOT increment retry counts.
        The orchestrator's _run_agent_with_circuit_breaker() handles retries
        via should_retry(). This method only checks whether the budget is
        exhausted after those retries have been consumed.
        """
        non_fatal = {PhaseOutcome.SUCCESS, PhaseOutcome.DATA_GAP}
        if outcome in non_fatal:
            return False

        # Check if retries are exhausted (read-only — no side effects)
        count = self._retry_counts.get(phase_name, 0)
        if count < self.max_retries:
            # Still have retries left — don't abort yet
            return False

        return True

    def get_abort_reason(self, phase_name: str) -> str:
        """Generate a human-readable abort reason."""
        outcomes = self._outcomes.get(phase_name, [])
        retries = self._retry_counts.get(phase_name, 0)
        return (
            f"Circuit breaker tripped: phase '{phase_name}' failed "
            f"{len(outcomes)} time(s) with outcomes "
            f"{[o.value for o in outcomes]}. "
            f"Retries: {retries}/{self.max_retries}."
        )


# ═══════════════════════════════════════════════════════════════════════════
# 5. Recursive Agent Prevention
# ═══════════════════════════════════════════════════════════════════════════

_active_v3_sessions: set[str] = set()


def enter_v3_session(session_key: str) -> None:
    """Register a V3 agent session. Prevents recursive spawning.

    Raises:
        RuntimeError: If a session with this key is already active,
            indicating a recursive spawn attempt.
    """
    if session_key in _active_v3_sessions:
        raise RuntimeError(
            f"[V3 Guard] Recursive agent spawn detected: '{session_key}' "
            f"is already running. Only the Orchestrator may spawn agents."
        )
    _active_v3_sessions.add(session_key)


def exit_v3_session(session_key: str) -> None:
    """Unregister a V3 agent session."""
    _active_v3_sessions.discard(session_key)
