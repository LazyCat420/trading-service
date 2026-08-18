"""`tool_context()` must RESTORE what it replaced, not blank it.

`set_tool_context` calls `ContextVar.set` and throws the Token away, so there
is nothing to reset; `clear_tool_context()` sets None, which is not a restore.
That was survivable while every value was scoped to a whole agent run and the
next agent simply overwrote it. A phase is narrower than an agent run — one
agent runs inside a cycle that already has a phase, and pre-collect runs
outside any agent — so an inner scope that blanked the outer one would lose
attribution for everything after it.
"""

from __future__ import annotations

import asyncio

import pytest

from app.tools.tool_context import (
    clear_tool_context,
    current_agent_name,
    current_cycle_id_or_none,
    current_phase,
    current_ticker,
    set_tool_context,
    tool_context,
)


@pytest.fixture(autouse=True)
def _clean():
    clear_tool_context()
    yield
    clear_tool_context()


def test_exit_restores_the_outer_values():
    with tool_context(cycle_id="cycle-outer", ticker="AAPL", phase="analyzing"):
        with tool_context(phase="junior_analyst"):
            assert current_phase() == "junior_analyst"
            assert current_ticker() == "AAPL"       # inherited, not cleared
            assert current_cycle_id_or_none() == "cycle-outer"
        assert current_phase() == "analyzing"        # RESTORED, not None
        assert current_ticker() == "AAPL"

    assert current_phase() is None
    assert current_ticker() is None


def test_exit_restores_on_exception():
    with tool_context(phase="analyzing", ticker="AAPL"):
        with pytest.raises(RuntimeError):
            with tool_context(phase="bear_rebuttal", ticker="MSFT"):
                raise RuntimeError("the agent blew up")
        assert current_phase() == "analyzing"
        assert current_ticker() == "AAPL"


def test_only_supplied_values_are_pushed():
    with tool_context(cycle_id="cycle-1", ticker="AAPL", phase="analyzing"):
        with tool_context(ticker="MSFT"):
            assert current_ticker() == "MSFT"
            assert current_phase() == "analyzing"
            assert current_cycle_id_or_none() == "cycle-1"


def test_values_are_normalized_the_same_way_as_the_imperative_form():
    with tool_context(agent_name="CUSTOM_V3_JUNIOR_ANALYST", ticker=" msft ", phase=" Precollect "):
        assert current_agent_name() == "v3_junior_analyst"
        assert current_ticker() == "MSFT"
        assert current_phase() == "precollect"


def test_a_prism_conversation_uuid_is_not_accepted_as_a_cycle():
    """Same rule as `set_tool_context` — the bridge forwards a UUID here."""
    with tool_context(cycle_id="cycle-real"):
        with tool_context(cycle_id="6f1b6e8a-1b3c-4d2e-9a77-2b0c9d5e4f31"):
            assert current_cycle_id_or_none() == "cycle-real"


def test_blank_values_do_not_push_a_scope():
    with tool_context(ticker="AAPL"):
        with tool_context(ticker="", phase=None):
            assert current_ticker() == "AAPL"


def test_current_cycle_id_or_none_never_logs():
    """`DbLoggingHandler` calls it, and a warning from it would re-enter the handler."""
    import logging

    records: list[logging.LogRecord] = []

    class _Catch(logging.Handler):
        def emit(self, record):
            records.append(record)

    root = logging.getLogger()
    h = _Catch()
    root.addHandler(h)
    try:
        assert current_cycle_id_or_none() is None
    finally:
        root.removeHandler(h)

    assert not records, "reading the scoped cycle id must not log"


async def test_scopes_in_sibling_tasks_are_independent():
    order: list[str] = []

    async def worker(name: str, ticker: str):
        with tool_context(ticker=ticker, phase=name):
            await asyncio.sleep(0)
            order.append(f"{name}:{current_ticker()}")
            assert current_phase() == name

    await asyncio.gather(worker("junior_analyst", "AAPL"), worker("quant_analyst", "MSFT"))
    assert sorted(order) == ["junior_analyst:AAPL", "quant_analyst:MSFT"]


def test_set_tool_context_still_works_for_the_http_bridge():
    """Three routers call the imperative form; it must keep its behaviour."""
    set_tool_context(agent_name="v3_quant_analyst", cycle_id="cycle-http", ticker="tsla", phase="analyzing")
    assert current_agent_name() == "v3_quant_analyst"
    assert current_cycle_id_or_none() == "cycle-http"
    assert current_ticker() == "TSLA"
    assert current_phase() == "analyzing"
