import pytest
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch
from app.services.vllm_client import VLLMClient, QueueItem, Priority
from app.config import settings

@pytest.mark.asyncio
async def test_vllm_client_routing_by_priority():
    """Verify that requests of Priority.HIGH use Prism routing while Priority.NORMAL/LOW bypass it."""
    client = VLLMClient()
    client._roles_discovered = True
    
    mock_ep = MagicMock()
    mock_ep.url = "http://fake_vllm:8000"
    mock_ep.model = "qwen-test"
    mock_ep.enabled = True
    mock_ep.active_count = 0
    mock_ep.max_concurrent = 5
    
    client.prism_client = MagicMock()
    client.prism_client.enabled = True
    client.prism_client.check_health = AsyncMock(return_value=True)
    
    client._get_client = AsyncMock()
    client._sync_endpoint_model = AsyncMock()
    
    # Mock tracker.record to avoid DB insert attempts
    with patch("app.services.vllm_client.tracker") as mock_tracker, \
         patch("app.services.vllm_client.strip_think_tags", return_value=("clean content", "think content")), \
         patch.object(settings, "PRISM_AGENT_ROUTING", True):
        
        mock_tracker.record = AsyncMock()
        
        # 1. Test Priority.HIGH -> routes through Prism
        item_high = QueueItem(
            priority=Priority.HIGH,
            seq=1,
            future=asyncio.Future(),
            payload={"model": "qwen-test", "messages": []},
            metadata={"agent_name": "user_chat", "ticker": "AAPL", "cycle_id": "test"}
        )
        
        with patch.object(client, "_call_prism_agent", new_callable=AsyncMock, return_value=("resp", 10, 10)) as mock_prism, \
             patch.object(client, "_call_vllm_direct", new_callable=AsyncMock, return_value=("resp", 10, 10)) as mock_direct:
            
            await client._execute_item(item_high, mock_ep)
            mock_prism.assert_called_once()
            mock_direct.assert_not_called()
            
        # 2. Test Priority.NORMAL with PRISM_AGENT_ROUTING=True -> still routes through Prism
        item_normal = QueueItem(
            priority=Priority.NORMAL,
            seq=2,
            future=asyncio.Future(),
            payload={"model": "qwen-test", "messages": []},
            metadata={"agent_name": "thesis_agent", "ticker": "AAPL", "cycle_id": "test"}
        )
        
        with patch.object(client, "_call_prism_agent", new_callable=AsyncMock, return_value=("resp", 10, 10)) as mock_prism, \
             patch.object(client, "_call_vllm_direct", new_callable=AsyncMock, return_value=("resp", 10, 10)) as mock_direct:
            
            await client._execute_item(item_normal, mock_ep)
            mock_prism.assert_called_once()
            mock_direct.assert_not_called()

    # 3. Test with PRISM_AGENT_ROUTING=False -> routes directly to vLLM
    with patch("app.services.vllm_client.tracker") as mock_tracker, \
         patch("app.services.vllm_client.strip_think_tags", return_value=("clean content", "think content")), \
         patch.object(settings, "PRISM_AGENT_ROUTING", False):
        
        mock_tracker.record = AsyncMock()
        
        item_normal = QueueItem(
            priority=Priority.NORMAL,
            seq=3,
            future=asyncio.Future(),
            payload={"model": "qwen-test", "messages": []},
            metadata={"agent_name": "thesis_agent", "ticker": "AAPL", "cycle_id": "test"}
        )
        
        with patch.object(client, "_call_prism_agent", new_callable=AsyncMock, return_value=("resp", 10, 10)) as mock_prism, \
             patch.object(client, "_call_vllm_direct", new_callable=AsyncMock, return_value=("resp", 10, 10)) as mock_direct:
            
            await client._execute_item(item_normal, mock_ep)
            mock_prism.assert_not_called()
            mock_direct.assert_called_once()
