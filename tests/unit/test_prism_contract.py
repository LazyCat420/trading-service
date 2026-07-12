import pytest
from app.services.prism_agent_caller import PrismClient

@pytest.mark.asyncio
async def test_prism_agent_call_mock(mock_prism_agent):
    """Test calling the mocked Prism Agent SSE endpoint."""
    client = PrismClient("http://localhost:8000")
    payload = {
        "project": "test-project",
        "username": "test-user",
        "messages": [{"role": "user", "content": "hello"}]
    }
    
    # It returns an async generator
    response_gen = client.stream_agent(payload)
    events = []
    async for event in response_gen:
        events.append(event)
        
    assert len(events) == 3
    assert events[0].get("status") == "starting"
    assert events[-1].get("status") == "completed"
