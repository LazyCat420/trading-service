import pytest
import asyncio
import time
from unittest.mock import AsyncMock, patch, MagicMock
from app.services.vllm_client import VLLMClient, Priority, QueueItem, VLLMEndpoint

@pytest.fixture
def mock_vllm_client_for_stream():
    client = VLLMClient()
    client._roles_discovered = True
    
    mock_ep = VLLMEndpoint(
        name="test_ep",
        url="http://fake_vllm:8000",
        role="collector",
        max_concurrent=2,
        purpose="Test endpoint"
    )
    mock_ep.init_concurrency(reserved_high=1)
    mock_ep.model = "qwen-test"
    mock_ep.enabled = True
    
    client._endpoints = {"test_ep": mock_ep}
    client._pick_best_endpoint = MagicMock(return_value=mock_ep)
    client._get_client = AsyncMock()
    return client, mock_ep

@pytest.mark.asyncio
async def test_stream_prism_agent_queue_handshake(mock_vllm_client_for_stream):
    client, ep = mock_vllm_client_for_stream
    
    # Mock httpx streaming response
    async def mock_aiter_bytes():
        yield b"data: {\"type\": \"chunk\", \"content\": \"hello from stream\"}\n\n"
        yield b"data: [DONE]\n\n"
        
    mock_response = AsyncMock()
    mock_response.status_code = 200
    mock_response.aiter_bytes = mock_aiter_bytes
    
    mock_stream_ctx = MagicMock()
    mock_stream_ctx.__aenter__ = AsyncMock(return_value=mock_response)
    mock_stream_ctx.__aexit__ = AsyncMock()
    
    mock_http_client = AsyncMock()
    mock_http_client.stream = MagicMock(return_value=mock_stream_ctx)
    client._get_client.return_value = mock_http_client
    
    # We mock _sync_endpoint_model to avoid model synchronization logic
    client._sync_endpoint_model = AsyncMock()
    
    # We need to run dispatcher for the endpoint.
    # We start the dispatcher loop by calling _ensure_dispatcher.
    # Note that _ensure_dispatcher is normally called in stream_prism_agent.
    # Let's set up the loop and dispatcher.
    client._ensure_dispatcher()
    
    payload = {"model": "qwen-test", "messages": []}
    
    # Call the stream
    chunks = []
    async for chunk in client.stream_prism_agent(payload, priority=Priority.HIGH, agent_name="OMNI"):
        chunks.append(chunk)
        
    # Check that we received the expected chunks
    assert len(chunks) > 0
    assert b"hello from stream" in b"".join(chunks)
    
    # Stop the dispatcher task if running to prevent test leakage
    if ep.dispatcher_task and not ep.dispatcher_task.done():
        ep.dispatcher_task.cancel()
        try:
            await ep.dispatcher_task
        except asyncio.CancelledError:
            pass

@pytest.mark.asyncio
async def test_stream_prism_agent_error_handling(mock_vllm_client_for_stream):
    client, ep = mock_vllm_client_for_stream
    
    # Mock HTTP stream failing (e.g. 500 error)
    mock_response = AsyncMock()
    mock_response.status_code = 500
    mock_response.aread = AsyncMock(return_value=b"Internal Server Error")
    
    mock_stream_ctx = MagicMock()
    mock_stream_ctx.__aenter__ = AsyncMock(return_value=mock_response)
    mock_stream_ctx.__aexit__ = AsyncMock()
    
    mock_http_client = AsyncMock()
    mock_http_client.stream = MagicMock(return_value=mock_stream_ctx)
    client._get_client.return_value = mock_http_client
    client._sync_endpoint_model = AsyncMock()
    
    client._ensure_dispatcher()
    
    payload = {"model": "qwen-test"}
    chunks = []
    async for chunk in client.stream_prism_agent(payload, priority=Priority.HIGH, agent_name="OMNI"):
        chunks.append(chunk)
        
    combined = b"".join(chunks)
    assert b"Prism returned 500" in combined
    
    if ep.dispatcher_task and not ep.dispatcher_task.done():
        ep.dispatcher_task.cancel()
        try:
            await ep.dispatcher_task
        except asyncio.CancelledError:
            pass
