import pytest
from unittest.mock import AsyncMock, patch, MagicMock
from app.services.agent_voice_service import generate_agent_quote, SYSTEM_PROMPTS

@pytest.mark.asyncio
async def test_generate_agent_quote_success():
    mock_response = "Here is an extremely long response that is supposed to represent a quant agent talking about eigenvalues."
    
    # Mock llm.chat to return our mock response
    mock_chat = AsyncMock(return_value=(mock_response, 0, 0))
    
    # Mock httpx.AsyncClient post method
    mock_post = AsyncMock()
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_post.return_value = mock_resp

    with patch("app.services.agent_voice_service.llm.chat", mock_chat), \
         patch("httpx.AsyncClient.post", mock_post):
         
        quote = await generate_agent_quote(
            agent_id="QUANT_AGENT_TEST",
            archetype="QUANT",
            context={"ticker": "TSLA", "tool": "test_tool", "action_result": "bullish"}
        )
        
        # Verify llm.chat was called with correct system prompt
        mock_chat.assert_called_once()
        kwargs = mock_chat.call_args[1]
        assert kwargs["system"] == SYSTEM_PROMPTS["QUANT"]
        assert "QUANT_AGENT_TEST" in kwargs["user"]
        assert "TSLA" in kwargs["user"]
        
        # Verify output is stripped to at most 8 words
        words = quote.split()
        assert len(words) <= 8
        assert quote == "Here is an extremely long response that is"
        
        # Verify it attempted to emit
        mock_post.assert_called_once()
        url = mock_post.call_args[0][0]
        assert "trading-client" in url or "localhost" in url or "127.0.0.1" in url or "10.0.0.16" in url
        payload = mock_post.call_args[1]["json"]
        assert payload["type"] == "agent_voice"
        assert payload["agentId"] == "QUANT_AGENT_TEST"
        assert payload["quote"] == quote
        assert payload["context"]["ticker"] == "TSLA"

@pytest.mark.asyncio
async def test_generate_agent_quote_vllm_failure():
    # Mock llm.chat to raise an error
    mock_chat = AsyncMock(side_effect=Exception("vLLM error"))
    
    with patch("app.services.agent_voice_service.llm.chat", mock_chat):
        quote = await generate_agent_quote(
            agent_id="QUANT_AGENT_TEST",
            archetype="QUANT",
            context={"ticker": "TSLA", "tool": "test_tool", "action_result": "bullish"}
        )
        # On failure, it should return empty string so frontend uses its fallback pool
        assert quote == ""
