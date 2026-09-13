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
from app.db import mongo_store

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


# ── ONE source of truth for turns ────────────────────────────────────────────
#
# ⚠ This block used to be `AGENT_ROLE_BUDGETS`, a second copy of the turn
# budgets keyed WITHOUT the `v3_` prefix ("bull_defense", "decision_synthesizer")
# under a comment claiming it was "harmonized with tool_whitelists.py
# AGENT_BUDGET_OVERRIDES". It was not, and it could not be: `get_budget_for_role`
# stripped `custom_v3_` and `custom_` but never a bare `v3_`, so every real agent
# name missed and fell through to the default. Measured 2026-09-13:
#
#     mismatches: 14 of 14
#
# Not one entry was ever read. A hand-maintained copy of another module's table
# does not drift slowly — this one was born dead and stayed dead, under a
# comment asserting the opposite. The turn budget now comes from the single
# table that the v3 runner actually uses, so the two cannot disagree again.
# See [[an-allowlist-can-drift-both-ways-and-keep-its-count]].
#
# Tool-call caps have no other home, so they stay here — keyed by the SAME
# canonical name, with `test_v3_budget_tables_agree` asserting set equality in
# both directions so a new agent cannot be added to one and missed in the other.
_DEFAULT_ROLE_BUDGET = {"max_turns": 7, "max_tool_calls": 10}

AGENT_MAX_TOOL_CALLS: dict[str, int] = {
    "v3_junior_analyst": 15,
    "v3_fundamental_analyst": 20,
    "v3_quant_analyst": 20,
    "v3_valuation_analyst": 10,
    "v3_bull_agent": 10,
    "v3_bear_agent": 10,
    "v3_bull_defense": 10,
    "v3_debate_judge": 8,        # raised with its turn budget 4->7
    "v3_delta_analyst": 10,
    "v3_regime_engine": 8,
    "v3_board_of_directors": 8,  # raised with its turn budget 5->7
    "v3_portfolio_manager": 10,
    "v3_decision_synthesizer": 14,  # raised with its turn budget 5->12;
                                    # at 5 the TOOL cap would bind first
                                    # and the turn raise would do nothing
    "user_chat": 20,
}


def canonical_agent_name(role: str) -> str:
    """Strip the wrapper prefixes an agent name can arrive with.

    `custom_v3_bull_agent` and `v3_bull_agent` are the same agent. The old
    version stripped `custom_v3_` down to the bare role, which is what made
    every lookup miss.
    """
    from app.agents.tool_whitelists import AGENT_BUDGET_OVERRIDES

    cleaned = (role or "").lower().strip()
    if cleaned.startswith("custom_v3_"):
        cleaned = "v3_" + cleaned[len("custom_v3_"):]
    elif cleaned.startswith("custom_"):
        cleaned = cleaned[len("custom_"):]
    # A BARE role name ("bull_agent") resolves to the v3 agent of that name.
    # Production has never used one — 30 days of `agent_traces` is 100% v3_-
    # prefixed, 0 bare — but the old table was keyed this way and its tests
    # were written to match it, which is exactly why a dead table read as a
    # live one. Accepting both spellings against ONE table keeps those callers
    # working without reviving a second source of truth.
    if cleaned not in AGENT_BUDGET_OVERRIDES and f"v3_{cleaned}" in AGENT_BUDGET_OVERRIDES:
        cleaned = f"v3_{cleaned}"
    return cleaned


def get_budget_for_role(role: str) -> V3AgentBudget:
    """Create a V3AgentBudget with role-specific limits."""
    from app.agents.tool_whitelists import AGENT_BUDGET_OVERRIDES

    cleaned = canonical_agent_name(role)
    turns = AGENT_BUDGET_OVERRIDES.get(cleaned)
    if turns is None or turns >= 9999:
        # 9999 is the "no override" sentinel in that table; it is not a budget.
        turns = _DEFAULT_ROLE_BUDGET["max_turns"]
    return V3AgentBudget(
        max_turns=int(turns),
        max_tool_calls=AGENT_MAX_TOOL_CALLS.get(
            cleaned, _DEFAULT_ROLE_BUDGET["max_tool_calls"]),
    )



# ═══════════════════════════════════════════════════════════════════════════
# 2. (Moved to lazycat-sdk/lazycat/agent.py: ToolLoopDetector)
# ═══════════════════════════════════════════════════════════════════════════

# ═══════════════════════════════════════════════════════════════════════════
# 3. Context Compressor — Prevents context snowball between agents
# ═══════════════════════════════════════════════════════════════════════════

_MAX_SUMMARY_CHARS = 2000


# Phrases an agent uses when it is reporting a BROKEN TOOL rather than a
# genuine absence of information. Matched against its own data_gaps strings.
_TOOL_FAILURE_PHRASES = (
    "search timeout", "web search timeout", "search failed", "search unavailable",
    "tool failed", "tool error", "timed out", "timeout", "unavailable",
    "could not retrieve", "unable to retrieve", "no response from",
    "connection", "rate limit",
)


def research_degraded(cycle_id: str, ticker: str, artifact: dict | None) -> str | None:
    """Why this ticker's research is untrustworthy, or None if it is sound.

    The Junior Analyst's triage decides whether the Fundamental Analyst runs at
    all. In cycle-v3-1785137616 that gate fired on a lie: DuckDuckGo was
    refusing our egress IP, lazy_web_search failed 8/8, and the analyst wrote
    "no qualitative catalysts found" — which the orchestrator read as a fact
    about the company rather than a fact about the tooling. STT and NDAQ each
    got a 526-byte stub with all five pillars "Not analyzed", and NDAQ went on
    to produce the cycle's only BUY.

    "I could not look" and "I looked and found nothing" are different claims,
    and only the second one justifies skipping work.

    Two independent signals, because neither alone is sufficient:
      * agent_tool_telemetry — hard evidence, independent of what the model
        chose to admit. Authoritative but only covers instrumented calls.
      * the artifact's own data_gaps / _failure_patterns — catches degradation
        the telemetry missed, at the cost of trusting self-reporting.
    """
    artifact = artifact or {}

    for gap in artifact.get("data_gaps") or []:
        low = str(gap).lower()
        if any(p in low for p in _TOOL_FAILURE_PHRASES):
            return f"analyst reported a tool failure: {str(gap)[:160]}"

    if "FALLBACK_OUTPUT" in (artifact.get("_failure_patterns") or []):
        return "analyst artifact was a fallback output"

    # Fail OPEN on a probe error: an unreachable DB would otherwise force the
    # full panel on every ticker forever.
    try:
        pipeline = [
            {"$match": {"cycle_id": cycle_id, "ticker": ticker, "success": False}},
            {"$group": {"_id": "$tool_name", "count": {"$sum": 1}}},
            {"$sort": {"count": -1}},
            {"$limit": 1},
        ]
        docs = mongo_store.aggregate("agent_tool_telemetry", pipeline)
        if docs and docs[0].get("count"):
            return f"{docs[0]['count']} failed {docs[0]['_id']} call(s) this cycle"
    except Exception as e:  # pragma: no cover - defensive
        logger.debug("[V3][triage] degradation probe failed for %s (fail-open): %s",
                     ticker, e)

    return None


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
