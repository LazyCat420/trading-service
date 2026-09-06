"""A retry started with 447 seconds left could not finish, and ran until the wall clock killed it.

MEASURED 2026-09-06, cycle-v3-1788660665, ABT v3_fundamental_analyst, from prism's
`requests` ledger:

    02:33:45  run starts (1800 s wall clock — ANALYSIS_WORKER_TIMEOUT_SECONDS)
    02:49:48  iteration 10 pending; 02:56:09 watchdog: "Provider stream stalled: no data received for 300s"
    02:56:09  [RESILIENCE] attempt 1/5 failed: PrismTransientHarnessError [transient] (1343762ms)
    02:56:15  attempt 2 starts — a NEW conversation, iteration 1, prompt rebuilt from scratch
    03:03:45  asyncio.wait_for(1800) kills attempt 2 at its iteration 4
    03:04:25  …and the server finishes iteration 4 for a client that is gone

Attempt 1 had taken 22 minutes to reach iteration 10. Attempt 2 was given the
447 seconds the wall clock had left. It could never finish; it cost eight more
minutes of a shared box and the desk was aborted anyway.

The retry wrapper cannot see the deadline, so the attempt itself now checks it:
past attempt 1, if the time left is under RETRY_MIN_BUDGET_S the attempt refuses
with RetryBudgetExhausted — registered NON-retryable, so aresilient_call stops
at once — and the runner records the run as AGENT_ERROR with its cost and the
failure reason RETRY_BUDGET_EXHAUSTED. A run with no deadline behaves as before.
"""
from __future__ import annotations

import time
from unittest.mock import AsyncMock, patch

import pytest

from app.agents.base_agent import (
    RETRY_MIN_BUDGET_S,
    PrismTransientHarnessError,
    RetryBudgetExhausted,
    run_agent,
)
from app.v3 import agent_runner
from app.v3.output_rules import FAILURE_REASONS, RETRY_BUDGET_EXHAUSTED

STALL = "Provider stream stalled: no data received for 300s"  # verbatim
ABT_SECONDS_LEFT = 447  # 1800 - (02:56:09 - 02:33:45)


async def _run(deadline_monotonic):
    harness = AsyncMock(side_effect=PrismTransientHarnessError(STALL))
    with patch("lazycat.agent.AgentHarness.run", harness), \
         patch("app.services.prism_agent_caller.resolve_default_model_for_agent",
               new=AsyncMock(return_value=(None, None))), \
         patch("app.agents.tool_whitelists.get_agent_tools", return_value=[{"name": "get_market_data"}]), \
         patch("lazycat.resilience.asyncio.sleep", new=AsyncMock()):
        try:
            await run_agent(agent_name="v3_fundamental_analyst", ticker="_AUDIT_TEST", cycle_id="cycle-test",
                            bot_id="b", system_prompt="s", user_prompt="u", enable_tools=True,
                            deadline_monotonic=deadline_monotonic)
        except Exception as exc:  # noqa: BLE001
            return harness.await_count, exc
    return harness.await_count, None


@pytest.mark.asyncio
async def test_with_447s_left_the_second_attempt_is_refused():
    n, exc = await _run(time.monotonic() + ABT_SECONDS_LEFT)
    assert n == 1, f"the retry ran anyway ({n} attempts)"
    assert exc is not None
    # aresilient_call wraps the last failure in ResilientCallError and keeps
    # per-attempt AttemptRecord(exception_type=str, ...) rows: the refusal must
    # be the LAST record, by name.
    records = getattr(exc, "attempts", None) or []
    assert records and records[-1].exception_type == "RetryBudgetExhausted", \
        [(r.attempt, r.exception_type, str(r.failure_type)) for r in records]


@pytest.mark.asyncio
async def test_with_ample_budget_all_five_attempts_run():
    n, _ = await _run(time.monotonic() + 100_000)
    assert n == 5


@pytest.mark.asyncio
async def test_no_deadline_means_no_change():
    n, _ = await _run(None)
    assert n == 5


def test_the_floor_is_a_named_constant_not_a_literal():
    assert RETRY_MIN_BUDGET_S >= 300, "a retry needs at least one watchdog window plus a prefill"


def test_the_reason_is_in_the_namespace_and_the_runner_uses_it():
    import inspect
    assert RETRY_BUDGET_EXHAUSTED in FAILURE_REASONS
    src = inspect.getsource(agent_runner)
    assert "RETRY_BUDGET_EXHAUSTED" in src and "RetryBudgetExhausted" in src

def test_the_runner_hands_the_deadline_to_every_run_agent_call():
    import inspect
    src = inspect.getsource(agent_runner)
    assert src.count("deadline_monotonic=") >= 2, "both the main call and the repair call must carry the deadline"
