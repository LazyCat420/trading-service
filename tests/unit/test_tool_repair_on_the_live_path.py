"""The arg repair must sit on the path V3 agents actually use (2026-07-30).

`app/v3/tool_repair.py` shipped 2026-07-29 (20cf56a) wired ONLY into the local
AgentHarness hook (`on_tool_call` in app/agents/base_agent.py). That hook never
runs for a V3 pipeline agent: those execute inside prism-service, so their tool
calls arrive over HTTP from lazy-agent-service at
`POST /agent-tools/execute` and go straight to `registry.execute_tool_call`.

So the repair was dead on exactly the path that needed it. Measured before the
fix:

    get_sec_filings malformed-arg rejections .... 16
    all from ......... v3_fundamental_analyst
    ticker known ..... yes, on every row
    repairs recorded . 0
    two of them landed AFTER the repair shipped

This is the "shipped the mechanism, not the capability" failure: the module,
its allow-list and its tests were all correct, and nothing called it.
"""

import inspect
import json

import pytest

from app.routers import agent_tools_router
from app.v3.tool_repair import REPAIRABLE_TICKER_TOOLS, repair_tool_arguments


def test_the_http_bridge_repairs_before_dispatch():
    """The bridge is the live path — the repair must run there."""
    src = inspect.getsource(getattr(agent_tools_router, "_execute_tool_scoped", agent_tools_router.execute_tool))
    assert "repair_tool_arguments" in src, (
        "POST /agent-tools/execute must repair arguments before "
        "registry.execute_tool_call validates them — this is the path V3 "
        "agents reach through prism, and the AgentHarness hook does not run here"
    )
    # It must repair the object it actually dispatches, not payload.arguments.
    assert "json.dumps(_arguments)" in src, (
        "the repaired dict must be what gets dispatched"
    )


def test_repair_runs_before_the_tool_call_is_built():
    src = inspect.getsource(getattr(agent_tools_router, "_execute_tool_scoped", agent_tools_router.execute_tool))
    assert src.index("repair_tool_arguments") < src.index('"call_lazy_tool_bridge"'), (
        "repair must precede building the tool_call payload"
    )


def test_repair_failure_cannot_block_a_tool_call():
    """A raise here would kill the agent's turn for a cosmetic fix."""
    src = inspect.getsource(getattr(agent_tools_router, "_execute_tool_scoped", agent_tools_router.execute_tool))
    seg = src[src.index("repair_tool_arguments"):]
    seg = seg[:seg.index('"call_lazy_tool_bridge"')]
    assert "except" in seg, "the repair must be wrapped — it may never block"


# ── behaviour of the repair itself on the failing shape ──────────────

def test_the_exact_failing_call_is_repaired():
    """The real shape from telemetry: extra args, no ticker."""
    args = {"form_type": "10-K", "limit": 5}
    repaired = repair_tool_arguments(
        "mcp__lazy-tool-service__get_sec_filings", args,
        ticker="NVDA", record=False,
    )
    assert repaired == ["ticker"]
    assert args["ticker"] == "NVDA"


def test_a_ticker_the_model_chose_is_never_overwritten():
    args = {"ticker": "AMD"}
    assert repair_tool_arguments("get_sec_filings", args, ticker="NVDA",
                                 record=False) == []
    assert args["ticker"] == "AMD"


def test_no_ticker_in_context_means_no_repair():
    args = {"form_type": "10-K"}
    assert repair_tool_arguments("get_sec_filings", args, ticker="",
                                 record=False) == []
    assert "ticker" not in args


@pytest.mark.parametrize("tool", ["buy_stock", "sell_stock", "watch_ticker",
                                  "remove_from_watchlist", "escalate_to_pm"])
def test_order_and_state_tools_are_never_repaired(tool):
    """Fail-closed: injecting a ticker into an ORDER is not a cosmetic fix."""
    assert tool not in REPAIRABLE_TICKER_TOOLS
    args = {}
    assert repair_tool_arguments(tool, args, ticker="NVDA", record=False) == []
    assert args == {}


def test_get_sec_filings_is_covered():
    """The tool that produced every measured rejection."""
    assert "get_sec_filings" in REPAIRABLE_TICKER_TOOLS
