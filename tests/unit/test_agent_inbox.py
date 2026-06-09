import pytest
import datetime
from app.agents.inbox import AgentInboxManager
from app.agents.agent_loop import run_agent_loop
from app.agents.agent_budget import AgentBudget

@pytest.mark.asyncio
async def test_agent_inbox_manager_lifecycle():
    manager = AgentInboxManager()
    
    # Test registration
    manager.register_instance("test_inst_1", "planner", "AAPL")
    active = manager.get_active_instances()
    assert len(active) == 1
    assert active[0]["agent_name"] == "planner"
    assert active[0]["ticker"] == "AAPL"
    
    # Test unregistration
    manager.unregister_instance("test_inst_1")
    active = manager.get_active_instances()
    assert len(active) == 0

@pytest.mark.asyncio
async def test_agent_inbox_messages():
    manager = AgentInboxManager()
    
    # Add messages
    manager.add_message("planner", "Test message 1", "AAPL")
    manager.add_message("planner", "Test message 2", "MSFT")
    manager.add_message("planner", "Global message")
    
    # Retrieve messages for AAPL
    msgs_aapl = manager.get_messages("planner", "AAPL")
    # Should get "Test message 1" and "Global message"
    assert "Test message 1" in msgs_aapl
    assert "Global message" in msgs_aapl
    assert "Test message 2" not in msgs_aapl
    
    # Retrieve messages again — should be empty (consumed)
    assert len(manager.get_messages("planner", "AAPL")) == 0

@pytest.mark.asyncio
async def test_active_instances_pruning():
    manager = AgentInboxManager()
    
    # Add a normal instance
    manager.register_instance("inst_fresh", "planner", "AAPL")
    
    # Add a stale instance (simulate manually by setting a stale date)
    manager._active_instances["inst_stale"] = {
        "agent_name": "retriever",
        "ticker": "MSFT",
        "status": "running",
        "registered_at": (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(minutes=10)).isoformat()
    }
    
    active = manager.get_active_instances()
    assert len(active) == 1
    assert active[0]["agent_name"] == "planner"
    assert "inst_stale" not in manager._active_instances
