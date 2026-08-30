"""A failed regime engine must not let the desk claim PM_DONE.

WHAT THIS FILE USED TO BE
-------------------------
It patched `app.v3.agent_runner.run_v3_agent` and then called
`_run_agent_with_circuit_breaker`. That is the wrong seam: `orchestrator` does
`from ... import run_v3_agent` at import time, so the name it calls is
`app.v3.orchestrator.run_v3_agent` and the patch on the source module never
applied. The test therefore drove the REAL agent runner — every attempt, plus
the circuit breaker's retry, plus ResilientCall's five internal attempts —
against whatever LLM endpoint the box happened to have.

It also contained no assertions. Only `print()`s. So it could not fail; it
could only pass or HANG, and on 2026-08-30 it hung a full `pytest` run
indefinitely with the decision box offline. A test with no assertion and a live
network call is a timeout with a docstring.

WHAT IT PINS NOW
----------------
The behaviour the original was reaching for, asserted: when the regime engine
fails, the phase machine refuses `INIT -> PM_DONE`. That refusal is what turns
a silent half-run into a countable one — 33 desks carried
`cycle_metadata.pipeline_incomplete` through the 08-28..08-30 outage precisely
because it fires.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.v3 import orchestrator
from app.v3.guardrails import CircuitBreaker
from app.v3.orchestrator import _run_agent_with_circuit_breaker
from app.v3.shared_desk import DeskPhase, PhaseOutcome, SharedDesk


class _MockRegimeEngine:
    AGENT_NAME = "v3_regime_engine"
    TOOL_WHITELIST: list[str] = []
    SYSTEM_PROMPT = "Mock prompt"
    ARTIFACT_TYPE = "regime_classification"


async def _drive(outcome: PhaseOutcome) -> tuple[PhaseOutcome, MagicMock]:
    """Run one phase with `run_v3_agent` stubbed AT THE SEAM ORCHESTRATOR CALLS."""
    runner = AsyncMock(return_value=outcome)
    with patch.object(orchestrator, "run_v3_agent", runner):
        got = await _run_agent_with_circuit_breaker(
            desk=SharedDesk("META"),
            agent_module=_MockRegimeEngine,
            phase_name="regime_engine",
            breaker=CircuitBreaker(),
            cycle_id="test_cycle",
            bot_id="bot123",
            emit=lambda *a, **k: None,
        )
    return got, runner


@pytest.mark.asyncio
async def test_a_failed_regime_engine_returns_the_failure_and_retries_once():
    got, runner = await _drive(PhaseOutcome.AGENT_ERROR)
    assert got is PhaseOutcome.AGENT_ERROR
    assert runner.await_count == 2, (
        "the circuit breaker retries a retryable failure exactly once; "
        f"the agent was awaited {runner.await_count} times"
    )
    assert runner.await_args.kwargs.get("is_retry") is True


@pytest.mark.asyncio
async def test_the_stub_is_bound_where_the_orchestrator_reads_it():
    """NEGATIVE CONTROL for the seam.

    If `orchestrator.run_v3_agent` were not the name actually called, the stub
    above would be awaited zero times and the assertions would be measuring the
    real runner — which is the original defect, in the shape that hid it.
    """
    _got, runner = await _drive(PhaseOutcome.SUCCESS)
    assert runner.await_count == 1, "the patched name was never called"
    assert orchestrator.run_v3_agent is not runner, "the patch leaked past its context"


def test_the_desk_refuses_pm_done_after_a_failed_regime_engine():
    """The half the original was actually about: the phase machine says no.

    `INIT -> PM_DONE` skips RESEARCH_DONE and DEBATE_DONE. The orchestrator's
    `except ValueError` catches this refusal and stores a
    `board_degraded_fallback` decision whose reasoning is the transition error —
    ugly on purpose, so the failure is countable rather than invisible.
    """
    desk = SharedDesk("META")
    assert desk.phase is DeskPhase.INIT
    with pytest.raises(ValueError):
        desk.advance_phase(DeskPhase.PM_DONE)
    assert desk.phase is DeskPhase.INIT, "a refused transition must not move the desk"
