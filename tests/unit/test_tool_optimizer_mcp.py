"""MCP prefix stripping in the tool optimizer.

These used to patch `tool_optimizer.get_db` and read SQL text off the mocked
cursor ("SELECT tool_name", "INSERT INTO agent_tool_optimization") plus its
positional params. The module reads and writes through `mongo_store`
(`find_docs` / `update_docs`) now and imports no `get_db`, so the patch
intercepted nothing: the lookups hit the LIVE store and every SQL assertion was
scanning an empty call list.

They patch `mongo_store` and assert on the Mongo call shapes. The old "SELECT
params == [agent, 'get_price', 'some_other_tool']" becomes an assertion on the
`find_docs` FILTER — the `tool_name: {"$in": [...]}` list — which is the same
claim (the query is issued with prefixes already stripped) expressed against
the call the module actually makes.
"""
import pytest
from unittest.mock import patch, MagicMock

from app.services.tool_optimizer import (
    optimize_agent_tools,
    record_tool_optimization_usage,
)


def _store(opt_docs=None):
    """Patch tool_optimizer's Mongo layer, dispatching find_docs on collection."""
    store = MagicMock()

    def _find_docs(collection, *a, **k):
        if collection == "agent_tool_optimization":
            return list(opt_docs or [])
        return []

    store.find_docs.side_effect = _find_docs
    return patch("app.services.tool_optimizer.mongo_store", store), store


def _opt_lookup(store):
    """The find_docs call that loaded this agent's optimization rows."""
    for c in store.find_docs.call_args_list:
        if c.args and c.args[0] == "agent_tool_optimization":
            return c
    return None


def _counter_writes(store):
    """update_docs calls against agent_tool_optimization that $set a counter."""
    return [
        c for c in store.update_docs.call_args_list
        if c.args and c.args[0] == "agent_tool_optimization" and "$set" in c.args[2]
    ]


@pytest.mark.asyncio
async def test_optimize_agent_tools_strips_mcp_prefixes():
    """Verify that optimize_agent_tools strips MCP prefixes when evaluating pruned tools."""
    # Simulate the store returning 'get_price' as pruned
    ctx, store = _store([
        {"agent_name": "test_agent", "tool_name": "get_price",
         "status": "pruned", "unused_count": 5},
    ])

    initial_tools = [
        {"name": "mcp__lazy-tool-service__get_price"},
        {"name": "mcp__lazy-tool-service__get_market_data"},
        {"name": "normal_tool"}
    ]

    with ctx:
        optimized, updated_prompt = await optimize_agent_tools(
            "test_agent", initial_tools, "system_prompt"
        )

    # "get_price" was pruned, so its MCP variant should be filtered out.
    names = [t["name"] for t in optimized]

    assert len(optimized) == 2
    assert "mcp__lazy-tool-service__get_price" not in names
    assert "mcp__lazy-tool-service__get_market_data" in names
    assert "normal_tool" in names

    # The lookup itself must be issued with prefixes already stripped —
    # otherwise the pruned row could never be matched in the first place.
    lookup = _opt_lookup(store)
    assert lookup is not None, "the optimization rows were never queried"
    assert set(lookup.args[1]["tool_name"]["$in"]) == {
        "get_price", "get_market_data", "normal_tool"
    }
    assert lookup.args[1]["agent_name"] == "test_agent"


@pytest.mark.asyncio
async def test_record_tool_optimization_usage_strips_mcp_prefixes():
    """Verify that record_tool_optimization_usage strips MCP prefixes for both offered and used tools."""
    # Return empty existing stats
    ctx, store = _store([])

    offered_tools = [
        {"name": "mcp__lazy-tool-service__get_price"},
        {"name": "mcp_some_other_tool"}
    ]
    # Simulate the agent using the MCP tool
    used_tool_names = ["mcp__lazy-tool-service__get_price"]

    with ctx:
        await record_tool_optimization_usage("test_agent", offered_tools, used_tool_names)

    # The lookup is the Mongo equivalent of the old SELECT: the offered names
    # must already be stripped when the query goes out.
    lookup = _opt_lookup(store)
    assert lookup is not None, "the optimization rows were never queried"
    assert lookup.args[1]["agent_name"] == "test_agent"
    assert lookup.args[1]["tool_name"]["$in"] == ["get_price", "some_other_tool"]

    # Check the counter writebacks (the old UPSERTs)
    writes = _counter_writes(store)
    assert len(writes) == 2
    by_tool = {c.args[1]["tool_name"]: c.args[2]["$set"] for c in writes}

    # get_price was used -> unused_count = 0, active
    assert "get_price" in by_tool
    assert by_tool["get_price"]["unused_count"] == 0
    assert by_tool["get_price"]["status"] == "active"

    # some_other_tool was NOT used -> unused_count = 1, active (0 + 1)
    assert "some_other_tool" in by_tool
    assert by_tool["some_other_tool"]["unused_count"] == 1
    assert by_tool["some_other_tool"]["status"] == "active"
