"""`execution_errors.phase` and `.ticker` must name the stage, not a sentinel.

Chapter 41 fixed the first of the three columns `DbLoggingHandler` resolves.
The other two had exactly ONE rung — the log record attribute — and nothing in
`app/` has ever passed `extra={"phase": ...}`, so 'unknown' and 'system' were
not fallbacks, they were the only possible outcomes. All 77 rows of the first
correctly-attributed cycle carried them.

These tests pin the resolution order, the precedence of an explicit `extra`,
and the fact that the ambient scope is consulted at all.
"""

from __future__ import annotations

import logging
from unittest.mock import patch

import pytest

from app.services.logging.unified_logger import DbLoggingHandler
from app.tools.tool_context import (
    clear_tool_context,
    current_agent_name,
    current_cycle_id_or_none,
    current_phase,
    current_ticker,
    tool_context,
)
from app.utils.trace import _trace_id_var, set_trace_id


@pytest.fixture(autouse=True)
def _clean():
    clear_tool_context()
    token = _trace_id_var.set(None)
    yield
    _trace_id_var.reset(token)
    clear_tool_context()


def _capture(record: logging.LogRecord) -> tuple:
    """Run the handler and return the (cycle_id, phase, ticker) it resolved."""
    seen: dict = {}

    def _fake_write(self, cycle_id, phase, ticker, error_type, msg, stack, levelname):
        seen["triple"] = (cycle_id, phase, ticker)

    with patch.object(DbLoggingHandler, "_write_to_db", _fake_write):
        DbLoggingHandler().emit(record)
    return seen.get("triple")


def _record(msg: str = "boom", **extra) -> logging.LogRecord:
    rec = logging.LogRecord("app.test", logging.ERROR, __file__, 1, msg, (), None)
    for k, v in extra.items():
        setattr(rec, k, v)
    return rec


def test_sentinels_when_nothing_is_scoped():
    assert _capture(_record()) == ("system-log", "unknown", "system")


def test_the_ambient_scope_supplies_all_three():
    with tool_context(cycle_id="cycle-42", ticker="AAPL", phase="junior_analyst"):
        assert _capture(_record()) == ("cycle-42", "junior_analyst", "AAPL")


def test_an_explicit_extra_beats_the_ambient_scope():
    """A caller who names the phase knows something the scope does not."""
    with tool_context(cycle_id="cycle-42", ticker="AAPL", phase="junior_analyst"):
        triple = _capture(_record(phase="freshness_gate", ticker="MSFT"))
    assert triple == ("cycle-42", "freshness_gate", "MSFT")


def test_trace_id_still_outranks_the_scoped_cycle_id():
    """Chapter 41's rung stays where it was."""
    set_trace_id("cycle-from-trace")
    with tool_context(cycle_id="cycle-from-scope", phase="analyzing"):
        assert _capture(_record())[0] == "cycle-from-trace"


def test_the_scoped_cycle_id_covers_the_http_bridge():
    """The bridge sets tool context from headers and never calls set_trace_id."""
    with tool_context(cycle_id="cycle-from-bridge", phase="analyzing"):
        assert _capture(_record())[0] == "cycle-from-bridge"


def test_partial_scope_falls_back_per_column():
    with tool_context(phase="precollect"):
        assert _capture(_record()) == ("system-log", "precollect", "system")


def test_handler_does_not_recurse_through_current_cycle_id():
    """The handler must not call the warning-emitting accessor.

    `current_cycle_id()` logs a warning when it resolves nothing, and that
    warning is a log record, which re-enters this handler.
    """
    import ast
    import inspect
    import textwrap

    tree = ast.parse(textwrap.dedent(inspect.getsource(DbLoggingHandler.emit)))
    # AST, not the source text: a COMMENT naming the function it must not call
    # would fail a substring check, and the comment is worth keeping.
    called = {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert "current_cycle_id_or_none" in called, "the scoped cycle id is not consulted"
    assert "current_cycle_id" not in called, (
        "current_cycle_id() warns when it resolves nothing, and that warning is "
        "a log record which re-enters this handler"
    )


class _FakeDesk:
    ticker = "AAPL"


class _FakeAgent:
    AGENT_NAME = "v3_bull_researcher"


async def test_the_agent_scope_names_the_stage_not_just_the_agent():
    """`bull_argument` and `bull_defense` are the SAME agent, two stages.

    Exercised through the decorator rather than through a real agent run: the
    decorator is the whole mechanism, and running an agent would need a live
    model.
    """
    from app.v3.agent_runner import _scoped_to_the_agent

    seen: list[tuple] = []

    @_scoped_to_the_agent
    async def probe(desk, agent_module, **kwargs):
        seen.append((current_cycle_id_or_none(), current_phase(), current_ticker(), current_agent_name()))
        return "ok"

    assert await probe(_FakeDesk(), _FakeAgent(), cycle_id="cycle-7", phase="bull_defense") == "ok"
    assert seen == [("cycle-7", "bull_defense", "AAPL", "v3_bull_researcher")]

    # And the scope closed on the way out.
    assert current_phase() is None


async def test_the_agent_scope_falls_back_to_the_agent_name():
    from app.v3.agent_runner import _scoped_to_the_agent

    seen: list[str | None] = []

    @_scoped_to_the_agent
    async def probe(desk, agent_module, **kwargs):
        seen.append(current_phase())

    await probe(_FakeDesk(), _FakeAgent(), cycle_id="cycle-8")
    assert seen == ["v3_bull_researcher"]


def test_run_v3_agent_still_accepts_every_documented_keyword():
    """The decorator must not swallow or rename any caller's argument."""
    import inspect

    from app.v3.agent_runner import run_v3_agent

    params = inspect.signature(run_v3_agent).parameters
    for name in (
        "desk", "agent_module", "cycle_id", "bot_id", "emit", "timeout_seconds",
        "include_debate_context", "custom_instructions", "parent_agent", "is_retry",
    ):
        assert name in params, f"run_v3_agent lost its {name!r} parameter"


def test_a_phase_is_carried_into_a_worker_thread(monkeypatch):
    """End to end: scope -> executor -> log record -> resolved triple."""
    import asyncio

    from app.utils.async_utils import run_in_executor_with_context

    def _log_from_worker():
        return _capture(_record("failed inside a collector"))

    async def _run():
        with tool_context(cycle_id="cycle-99", ticker="NVDA", phase="precollect"):
            return await run_in_executor_with_context(_log_from_worker)

    assert asyncio.run(_run()) == ("cycle-99", "precollect", "NVDA")
