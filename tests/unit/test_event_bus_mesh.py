import asyncio
import pytest
from app.cycle.orchestration.event_bus import EventBus
from app.cycle.orchestration.agent_mesh import AgentMeshNode

@pytest.mark.asyncio
async def test_event_bus_unsubscribe_and_pattern():
    bus = EventBus()
    bus.start()
    
    received_wildcard = []
    received_direct = []
    
    async def wildcard_callback(payload):
        received_wildcard.append(payload)
        
    async def direct_callback(payload):
        received_direct.append(payload)
        
    # Subscribe
    bus.subscribe("FACT_CHECK_RESOLVED_*", wildcard_callback)
    bus.subscribe("TEST_DIRECT", direct_callback)
    
    # Publish and wait
    bus.publish("FACT_CHECK_RESOLVED_123", {"data": "yes"})
    bus.publish("TEST_DIRECT", {"data": "direct"})
    
    await asyncio.sleep(0.1)
    
    assert len(received_wildcard) == 1
    assert received_wildcard[0]["data"] == "yes"
    assert len(received_direct) == 1
    assert received_direct[0]["data"] == "direct"
    
    # Unsubscribe direct
    bus.unsubscribe("TEST_DIRECT", direct_callback)
    bus.publish("TEST_DIRECT", {"data": "direct_ignored"})
    
    # Unsubscribe wildcard
    bus.unsubscribe("FACT_CHECK_RESOLVED_*", wildcard_callback)
    bus.publish("FACT_CHECK_RESOLVED_456", {"data": "no"})
    
    await asyncio.sleep(0.1)
    
    # Counts should remain 1
    assert len(received_wildcard) == 1
    assert len(received_direct) == 1
    
    bus.stop()

class MockMeshNode(AgentMeshNode):
    def __init__(self):
        super().__init__("mock_node")
        self.received = []

    def register_subscriptions(self):
        self.subscribe("FACT_CHECK_REQUESTED", self.on_request)

    async def on_request(self, payload):
        self.received.append(payload)
        reply_channel = payload.get("reply_channel")
        correlation_id = payload.get("correlation_id")
        
        # Publish resolved fact back
        event_bus = EventBus() # use temporary event bus or local
        from app.cycle.orchestration.event_bus import event_bus as global_bus
        global_bus.publish(reply_channel, {
            "correlation_id": correlation_id,
            "evidence": "verified evidence data"
        })

@pytest.mark.asyncio
async def test_lateral_mesh_communication():
    from app.cycle.orchestration.event_bus import event_bus
    from app.tools.mesh_tools import request_lateral_fact_check
    
    event_bus.start()
    
    node = MockMeshNode()
    await node.start()
    
    # Trigger fact check using the tool
    # Note: request_lateral_fact_check relies on kwargs ticker and cycle_id
    res = await request_lateral_fact_check(
        query="Verify Q1 revenue",
        target_agent="retriever",
        ticker="OKLO",
        cycle_id="test_cycle_1"
    )
    
    assert "verified evidence data" in res
    assert len(node.received) == 1
    assert node.received[0]["query"] == "Verify Q1 revenue"
    
    await node.stop()
    event_bus.stop()
    event_bus.clear()
