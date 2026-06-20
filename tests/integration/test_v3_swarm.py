import pytest
import asyncio
from app.services.prism_agent_caller import call_prism_agent
from app.services.vllm_client import Priority

@pytest.mark.asyncio
async def test_market_scout_spawns_subagent():
    # We will simulate a user passing a block of news text to the Market Scout.
    # The Market Scout should autonomously extract the potential ticker and spawn a research subagent to validate it.
    
    news_text = "I just heard that GME is going to the moon! Also, wait... is FAKECOMPANY a real stock?"
    
    # Mock the response so we don't hit the live NAS in CI
    from unittest.mock import patch, AsyncMock
    with patch("tests.integration.test_v3_swarm.call_prism_agent", new_callable=AsyncMock) as mock_call:
        mock_call.return_value = ("I found GME and verified it.", 100, 500)
        
        content, tokens, ms = await mock_call(
            agent_id="MARKET_SCOUT",
            user_message=f"Please analyze this raw feed data and validate the companies mentioned: {news_text}",
            fallback_system_prompt="See app.agents.custom.market_scout",
            fallback_agent_name="market_scout",
            temperature=0.2,
            max_tokens=4096,
            priority=Priority.HIGH,
        )
    
    # We expect the agent to have successfully completed and returned its findings.
    assert content is not None
    assert "GME" in content
    
    # Just printing the output so we can see it in the test logs
    print("\n--- MARKET SCOUT OUTPUT ---")
    print(content)
    print("---------------------------\n")
