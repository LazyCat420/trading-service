"""
Tests for V3 Guardrails — Budget, Loop Detection, Compression, Circuit Breaker.

All pure Python tests — no DB, LLM, or network calls needed.
"""

import pytest

from app.v3.guardrails import (
    V3AgentBudget,
    ToolLoopDetector,
    CircuitBreaker,
    compress_artifact_for_downstream,
    get_budget_for_role,
    enter_v3_session,
    exit_v3_session,
    _active_v3_sessions,
)
from app.v3.shared_desk import PhaseOutcome


# ═══════════════════════════════════════════════════════════════════════════
# V3AgentBudget Tests
# ═══════════════════════════════════════════════════════════════════════════


class TestV3AgentBudget:
    """Tests for V3AgentBudget enforcement."""

    def test_initial_state(self):
        budget = V3AgentBudget(max_turns=7, max_tool_calls=10)
        assert budget.remaining_turns == 7
        assert budget.remaining_tool_calls == 10
        assert not budget.is_exhausted()

    def test_consume_turns(self):
        budget = V3AgentBudget(max_turns=3, max_tool_calls=10)
        assert budget.consume_turn()  # Turn 1
        assert budget.consume_turn()  # Turn 2
        assert budget.consume_turn()  # Turn 3
        assert not budget.consume_turn()  # Over budget
        assert budget.is_exhausted()

    def test_consume_tool_calls(self):
        budget = V3AgentBudget(max_turns=99, max_tool_calls=2)
        assert budget.consume_tool_call()  # Call 1
        assert budget.consume_tool_call()  # Call 2
        assert not budget.consume_tool_call()  # Over budget
        assert budget.is_exhausted()

    def test_is_last_turn(self):
        budget = V3AgentBudget(max_turns=3, max_tool_calls=10)
        budget.consume_turn()
        assert not budget.is_last_turn()
        budget.consume_turn()
        assert budget.is_last_turn()  # Turn 2 of 3 = last

    def test_remaining_counts(self):
        budget = V3AgentBudget(max_turns=5, max_tool_calls=8)
        budget.consume_turn()
        budget.consume_turn()
        budget.consume_tool_call()
        assert budget.remaining_turns == 3
        assert budget.remaining_tool_calls == 7

    def test_token_tracking(self):
        budget = V3AgentBudget()
        budget.consume_tokens(500)
        budget.consume_tokens(300)
        assert budget.current_tokens == 800


class TestBudgetForRole:
    """Tests for role-specific budget configuration."""

    def test_bull_agent_no_tools(self):
        budget = get_budget_for_role("bull_agent")
        assert budget.max_tool_calls == 0
        assert budget.max_turns == 3

    def test_fundamental_analyst_tools(self):
        budget = get_budget_for_role("fundamental_analyst")
        assert budget.max_tool_calls == 12
        assert budget.max_turns == 7

    def test_unknown_role_defaults(self):
        budget = get_budget_for_role("unknown_agent")
        assert budget.max_turns == 7
        assert budget.max_tool_calls == 10


# ═══════════════════════════════════════════════════════════════════════════
# ToolLoopDetector Tests
# ═══════════════════════════════════════════════════════════════════════════


class TestToolLoopDetector:
    """Tests for ToolLoopDetector loop breaking."""

    def test_no_loop_on_success(self):
        detector = ToolLoopDetector(max_identical_failures=3)
        for _ in range(10):
            result = detector.record_call("get_market_data", {"ticker": "AAPL"}, failed=False)
            assert result is None  # Success calls never trigger stop

    def test_loop_detected_on_repeated_failures(self):
        detector = ToolLoopDetector(max_identical_failures=3)
        args = {"ticker": "AAPL"}

        result = detector.record_call("get_market_data", args, failed=True)
        assert result is None  # First failure

        result = detector.record_call("get_market_data", args, failed=True)
        assert result is None  # Second failure

        result = detector.record_call("get_market_data", args, failed=True)
        assert result is not None  # Third failure — loop detected
        assert "SYSTEM OVERRIDE" in result
        assert "get_market_data" in result

    def test_different_args_dont_trigger(self):
        """Different arguments should not count as the same call."""
        detector = ToolLoopDetector(max_identical_failures=2)

        detector.record_call("get_market_data", {"ticker": "AAPL"}, failed=True)
        result = detector.record_call("get_market_data", {"ticker": "MSFT"}, failed=True)
        assert result is None  # Different args

    def test_different_tools_dont_trigger(self):
        """Different tools with same args should not count."""
        detector = ToolLoopDetector(max_identical_failures=2)

        detector.record_call("get_market_data", {"ticker": "AAPL"}, failed=True)
        result = detector.record_call("get_technical_indicators", {"ticker": "AAPL"}, failed=True)
        assert result is None

    def test_total_calls_tracking(self):
        detector = ToolLoopDetector()
        detector.record_call("tool_a", {}, failed=False)
        detector.record_call("tool_b", {}, failed=True)
        assert detector.total_calls == 2

    def test_unique_failures_tracking(self):
        detector = ToolLoopDetector()
        detector.record_call("tool_a", {}, failed=True)
        detector.record_call("tool_a", {}, failed=True)
        detector.record_call("tool_b", {}, failed=False)
        assert detector.unique_failures >= 1


# ═══════════════════════════════════════════════════════════════════════════
# Context Compressor Tests
# ═══════════════════════════════════════════════════════════════════════════


class TestContextCompressor:
    """Tests for compress_artifact_for_downstream."""

    def test_extracts_summary(self):
        artifact = {"summary": "Apple is growing revenue at 15%.", "raw_data": "..." * 1000}
        result = compress_artifact_for_downstream(artifact)
        assert "Apple is growing" in result
        assert "raw_data" not in result

    def test_fallback_to_reasoning(self):
        artifact = {"reasoning": "The stock is overvalued."}
        result = compress_artifact_for_downstream(artifact)
        assert "overvalued" in result

    def test_empty_artifact(self):
        result = compress_artifact_for_downstream({})
        # Empty dict has no summary/reasoning keys, falls back to JSON dump
        assert isinstance(result, str)
        assert len(result) > 0

    def test_none_artifact(self):
        result = compress_artifact_for_downstream(None)
        assert result == "[No artifact produced]"

    def test_truncation(self):
        artifact = {"summary": "A" * 5000}
        result = compress_artifact_for_downstream(artifact)
        assert len(result) <= 2100  # _MAX_SUMMARY_CHARS + margin


# ═══════════════════════════════════════════════════════════════════════════
# Circuit Breaker Tests
# ═══════════════════════════════════════════════════════════════════════════


class TestCircuitBreaker:
    """Tests for CircuitBreaker retry and abort logic."""

    def test_success_not_retried(self):
        breaker = CircuitBreaker(max_retries_per_phase=1)
        assert not breaker.should_retry("research", PhaseOutcome.SUCCESS)

    def test_data_gap_not_retried(self):
        breaker = CircuitBreaker(max_retries_per_phase=1)
        assert not breaker.should_retry("research", PhaseOutcome.DATA_GAP)

    def test_tool_outage_retried_once(self):
        breaker = CircuitBreaker(max_retries_per_phase=1)
        assert breaker.should_retry("research", PhaseOutcome.TOOL_OUTAGE)
        assert not breaker.should_retry("research", PhaseOutcome.TOOL_OUTAGE)

    def test_agent_error_retried_once(self):
        breaker = CircuitBreaker(max_retries_per_phase=1)
        assert breaker.should_retry("debate", PhaseOutcome.AGENT_ERROR)
        assert not breaker.should_retry("debate", PhaseOutcome.AGENT_ERROR)

    def test_success_not_aborted(self):
        breaker = CircuitBreaker()
        assert not breaker.should_abort("research", PhaseOutcome.SUCCESS)

    def test_data_gap_not_aborted(self):
        breaker = CircuitBreaker()
        assert not breaker.should_abort("research", PhaseOutcome.DATA_GAP)

    def test_timed_out_not_retried(self):
        breaker = CircuitBreaker()
        assert not breaker.should_retry("research", PhaseOutcome.TIMED_OUT)

    def test_abort_reason_message(self):
        breaker = CircuitBreaker()
        breaker.record_outcome("research", PhaseOutcome.TOOL_OUTAGE)
        reason = breaker.get_abort_reason("research")
        assert "research" in reason
        assert "TOOL_OUTAGE" in reason


# ═══════════════════════════════════════════════════════════════════════════
# Recursive Agent Prevention Tests
# ═══════════════════════════════════════════════════════════════════════════


class TestRecursiveAgentPrevention:
    """Tests for enter/exit V3 session guards."""

    def setup_method(self):
        """Clear the global session set before each test."""
        _active_v3_sessions.clear()

    def test_enter_and_exit(self):
        enter_v3_session("cycle1:AAPL:analyst")
        assert "cycle1:AAPL:analyst" in _active_v3_sessions
        exit_v3_session("cycle1:AAPL:analyst")
        assert "cycle1:AAPL:analyst" not in _active_v3_sessions

    def test_recursive_spawn_blocked(self):
        enter_v3_session("cycle1:AAPL:analyst")
        with pytest.raises(RuntimeError, match="Recursive agent spawn"):
            enter_v3_session("cycle1:AAPL:analyst")
        exit_v3_session("cycle1:AAPL:analyst")

    def test_different_sessions_allowed(self):
        enter_v3_session("cycle1:AAPL:analyst")
        enter_v3_session("cycle1:MSFT:analyst")
        assert len(_active_v3_sessions) == 2
        exit_v3_session("cycle1:AAPL:analyst")
        exit_v3_session("cycle1:MSFT:analyst")

    def test_exit_nonexistent_safe(self):
        """Exiting a session that doesn't exist should not raise."""
        exit_v3_session("nonexistent_session")
