import pytest
from unittest.mock import patch, MagicMock
from app.services.vllm_client import VLLMClient
from app.config import settings
from app.tools.prism_agent_harness import run_prism_agent

@pytest.mark.asyncio
async def test_mock_llm_discovery_and_routing():
    """Verify that when MOCK_LLM=True, endpoint discovery does not fail,
    _pick_best_endpoint returns a mock fallback endpoint, and completions work.
    """
    client = VLLMClient()
    # Force mock settings
    with patch.object(settings, "MOCK_LLM", True), \
         patch.object(settings, "PRISM_FALLBACK_MODEL", "gemini-test-model"):
         
        # 1. Test health check
        health_status = await client.health_all()
        assert health_status.get("jetson") is True
        assert health_status.get("dgx_spark") is True

        # 2. Test discover_roles
        # Ensure that discovery does not raise errors even if endpoints are down
        roles = await client.discover_roles()
        assert client.model == "gemini-test-model"

        # 3. Test endpoint selection fallback
        ep = client._pick_best_endpoint()
        assert ep.name in ("fallback_ep", "jetson", "dgx_spark")
        assert ep.model == "gemini-test-model"

        # 4. Test chat completions
        content, tokens, ms = await client.chat(
            system="system instructions",
            user="user query",
            agent_name="pre_trade",
            ticker="AAPL"
        )
        assert "APPROVE" in content or "VETO" in content
        assert "AAPL" in content

        # 5. Test other agent response
        content_alloc, _, _ = await client.chat(
            system="system instructions",
            user="user query",
            agent_name="portfolio_allocator",
            ticker="NVDA"
        )
        assert "allocations" in content_alloc
        assert "NVDA" in content_alloc

@pytest.mark.asyncio
async def test_mock_prism_agent_harness():
    """Verify run_prism_agent directly returns mock result when MOCK_LLM is True."""
    with patch.object(settings, "MOCK_LLM", True):
        result = await run_prism_agent(
            system_prompt="sys",
            user_prompt="user",
            ticker="AAPL",
            agent_name="pre_trade"
        )
        assert result["routed_via"] == "mock"
        assert "decision" in result["final_text"]
        assert "AAPL" in result["final_text"]
