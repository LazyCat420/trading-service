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

@pytest.mark.asyncio
async def test_step_data_voice_dispatch():
    ctx = MagicMock()
    ctx.ticker = "AAPL"
    ctx.emit = MagicMock()
    ctx.elapsed_ms.return_value = 100
    
    with patch("app.pipeline.data.data_completeness.check_and_fill", new_callable=AsyncMock) as mock_fill, \
         patch("app.pipeline.data.data_perticker_collection.run_ticker_processors", new_callable=AsyncMock) as mock_proc, \
         patch("app.services.agent_voice_service.dispatch_agent_quote") as mock_dispatch:
        
        mock_fill.return_value = {"filled": []}
        mock_proc.return_value = None
        
        from app.ticker_pipeline.step_data import run_data_step
        await run_data_step(ctx)
        
        mock_dispatch.assert_called_once_with(
            agent_id="DATA_JANITOR_AGENT",
            archetype="DATA_JANITOR",
            context={
                "ticker": "AAPL",
                "tool": "data_processors",
                "action_result": "complete",
            }
        )

@pytest.mark.asyncio
async def test_step_agents_voice_dispatch():
    ctx = MagicMock()
    ctx.ticker = "AAPL"
    insights = {
        "sentiment": "bullish sentiment looks strong",
        "macro_risk": "some macro risks, interest rates are high",
        "fundamentals": "solid fundamentals, PE ratio is 15",
        "deep_research": "non-obvious catalyst found",
    }
    ctx.agent_insights = insights
    
    with patch("app.cognition.orchestration.meta_orchestrator.MetaOrchestrator.orchestrate", new_callable=AsyncMock) as mock_orch, \
         patch("app.services.agent_voice_service.dispatch_agent_quote") as mock_dispatch:
        
        mock_orch.return_value = (insights, 100)
        from app.ticker_pipeline.step_agents import run_agents_step
        await run_agents_step(ctx)
        
        # Should call dispatch for each of the 4 agents
        assert mock_dispatch.call_count == 4
        calls = []
        for c in mock_dispatch.mock_calls:
            # Unpack the mock call tuple (name, args, kwargs)
            _, args, kwargs = c
            call_dict = {}
            if args:
                if len(args) >= 1: call_dict["agent_id"] = args[0]
                if len(args) >= 2: call_dict["archetype"] = args[1]
                if len(args) >= 3: call_dict["context"] = args[2]
            if kwargs:
                call_dict.update(kwargs)
            calls.append(call_dict)
            
        assert any(c.get("agent_id") == "SENTIMENT_AGENT" and c.get("archetype") == "BULL" for c in calls)
        assert any(c.get("agent_id") == "MACRO_RISK_AGENT" and c.get("archetype") == "RISK" for c in calls)
        assert any(c.get("agent_id") == "FUNDAMENTAL_AGENT" and c.get("archetype") == "QUANT" for c in calls)
        assert any(c.get("agent_id") == "DEEP_RESEARCH_AGENT" and c.get("archetype") == "RESEARCH" for c in calls)

@pytest.mark.asyncio
async def test_step_debate_voice_dispatch():
    ctx = MagicMock()
    ctx.ticker = "AAPL"
    ctx.orchestrator_had_agents = True
    ctx.elapsed_s.return_value = -1000.0
    ctx.debate_result = MagicMock()
    ctx.debate_result.winning_side = "bear"
    ctx.debate_result.judge_action = "SELL"
    
    with patch("app.cognition.debate.debate_coordinator.run_adversarial_debate", new_callable=AsyncMock) as mock_debate_run, \
         patch("app.services.agent_voice_service.dispatch_agent_quote") as mock_dispatch:
        
        mock_debate_run.return_value = ctx.debate_result
        from app.ticker_pipeline.step_debate import run_debate_step
        await run_debate_step(ctx)
        
        mock_dispatch.assert_called_once_with(
            agent_id="BEARISH_DEBATER",
            archetype="BEAR",
            context={
                "ticker": "AAPL",
                "tool": "adversarial_debate",
                "action_result": "SELL",
            }
        )

@pytest.mark.asyncio
async def test_step_thesis_voice_dispatch():
    ctx = MagicMock()
    ctx.ticker = "AAPL"
    ctx.final_action = "BUY"
    ctx.portfolio_dashboard = ""
    ctx.position_context = {"held": False}
    ctx.ontology_ctx = {"ontology_context": ""}
    ctx.memory_context = {"memory_brief": ""}
    ctx.agent_insights = {}
    ctx.debate_result = None
    ctx.macro_memo = ""
    
    ctx.thesis = MagicMock()
    ctx.thesis.confidence = 85
    ctx.thesis.core_claims = ["some claim"]
    ctx.thesis.rationale = "rationale"
    ctx.thesis.action = "BUY"
    
    with patch("app.agents.debate_agents.thesis_agent.generate_thesis", new_callable=AsyncMock) as mock_gen, \
         patch("app.services.logging.meta_auditor.audit_thesis_quality", new_callable=AsyncMock) as mock_audit, \
         patch("app.cognition.lesson_store.retrieve_lessons", return_value=[]) as mock_lessons, \
         patch("app.services.agent_voice_service.dispatch_agent_quote") as mock_dispatch:
        
        mock_gen.return_value = (ctx.thesis, 100)
        mock_audit.return_value = "pass"
        
        from app.ticker_pipeline.step_thesis import run_thesis_step
        await run_thesis_step(ctx)
        
        mock_dispatch.assert_called_once_with(
            agent_id="QUANT_RESEARCH_AGENT",
            archetype="QUANT",
            context={
                "ticker": "AAPL",
                "tool": "thesis_generation",
                "action_result": "BUY",
            }
        )
