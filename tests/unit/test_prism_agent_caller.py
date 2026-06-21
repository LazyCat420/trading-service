import pytest
import asyncio
import time
from unittest.mock import AsyncMock, patch, MagicMock

from app.services.prism_agent_caller import call_prism_agent
from app.pipeline.orchestration.cycle_control import CycleControl
from app.config import settings

@pytest.fixture
def mock_vllm_client_for_caller():
    """Mock the global llm object for Prism client calls."""
    mock_llm = MagicMock()
    mock_llm.prism_client = MagicMock()
    mock_llm.prism_client.enabled = True
    mock_llm.prism_client.check_health = AsyncMock(return_value=True)
    mock_llm._resolve_model = MagicMock(return_value="qwen-test")
    mock_llm.resolve_provider_for_model = MagicMock(return_value="vllm-spark")
    mock_llm.chat = AsyncMock(return_value=("Mock Chat response", 100, 50))
    
    # Mock payload formatting
    mock_llm.prism_client.get_chat_payload_and_url = MagicMock(return_value=(
        {"test": "payload"},
        "http://fake-prism:7777/agent?stream=false",
        {"Content-Type": "application/json"}
    ))
    return mock_llm

@pytest.mark.asyncio
async def test_prism_agent_caller_mock_fallback(mock_vllm_client_for_caller):
    """Test that call_prism_agent falls back to non-streaming direct endpoint call when HTTP client is a Mock."""
    # We patch settings to enable Prism
    with patch.object(settings, "PRISM_ENABLED", True), \
         patch.object(settings, "PRISM_AGENT_ROUTING", True), \
         patch("app.services.vllm_client.llm", mock_vllm_client_for_caller):
         
        mock_response = MagicMock()
        mock_response.json = MagicMock(return_value={
            "response": {
                "text": "Fallback text response",
                "usage": {"totalTokens": 42}
            }
        })
        mock_vllm_client_for_caller.prism_client._call_endpoint = AsyncMock(return_value=mock_response)
        
        # client is a Mock object
        mock_client = MagicMock()
        mock_client._is_mock_json = True
        mock_vllm_client_for_caller.prism_client._get_client = AsyncMock(return_value=mock_client)
        
        # Trigger
        # This will execute _call_via_prism internally and see is_mock_client = True
        text, tokens, elapsed = await call_prism_agent(
            agent_id="TEST_AGENT",
            user_message="Hello",
            fallback_system_prompt="system",
            fallback_agent_name="test_agent"
        )
        
        assert text == "Fallback text response"
        assert tokens == 42
        mock_vllm_client_for_caller.prism_client._call_endpoint.assert_called_once()
