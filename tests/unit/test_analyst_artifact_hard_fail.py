"""
Tests for the analyst-artifact hard fail (v3/agent_runner.py).

The 2026-07-26 audit: the fundamental analyst for JPM (cycle-v3-1785038939)
returned ONLY the body of its nested `near_term_read` object, promoted to the
top level — {direction, matters_this_week, why}. That parses as clean JSON, so
the unparseable-repair pass never fired. All four required fields of the
envelope (summary/pillars/thesis_direction/confidence) were missing, it scored
36/dead_end, and the runner appended it and returned SUCCESS anyway because the
missing-required hard fail was scoped to decision artifacts only.

Downstream, nothing noticed: the Board issued a confident HOLD at 65% citing
`data_quality: 95` over a fundamental desk that had contributed nothing.

Two behaviours are pinned here:
  1. A malformed analyst artifact earns the circuit breaker's retry
     (AGENT_ERROR) instead of passing as SUCCESS.
  2. The retry degrades to DATA_GAP rather than AGENT_ERROR — a second
     AGENT_ERROR trips should_abort() and kills the entire ticker, which is
     strictly worse than the bug being fixed.
"""
import json
from unittest.mock import AsyncMock, patch

import pytest

from app.v3.agent_runner import run_v3_agent
from app.v3.shared_desk import SharedDesk, PhaseOutcome

# Verbatim from shared_desk for JPM, cycle-v3-1785038939 (2026-07-26 04:11 UTC).
FLATTENED_NEAR_TERM_READ = json.dumps({
    "why": (
        "Extreme technical overextension (Stochastic K/D 99.4, Bollinger 98th "
        "percentile) and resistance at $353.37 suggest an imminent "
        "mean-reversion pullback."
    ),
    "direction": "BEARISH",
    "matters_this_week": True,
})

VALID_FUNDAMENTAL_REPORT = json.dumps({
    "summary": "Net interest income held up; credit costs normalised.",
    "pillars": {"profitability": "strong", "valuation": "full"},
    "thesis_direction": "NEUTRAL",
    "confidence": 62,
    "near_term_read": {
        "direction": "NEUTRAL",
        "matters_this_week": False,
        "why": "No catalyst inside the trade horizon.",
    },
    "data_gaps": [],
})


class _FundamentalAgent:
    AGENT_NAME = "v3_fundamental_analyst"
    ARTIFACT_TYPE = "fundamental_report"
    TOOL_WHITELIST = ["get_sec_filings", "get_finviz_fundamentals"]
    SYSTEM_PROMPT = "You are the fundamental analyst. Output JSON."


def _desk() -> SharedDesk:
    desk = SharedDesk(ticker="JPM", cycle_id="cycle-test")
    desk.cycle_metadata = {"ticker": "JPM", "agent_locale": "default"}
    return desk


def _result(response: str) -> dict:
    return {
        "response": response,
        "tokens_used": 214575,
        # 8 of a budget of 12 — the agent stopped voluntarily, so this was a
        # formatting slip and NOT budget exhaustion.
        "loops_used": 8,
        "stop_reason": "completed",
    }


@pytest.mark.asyncio
async def test_flattened_fundamental_report_does_not_pass_as_success():
    """The JPM regression: this used to return SUCCESS with warnings attached."""
    async def _run(**kwargs):
        return _result(FLATTENED_NEAR_TERM_READ)

    with patch("app.agents.base_agent.run_agent", new=AsyncMock(side_effect=_run)):
        outcome = await run_v3_agent(
            desk=_desk(), agent_module=_FundamentalAgent,
            cycle_id="cycle-test", bot_id="b1",
        )

    assert outcome == PhaseOutcome.AGENT_ERROR, (
        "an analyst artifact missing every required field must earn the retry, "
        "not append as a clean SUCCESS"
    )


@pytest.mark.asyncio
async def test_retry_degrades_to_data_gap_instead_of_aborting_the_desk():
    """A second AGENT_ERROR would trip should_abort() and kill the ticker."""
    async def _run(**kwargs):
        return _result(FLATTENED_NEAR_TERM_READ)

    with patch("app.agents.base_agent.run_agent", new=AsyncMock(side_effect=_run)):
        outcome = await run_v3_agent(
            desk=_desk(), agent_module=_FundamentalAgent,
            cycle_id="cycle-test", bot_id="b1", is_retry=True,
        )

    assert outcome == PhaseOutcome.DATA_GAP, (
        "the retry must degrade, not abort — losing the whole desk is worse "
        "than a degraded one"
    )


@pytest.mark.asyncio
async def test_degraded_artifact_reaches_the_desk_tagged():
    """Salvaged research still flows, but never disguised as a clean run."""
    async def _run(**kwargs):
        return _result(FLATTENED_NEAR_TERM_READ)

    desk = _desk()
    with patch("app.agents.base_agent.run_agent", new=AsyncMock(side_effect=_run)):
        await run_v3_agent(
            desk=desk, agent_module=_FundamentalAgent,
            cycle_id="cycle-test", bot_id="b1", is_retry=True,
        )

    report = desk.fundamental_report
    assert report, "the degraded artifact should still reach the desk"
    assert report.get("_degraded") is True, "it must be tagged as degraded"
    assert report.get("_validation_warnings"), "warnings must be preserved"
    # The one field the debate actually reads is re-nested, not discarded.
    assert report.get("near_term_read", {}).get("direction") == "BEARISH", (
        "the flattened near_term_read should be recovered, not thrown away"
    )


class _JuniorAgent:
    AGENT_NAME = "v3_junior_analyst"
    ARTIFACT_TYPE = "desk_note"
    TOOL_WHITELIST = ["get_market_data"]
    SYSTEM_PROMPT = "You are the junior analyst. Output JSON."


# desk_note.triage_recommendation is schema-REQUIRED but absent in 530 of 810
# production desk_notes (65%), all of which completed normally. Hard-failing on
# any missing required field turned the majority of junior-analyst runs into
# retries and desk aborts — caught by test_artifact_salvage.py before ship.
DESK_NOTE_NO_TRIAGE = json.dumps({
    "summary": "Loan book stable; deposit costs easing into the quarter.",
    "key_findings": ["NII guidance intact", "Buyback pace unchanged"],
    "data_gaps": [],
    "confidence": 61,
})


@pytest.mark.asyncio
async def test_missing_routing_field_alone_is_not_a_failure():
    """The near-regression: a single missing required field must NOT hard-fail.

    Only TOTAL collapse — no substantive content at all — is a failed run.
    """
    async def _run(**kwargs):
        return _result(DESK_NOTE_NO_TRIAGE)

    desk = _desk()
    with patch("app.agents.base_agent.run_agent", new=AsyncMock(side_effect=_run)):
        outcome = await run_v3_agent(
            desk=desk, agent_module=_JuniorAgent,
            cycle_id="cycle-test", bot_id="b1",
        )

    assert outcome == PhaseOutcome.SUCCESS, (
        "a desk_note with real content but no triage_recommendation is the "
        "healthy 65% case — hard-failing it would abort most desks"
    )
    assert not desk.desk_note.get("_degraded")


@pytest.mark.asyncio
async def test_breaker_retries_once_then_survives_without_aborting_the_desk():
    """End-to-end through the circuit breaker: retry, degrade, keep the ticker.

    The whole point of degrading on the retry. Two AGENT_ERRORs would exhaust
    the retry budget and make should_abort() true, killing the desk.
    """
    from app.v3.guardrails import CircuitBreaker
    from app.v3.orchestrator import _run_agent_with_circuit_breaker

    calls = []

    async def _run(**kwargs):
        calls.append(kwargs)
        return _result(FLATTENED_NEAR_TERM_READ)

    breaker = CircuitBreaker()
    with patch("app.agents.base_agent.run_agent", new=AsyncMock(side_effect=_run)):
        outcome = await _run_agent_with_circuit_breaker(
            desk=_desk(), agent_module=_FundamentalAgent,
            phase_name="fundamental_analyst", breaker=breaker,
            cycle_id="cycle-test", bot_id="b1", emit=lambda *a, **k: None,
        )

    # The breaker's unit is the TOOL-ENABLED attempt. Since 2026-08-04 a
    # fragment also triggers one tool-less repair call per attempt, which is
    # not a retry and must not be counted as one.
    attempts = [c for c in calls if c.get("enable_tools")]
    repairs = [c for c in calls if not c.get("enable_tools")]
    assert len(attempts) == 2, "expected exactly one breaker retry"
    assert len(repairs) == 2, (
        "each attempt should try the cheap tool-less repair before the "
        "fragment is graded — that is what stops a parse failure costing a "
        "full tool-enabled re-run"
    )
    assert outcome == PhaseOutcome.DATA_GAP
    assert not breaker.should_abort("fundamental_analyst", outcome), (
        "a persistently malformed analyst artifact must degrade the desk, "
        "never abort the whole ticker"
    )


@pytest.mark.asyncio
async def test_valid_fundamental_report_still_succeeds():
    """The happy path must be untouched — this is the negative control."""
    async def _run(**kwargs):
        return _result(VALID_FUNDAMENTAL_REPORT)

    desk = _desk()
    with patch("app.agents.base_agent.run_agent", new=AsyncMock(side_effect=_run)):
        outcome = await run_v3_agent(
            desk=desk, agent_module=_FundamentalAgent,
            cycle_id="cycle-test", bot_id="b1",
        )

    assert outcome == PhaseOutcome.SUCCESS
    assert not desk.fundamental_report.get("_degraded"), (
        "a well-formed artifact must never be tagged degraded"
    )
    assert desk.fundamental_report.get("near_term_read", {}).get("direction") == "NEUTRAL", (
        "a correctly nested near_term_read must be left alone"
    )
