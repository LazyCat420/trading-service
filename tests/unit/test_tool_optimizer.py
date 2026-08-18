"""Tool pruning, highlighting and the unused-counter writeback.

These used to patch `tool_optimizer.get_db` and drive a `db.fetchall` cursor.
`app/services/tool_optimizer.py` reads and writes through `mongo_store`
(`find_docs` / `update_docs`) now and imports no `get_db`, so the patch
intercepted nothing: `find_docs` went to the LIVE store, the pruning decisions
came from whatever production held, and "assert execute.call_count > 0" scored
a mock the code never touched.

They patch `mongo_store` now. `find_docs` returns DOCUMENTS (not the tuples the
old cursor yielded), and the writes are asserted structurally — collection,
filter, and the `$set` document — which pins more than the old SQL-substring
scan did: it names which agent/tool pair each counter belongs to.
"""
import pytest
from unittest.mock import MagicMock, patch

from app.services.tool_optimizer import (
    optimize_agent_tools,
    record_tool_optimization_usage,
    record_run_usage_from_db,
)


def _store(opt_docs=None):
    """Patch tool_optimizer's Mongo layer, dispatching find_docs on collection.

    `tool_usage_stats` (the reputation window) is kept empty so the reputation
    block appends nothing and the prompt assertions stay about highlighting.
    """
    store = MagicMock()

    def _find_docs(collection, *a, **k):
        if collection == "agent_tool_optimization":
            return list(opt_docs or [])
        return []

    store.find_docs.side_effect = _find_docs
    return patch("app.services.tool_optimizer.mongo_store", store), store


def _opt(tool_name, status="active", unused_count=0, agent="test_agent"):
    """One agent_tool_optimization document."""
    return {
        "agent_name": agent,
        "tool_name": tool_name,
        "status": status,
        "unused_count": unused_count,
    }


def _counter_writes(store):
    """update_docs calls against agent_tool_optimization that $set a counter."""
    return [
        c for c in store.update_docs.call_args_list
        if c.args and c.args[0] == "agent_tool_optimization" and "$set" in c.args[2]
    ]


@pytest.mark.asyncio
async def test_optimize_agent_tools_filtering():
    # tool1 active, tool2 highlighted, tool3 pruned
    ctx, store = _store([
        _opt("tool1", "active", 0),
        _opt("tool2", "highlighted", 2),
        _opt("tool3", "pruned", 4),
    ])

    initial_tools = [
        {"function": {"name": "tool1"}},
        {"function": {"name": "tool2"}},
        {"function": {"name": "tool3"}}
    ]

    with ctx:
        filtered, prompt = await optimize_agent_tools(
            agent_name="test_agent",
            initial_tools=initial_tools,
            system_prompt="Base prompt."
        )

    # tool3 is pruned, so it should be removed from the returned schemas
    assert len(filtered) == 2
    assert filtered[0]["function"]["name"] == "tool1"
    assert filtered[1]["function"]["name"] == "tool2"

    # tool2 is highlighted, so we expect the nudge in the system prompt.
    # (The banner text moved from "UNDERUSED TOOLS WARNING" to
    # "[RECOMMENDED TOOLS]" in the Mongo port; the invariant asserted here is
    # unchanged — the highlighted tool is named and the pruned one is not.)
    assert "[RECOMMENDED TOOLS]" in prompt
    assert "tool2" in prompt
    assert "tool3" not in prompt


@pytest.mark.asyncio
async def test_record_tool_optimization_usage():
    # tool1 had 1 unused count, tool2 had 3 unused counts, tool3 is new
    ctx, store = _store([
        _opt("tool1", "active", 1),
        _opt("tool2", "highlighted", 3),
    ])

    offered_tools = [
        {"function": {"name": "tool1"}},
        {"function": {"name": "tool2"}},
        {"function": {"name": "tool3"}}  # new tool, not in DB yet
    ]

    # tool1 was used during run, tool2 and tool3 were NOT used
    used_tool_names = ["tool1"]

    with ctx:
        await record_tool_optimization_usage(
            agent_name="test_agent",
            offered_tools=offered_tools,
            used_tool_names=used_tool_names
        )

    # Every offered tool gets its counter written back. The old test only
    # checked "execute was called at all"; the counters are the point, so
    # they are pinned here.
    writes = _counter_writes(store)
    by_tool = {c.args[1]["tool_name"]: c.args[2]["$set"] for c in writes}
    assert set(by_tool) == {"tool1", "tool2", "tool3"}
    for c in writes:
        assert c.args[1]["agent_name"] == "test_agent"
        assert c.kwargs.get("upsert") is True

    # tool1 was used -> counter resets and the tool goes back to active.
    assert by_tool["tool1"]["unused_count"] == 0
    assert by_tool["tool1"]["status"] == "active"
    # tool2 unused: 3 -> 4, which crosses the prune threshold.
    assert by_tool["tool2"]["unused_count"] == 4
    assert by_tool["tool2"]["status"] == "pruned"
    # tool3 is new: absent from the DB means it starts at 0 and becomes 1.
    assert by_tool["tool3"]["unused_count"] == 1
    assert by_tool["tool3"]["status"] == "active"


@pytest.mark.asyncio
async def test_record_tool_optimization_usage_mcp_normalization():
    ctx, store = _store([
        _opt("get_market_data", "active", 1),
        _opt("get_financial_ratios", "highlighted", 3),
    ])

    offered_tools = [
        {"function": {"name": "get_market_data"}},
        {"function": {"name": "get_financial_ratios"}},
    ]

    # Used names come from Prism-routed tool calls and have MCP prefixes
    used_tool_names = [
        "mcp__lazy-tool-service__get_market_data",
        "mcp__lazy-tools__get_financial_ratios"
    ]

    with ctx:
        await record_tool_optimization_usage(
            agent_name="test_agent",
            offered_tools=offered_tools,
            used_tool_names=used_tool_names
        )

    # Verify that both tools were treated as "used" (reset to unused_count=0,
    # status=active) despite the prefixes on the used names.
    writes = _counter_writes(store)
    assert len(writes) == 2
    for c in writes:
        update = c.args[2]["$set"]
        assert update["unused_count"] == 0  # unused_count reset
        assert update["status"] == "active"  # status reset to active
