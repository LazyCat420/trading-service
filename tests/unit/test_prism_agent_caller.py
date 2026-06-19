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


@pytest.mark.asyncio
async def test_prism_agent_caller_streaming_success(mock_vllm_client_for_caller):
    """Test that call_prism_agent successfully streams SSE chunks and parses them."""
    # We patch settings to enable Prism
    with patch.object(settings, "PRISM_ENABLED", True), \
         patch.object(settings, "PRISM_AGENT_ROUTING", True), \
         patch("app.services.vllm_client.llm", mock_vllm_client_for_caller):
         
        # Setup an HTTP Client mock that supports stream()
        mock_client = AsyncMock()
        mock_client._is_mock_json = False
        mock_vllm_client_for_caller.prism_client._get_client = AsyncMock(return_value=mock_client)
        
        # Setup mock stream context
        mock_response = AsyncMock()
        mock_response.status_code = 200
        mock_response.headers = {"content-type": "text/event-stream"}
        
        async def mock_aiter_text():
            yield 'data: {"type": "chunk", "content": "Hello "}\n\n'
            yield 'data: {"type": "chunk", "content": "world!"}\n\n'
            yield 'data: {"type": "done", "usage": {"totalTokens": 10}}\n\n'
            yield 'data: [DONE]\n\n'
            
        mock_response.aiter_text = mock_aiter_text
        
        mock_stream_ctx = MagicMock()
        mock_stream_ctx.__aenter__ = AsyncMock(return_value=mock_response)
        mock_stream_ctx.__aexit__ = AsyncMock()
        mock_client.stream = MagicMock(return_value=mock_stream_ctx)
        
        # Call it
        text, tokens, elapsed = await call_prism_agent(
            agent_id="TEST_AGENT",
            user_message="Hello",
            fallback_system_prompt="system",
            fallback_agent_name="test_agent"
        )
        
        assert text == "Hello world!"
        assert tokens == 10
        mock_client.stream.assert_called_once()


@pytest.mark.asyncio
async def test_prism_agent_caller_stopped_early(mock_vllm_client_for_caller):
    """Test that streaming terminates early if cycle_control is stopped mid-stream."""
    # Setup stopped cycle control
    cc = CycleControl()
    
    with patch.object(settings, "PRISM_ENABLED", True), \
         patch.object(settings, "PRISM_AGENT_ROUTING", True), \
         patch("app.pipeline.orchestration.cycle_control.cycle_control", cc), \
         patch("app.services.vllm_client.llm", mock_vllm_client_for_caller):
         
        mock_client = AsyncMock()
        mock_client._is_mock_json = False
        mock_vllm_client_for_caller.prism_client._get_client = AsyncMock(return_value=mock_client)
        
        mock_response = AsyncMock()
        mock_response.status_code = 200
        mock_response.headers = {"content-type": "text/event-stream"}
        
        async def mock_aiter_text_with_stop():
            yield 'data: {"type": "chunk", "content": "Initial part. "}\n\n'
            # Stop the cycle mid-stream
            cc.stop()
            yield 'data: {"type": "chunk", "content": "This should be ignored."}\n\n'
            yield 'data: {"type": "done", "usage": {"totalTokens": 20}}\n\n'
            yield 'data: [DONE]\n\n'
            
        mock_response.aiter_text = mock_aiter_text_with_stop
        
        mock_stream_ctx = MagicMock()
        mock_stream_ctx.__aenter__ = AsyncMock(return_value=mock_response)
        mock_stream_ctx.__aexit__ = AsyncMock()
        mock_client.stream = MagicMock(return_value=mock_stream_ctx)
        
        # Call it
        text, tokens, elapsed = await call_prism_agent(
            agent_id="TEST_AGENT",
            user_message="Hello",
            fallback_system_prompt="system",
            fallback_agent_name="test_agent"
        )
        
        # Check that "Initial part. " was read, but the second chunk after the stop was ignored.
        assert text == "Initial part. "
        assert tokens == 0  # done event ignored/not reached
