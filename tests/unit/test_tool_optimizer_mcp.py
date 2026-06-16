import pytest
from unittest.mock import patch, MagicMock

from app.services.tool_optimizer import (
    optimize_agent_tools,
    record_tool_optimization_usage,
)

@pytest.mark.asyncio
@patch("app.services.tool_optimizer.get_db")
async def test_optimize_agent_tools_strips_mcp_prefixes(mock_get_db):
    """Verify that optimize_agent_tools strips MCP prefixes when evaluating pruned tools."""
    mock_db = MagicMock()
    mock_get_db.return_value.__enter__.return_value = mock_db
    
    # Simulate DB returning 'get_price' as pruned
    mock_db.fetchall.return_value = [
        ("get_price", "pruned", 5)
    ]
    
    initial_tools = [
        {"name": "mcp__lazy-tool-service__get_price"},
        {"name": "mcp__lazy-tool-service__get_market_data"},
        {"name": "normal_tool"}
    ]
    
    optimized, updated_prompt = await optimize_agent_tools("test_agent", initial_tools, "system_prompt")
    
    # "get_price" was pruned, so its MCP variant should be filtered out.
    names = [t["name"] for t in optimized]
    
    assert len(optimized) == 2
    assert "mcp__lazy-tool-service__get_price" not in names
    assert "mcp__lazy-tool-service__get_market_data" in names
    assert "normal_tool" in names

@pytest.mark.asyncio
@patch("app.services.tool_optimizer.get_db")
async def test_record_tool_optimization_usage_strips_mcp_prefixes(mock_get_db):
    """Verify that record_tool_optimization_usage strips MCP prefixes for both offered and used tools."""
    mock_db = MagicMock()
    mock_get_db.return_value.__enter__.return_value = mock_db
    
    # Return empty existing stats
    mock_db.fetchall.return_value = []
    
    offered_tools = [
        {"name": "mcp__lazy-tool-service__get_price"},
        {"name": "mcp_some_other_tool"}
    ]
    # Simulate the agent using the MCP tool
    used_tool_names = ["mcp__lazy-tool-service__get_price"]
    
    await record_tool_optimization_usage("test_agent", offered_tools, used_tool_names)
    
    execute_calls = mock_db.execute.call_args_list
    
    # Check SELECT query args
    select_call = next((c for c in execute_calls if "SELECT tool_name" in c[0][0]), None)
    assert select_call is not None, "SELECT query was not called"
    
    # The parameters to the SELECT query should be ['test_agent', 'get_price', 'some_other_tool']
    select_params = select_call[0][1]
    assert select_params == ["test_agent", "get_price", "some_other_tool"]
    
    # Check UPSERT queries
    upsert_calls = [c for c in execute_calls if "INSERT INTO agent_tool_optimization" in c[0][0]]
    assert len(upsert_calls) == 2
    
    # get_price was used -> unused_count = 0, active
    upsert_get_price = next((c for c in upsert_calls if c[0][1][1] == "get_price"), None)
    assert upsert_get_price is not None
    assert upsert_get_price[0][1][2] == 0  # unused_count
    assert upsert_get_price[0][1][3] == "active"
    
    # some_other_tool was NOT used -> unused_count = 1, active (0 + 1)
    upsert_other = next((c for c in upsert_calls if c[0][1][1] == "some_other_tool"), None)
    assert upsert_other is not None
    assert upsert_other[0][1][2] == 1  # unused_count
    assert upsert_other[0][1][3] == "active"
