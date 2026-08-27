"""A dead desk must report truthfully and page someone.

2026-08-26, cycle-v3-1787786020/KSS (and the 2026-08-05 KSS abort on file):
a circuit-breaker abort produced the reason "Circuit breaker tripped: phase
'quant_analyst' failed 0 time(s) with outcomes []" — the phase had failed
twice, but `_check_abort` built the reason BEFORE `record_outcome` ran, and
the attempt consumed by `should_retry` was never ledgered at all. The abort
also paged nobody: one log line plus a HOLD@0 noop row that reads like a
quiet decision.

These tests drive the real orchestrator seams (`_run_agent_with_circuit_breaker`
→ `_check_abort`), not a re-implementation of them.
"""

from unittest.mock import AsyncMock, patch

import pytest

from app.v3.guardrails import CircuitBreaker
from app.v3.orchestrator import _check_abort, _run_agent_with_circuit_breaker
from app.v3.shared_desk import DeskPhase, PhaseOutcome, SharedDesk


@pytest.fixture
def saved_desks():
    """Capture the desk `_check_abort` persists (same seam as
    test_deferred_decisions.py — an abort that forgets to save fails here)."""
    saved = []
    with patch("app.v3.orchestrator.save_desk", side_effect=saved.append):
        yield saved


@pytest.fixture
def paged():
    """Capture phase-abort pages. Patched at the definition site because
    `_page_phase_abort` imports it lazily inside the function body."""
    with patch("app.services.degraded_alert.alert_phase_abort") as mock_alert:
        mock_alert.return_value = True
        yield mock_alert


async def _fail_phase_twice(desk, breaker, phase_name):
    """Run the REAL retry ladder: run_v3_agent fails twice, retry consumed."""
    with patch(
        "app.v3.orchestrator.run_v3_agent",
        new=AsyncMock(return_value=PhaseOutcome.AGENT_ERROR),
    ) as mock_run:
        outcome = await _run_agent_with_circuit_breaker(
            desk=desk,
            agent_module=object(),
            phase_name=phase_name,
            breaker=breaker,
            cycle_id=desk.cycle_id,
            bot_id="b1",
            emit=lambda *a, **k: None,
        )
    assert mock_run.await_count == 2  # first attempt + the one retry
    assert outcome == PhaseOutcome.AGENT_ERROR
    return outcome


async def test_abort_reason_counts_every_failure(saved_desks, paged):
    """Pre-fix this read 'failed 0 time(s) with outcomes []'."""
    desk = SharedDesk(ticker="KSS", cycle_id="cycle-abort-truth")
    breaker = CircuitBreaker(max_retries_per_phase=1)

    outcome = await _fail_phase_twice(desk, breaker, "junior_analyst")
    result = _check_abort(desk, breaker, "junior_analyst", outcome)

    assert result is not None
    reason = result["v3_metadata"]["abort_reason"]
    assert "failed 2 time(s)" in reason
    assert reason.count("AGENT_ERROR") == 2
    assert "Retries: 1/1" in reason
    # The dead-desk markers the disposition layer keys on must survive
    # any rewording (app/v3/disposition.py _ABORT_MARKERS).
    assert "Circuit breaker tripped" in reason
    assert "V3 Pipeline aborted" in result["rationale"]
    assert desk.phase == DeskPhase.ABORTED


async def test_circuit_breaker_abort_pages(saved_desks, paged):
    """Pre-fix an aborted desk was invisible: log line + noop row, no alert."""
    desk = SharedDesk(ticker="KSS", cycle_id="cycle-abort-page")
    breaker = CircuitBreaker(max_retries_per_phase=1)

    outcome = await _fail_phase_twice(desk, breaker, "junior_analyst")
    result = _check_abort(desk, breaker, "junior_analyst", outcome)

    assert result is not None
    paged.assert_called_once()
    kwargs = paged.call_args.kwargs
    assert kwargs["ticker"] == "KSS"
    assert kwargs["phase"] == "junior_analyst"
    assert kwargs["cycle_id"] == "cycle-abort-page"
    assert "Circuit breaker tripped" in kwargs["reason"]


async def test_timeout_abort_pages_too(saved_desks, paged):
    desk = SharedDesk(ticker="KSS", cycle_id="cycle-abort-timeout")
    breaker = CircuitBreaker(max_retries_per_phase=1)

    result = _check_abort(desk, breaker, "board_of_directors", PhaseOutcome.TIMED_OUT)

    assert result is not None
    paged.assert_called_once()
    assert paged.call_args.kwargs["phase"] == "board_of_directors"


async def test_survive_path_neither_aborts_nor_pages(saved_desks, paged):
    """First failure with a retry still in budget: record, continue, no page."""
    desk = SharedDesk(ticker="KSS", cycle_id="cycle-survive")
    breaker = CircuitBreaker(max_retries_per_phase=1)

    result = _check_abort(desk, breaker, "junior_analyst", PhaseOutcome.AGENT_ERROR)

    assert result is None
    paged.assert_not_called()
    assert desk.phase != DeskPhase.ABORTED
    # ...but the failure IS in the ledger now.
    assert "failed 1 time(s)" in breaker.get_abort_reason("junior_analyst")


def test_paging_failure_never_breaks_the_abort(saved_desks):
    """Alerting must never hurt the abort path."""
    desk = SharedDesk(ticker="KSS", cycle_id="cycle-abort-alertfail")
    breaker = CircuitBreaker(max_retries_per_phase=1)
    breaker.should_retry("junior_analyst", PhaseOutcome.AGENT_ERROR)  # consume

    with patch(
        "app.services.degraded_alert.alert_phase_abort",
        side_effect=RuntimeError("mongo down"),
    ):
        result = _check_abort(desk, breaker, "junior_analyst", PhaseOutcome.AGENT_ERROR)

    assert result is not None
    assert desk.phase == DeskPhase.ABORTED
