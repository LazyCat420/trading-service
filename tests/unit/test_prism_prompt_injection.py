import pytest
from unittest.mock import AsyncMock, patch
from app.services.prism_agent_caller import call_prism_agent

@pytest.mark.asyncio
async def test_call_prism_agent_prepends_system_prompt():
    """Verify that call_prism_agent prepends system prompt to messages array."""
    mock_response = AsyncMock()
    mock_response.text = "{\n  \"selected_tickers\": [\"AAPL\"],\n  \"rationale\": \"test\"\n}"
    
    with patch("app.services.prism_agent_caller.prism_client") as mock_prism_client:
        mock_prism_client.call_agent = AsyncMock(return_value=mock_response)
        
        user_message = "Here is the watchlist table..."
        fallback_system_prompt = "Select tickers based on news"
        fallback_agent_name = "v3_portfolio_manager"
        
        await call_prism_agent(
            agent_id="CUSTOM_V3_PORTFOLIO_MANAGER",
            user_message=user_message,
            fallback_system_prompt=fallback_system_prompt,
            fallback_agent_name=fallback_agent_name,
            temperature=0.1,
            max_tokens=1024
        )
        
        # Verify call_agent arguments
        mock_prism_client.call_agent.assert_called_once()
        kwargs = mock_prism_client.call_agent.call_args.kwargs
        
        messages = kwargs.get("messages")
        assert len(messages) == 3
        assert messages[0]["role"] == "system"
        assert "Select tickers based on news" in messages[0]["content"]
        assert messages[1]["role"] == "user"
        assert "ready to process" in messages[1]["content"]
        assert messages[2]["role"] == "user"
        assert messages[2]["content"] == user_message
