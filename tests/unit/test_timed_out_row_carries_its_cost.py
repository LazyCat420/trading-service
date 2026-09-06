"""A run that dies by TIMEOUT or CANCEL spent real money, and its row said zero.

MEASURED 2026-09-06 on cycle-v3-1788660665. The ABT fundamental analyst ran the
full agent timeout and its `v3_agent_telemetry` row reads, verbatim:

    outcome=TIMED_OUT  loops_used=0  prompt_tokens=0  token_usage=0
    cost_partial=False  elapsed_ms=1800068  failure_reason=TIMEOUT

while `agent_tool_telemetry` holds **18 tool calls** attributed to that agent
on that ticker in that cycle (whiteboard_read, get_earnings_data,
screener_query x7, whiteboard_write, ...), the last at 02:45:12 — eighteen
minutes before the timeout fired at 03:03:45.

`6586589` fixed exactly this for the CRASH path: `run_agent` accumulates
`partial_cost` and attaches it to whatever exception escapes, and the runner's
`except Exception` reads it. The timeout path is a sibling seam that fix could
not reach: `asyncio.wait_for` CANCELS the inner coroutine — `run_agent` dutifully
attaches the cost to the CancelledError — and then raises its OWN
`asyncio.TimeoutError` to the runner, an object nothing decorated. The cost dies
with the cancelled task. The CANCELLED (stop requested) path has the same
hardcoded zeros. [[a-shared-surface-must-satisfy-every-boundary-at-once]]:
a patch fitted to one seam parks the residual on the others.

The fix is a cost sink the RUNNER owns and passes in: `run_agent(cost_sink=d)`
accumulates into the caller's dict, so every failure handler — timeout, cancel,
crash — reads the same numbers from the same place. The tool-call count and the
elapsed time below are the production row; the token figure is synthetic (the
row that motivated this file recorded none).
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from app.v3 import agent_runner
from app.v3.agent_runner import run_v3_agent
from app.v3.shared_desk import PhaseOutcome, SharedDesk

ABT_TOOL_CALLS = 18          # agent_tool_telemetry, verbatim
SPENT_TOKENS = 150_000       # synthetic: the real row recorded 0, which is the defect


class _FundamentalAgent:
    AGENT_NAME = "v3_fundamental_analyst"
    ARTIFACT_TYPE = "fundamental_report"
    TOOL_WHITELIST = ["get_earnings_data", "screener_query"]
    SYSTEM_PROMPT = "You are the fundamental analyst. Output JSON."


def _desk(recorded: list) -> SharedDesk:
    desk = SharedDesk(ticker="ABT", cycle_id="cycle-v3-1788660665")
    desk.cycle_metadata = {"ticker": "ABT", "agent_locale": "default"}
    desk.record_agent_telemetry = recorded.append
    return desk


def _spend_then(behaviour):
    """A stand-in for run_agent that spends into the caller's sink the way the
    real one does (in place, cumulatively), then dies the way `behaviour` says."""
    async def _run(**kwargs):
        sink = kwargs.get("cost_sink")
        if sink is not None:
            sink["tokens"] = sink.get("tokens", 0) + SPENT_TOKENS
            sink["tool_calls"] = sink.get("tool_calls", 0) + ABT_TOOL_CALLS
            sink["loops"] = ABT_TOOL_CALLS + 1
        return await behaviour()
    return _run


@pytest.fixture(autouse=True)
def _no_flush(monkeypatch):
    monkeypatch.setattr(agent_runner, "flush_agent_telemetry", lambda d: None, raising=False)


def _row(recorded, outcome):
    rows = [r for r in recorded if r["outcome"] == outcome]
    assert rows, f"no {outcome} row recorded; got {[r['outcome'] for r in recorded]}"
    return rows[-1]


@pytest.mark.asyncio
async def test_a_timed_out_row_carries_what_the_run_had_spent():
    async def _hang():
        await asyncio.sleep(30)

    recorded: list = []
    with patch("app.agents.base_agent.run_agent", new=AsyncMock(side_effect=_spend_then(_hang))):
        outcome = await run_v3_agent(
            desk=_desk(recorded), agent_module=_FundamentalAgent,
            cycle_id="cycle-v3-1788660665", bot_id="b1", timeout_seconds=0.05,
        )
    assert outcome == PhaseOutcome.TIMED_OUT
    row = _row(recorded, "TIMED_OUT")
    assert row["cost_partial"] is True, row
    assert row["prompt_tokens"] == SPENT_TOKENS, row
    assert row["token_usage"] == SPENT_TOKENS, row
    assert row["loops_used"] == ABT_TOOL_CALLS + 1, row


@pytest.mark.asyncio
async def test_a_cancelled_row_carries_what_the_run_had_spent():
    started = asyncio.Event()

    async def _hang_until_cancelled():
        started.set()
        await asyncio.sleep(30)

    recorded: list = []
    with patch("app.agents.base_agent.run_agent", new=AsyncMock(side_effect=_spend_then(_hang_until_cancelled))):
        task = asyncio.create_task(run_v3_agent(
            desk=_desk(recorded), agent_module=_FundamentalAgent,
            cycle_id="cycle-v3-1788660665", bot_id="b1", timeout_seconds=30,
        ))
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    row = _row(recorded, "CANCELLED")
    assert row["cost_partial"] is True, row
    assert row["prompt_tokens"] == SPENT_TOKENS, row
    assert row["loops_used"] == ABT_TOOL_CALLS + 1, row


@pytest.mark.asyncio
async def test_the_crash_path_reads_the_same_sink():
    """One source for all three handlers — the crash path must not regress to
    relying only on the attribute the exception happens to carry."""
    async def _boom():
        raise RuntimeError("provider exploded")

    recorded: list = []
    with patch("app.agents.base_agent.run_agent", new=AsyncMock(side_effect=_spend_then(_boom))):
        outcome = await run_v3_agent(
            desk=_desk(recorded), agent_module=_FundamentalAgent,
            cycle_id="cycle-v3-1788660665", bot_id="b1",
        )
    assert outcome == PhaseOutcome.AGENT_ERROR
    row = _row(recorded, "AGENT_ERROR")
    assert row["cost_partial"] is True
    assert row["prompt_tokens"] == SPENT_TOKENS, row


class TestBaseAgentFillsTheCallersSink:
    def test_run_agent_accepts_a_cost_sink(self):
        import inspect
        from app.agents.base_agent import run_agent
        assert "cost_sink" in inspect.signature(run_agent).parameters

    def test_the_sink_is_the_accumulator_not_a_copy(self):
        """If run_agent copied the sink, a cancellation mid-run would leave the
        caller's dict empty — the exact failure this file is about."""
        import ast, inspect
        from app.agents import base_agent
        src = inspect.getsource(base_agent.run_agent)
        tree = ast.parse(src)
        assigns = [n for n in ast.walk(tree) if isinstance(n, (ast.Assign, ast.AnnAssign))
                   and any(isinstance(t, ast.Name) and t.id == "partial_cost" for t in
                           (n.targets if isinstance(n, ast.Assign) else [n.target]))]
        assert assigns, "partial_cost is no longer assigned in run_agent"
        value = assigns[0].value
        text = ast.get_source_segment(src, value)
        # `cost_sink if cost_sink is not None else {...}` — the branch that
        # takes the sink must be the BARE name. `dict(cost_sink)` also contains
        # the word and is exactly the copy this test exists to forbid.
        assert isinstance(value, ast.IfExp), f"expected a conditional alias, got: {text}"
        assert isinstance(value.body, ast.Name) and value.body.id == "cost_sink", (
            f"partial_cost must BE the caller's sink, not derived from it: {text}"
        )
