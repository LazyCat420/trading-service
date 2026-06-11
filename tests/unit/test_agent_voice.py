import pytest
from unittest.mock import AsyncMock, patch, MagicMock
from app.services.agent_voice_service import generate_agent_quote, SYSTEM_PROMPTS

@pytest.mark.asyncio
async def test_generate_agent_quote_success():
    mock_response = "Here is an extremely long response that is supposed to represent a quant agent talking about eigenvalues and variance decay patterns."
    
    # Mock llm.chat to return our mock response
    mock_chat = AsyncMock(return_value=(mock_response, 0, 0))
    
    # Mock httpx.AsyncClient post method
    mock_post = AsyncMock()
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {"status": "ok", "delivered_to": 1}
    mock_post.return_value = mock_resp

    with patch("app.services.agent_voice_service.llm.chat", mock_chat), \
         patch("app.services.agent_voice_service._get_emit_client") as mock_get_client:
        mock_client = AsyncMock()
        mock_client.post = mock_post
        mock_get_client.return_value = mock_client
         
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
        
        # Ensure the generated quote matches the mock response since the word limit was removed.
        assert quote == mock_response
        
        # Verify it attempted to emit
        mock_post.assert_called_once()
        payload = mock_post.call_args[1]["json"]
        assert payload["type"] == "agent_voice"
        assert payload["agentId"] == "QUANT_AGENT_TEST"
        assert payload["quote"] == quote
        assert payload["context"]["ticker"] == "TSLA"

@pytest.mark.asyncio
async def test_generate_agent_quote_vllm_failure():
    # Mock llm.chat to raise an error
    mock_chat = AsyncMock(side_effect=Exception("vLLM error"))
    
    mock_post = AsyncMock()
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {"status": "ok", "delivered_to": 1}
    mock_post.return_value = mock_resp

    with patch("app.services.agent_voice_service.llm.chat", mock_chat), \
         patch("app.services.agent_voice_service._get_emit_client") as mock_get_client:
        mock_client = AsyncMock()
        mock_client.post = mock_post
        mock_get_client.return_value = mock_client
        quote = await generate_agent_quote(
            agent_id="QUANT_AGENT_TEST",
            archetype="QUANT",
            context={"ticker": "TSLA", "tool": "test_tool", "action_result": "bullish"}
        )
        # On failure, it should return empty string so frontend uses its fallback pool
        assert quote == ""

@pytest.mark.asyncio
async def test_generate_agent_quote_override():
    # Mock shared httpx client
    mock_post = AsyncMock()
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {"status": "ok", "delivered_to": 1}
    mock_post.return_value = mock_resp

    with patch("app.services.agent_voice_service._get_emit_client") as mock_get_client:
        mock_client = AsyncMock()
        mock_client.post = mock_post
        mock_get_client.return_value = mock_client
        quote = await generate_agent_quote(
            agent_id="QUANT_AGENT_TEST",
            archetype="QUANT",
            context={"ticker": "TSLA", "quote_override": "This is an explicit override quote."}
        )
        assert quote == "This is an explicit override quote."
        mock_post.assert_called_once()

@pytest.mark.asyncio
async def test_generate_agent_quote_delegation():
    # Mock shared httpx client
    mock_post = AsyncMock()
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {"status": "ok", "delivered_to": 1}
    mock_post.return_value = mock_resp

    with patch("app.services.agent_voice_service._get_emit_client") as mock_get_client:
        mock_client = AsyncMock()
        mock_client.post = mock_post
        mock_get_client.return_value = mock_client
        quote = await generate_agent_quote(
            agent_id="QUANT_AGENT_TEST",
            archetype="QUANT",
            context={
                "ticker": "TSLA",
                "agent_insight": "Blah blah DELEGATION: @Janitor - check the split error on March 4th. Blah blah"
            }
        )
        assert quote == "Ray, check the split error on March 4th."
        mock_post.assert_called_once()

@pytest.mark.asyncio
async def test_generate_agent_quote_taskboard_finding():
    mock_post = AsyncMock()
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {"status": "ok", "delivered_to": 1}
    mock_post.return_value = mock_resp
    
    mock_chat = AsyncMock(return_value=("Findings verified successfully.", 0, 0))

    # Mock TaskBoard get_findings
    mock_get_findings = AsyncMock(return_value=[
        {"source_agent": "fundamentals_agent", "content": "Company exhibits massive revenue growth."}
    ])

    with patch("app.services.agent_voice_service._get_emit_client") as mock_get_client, \
         patch("app.agents.task_board.task_board.get_findings", mock_get_findings), \
         patch("app.services.agent_voice_service.llm.chat", mock_chat):
        mock_client = AsyncMock()
        mock_client.post = mock_post
        mock_get_client.return_value = mock_client
         
        quote = await generate_agent_quote(
            agent_id="FUNDAMENTAL_AGENT",
            archetype="RESEARCH",
            context={"ticker": "TSLA", "cycle_id": "test-cycle-123", "tool": "test"}
        )
        
        mock_get_findings.assert_called_once_with(ticker="TSLA", cycle_id="test-cycle-123")
        mock_chat.assert_called_once()
        user_prompt = mock_chat.call_args[1]["user"]
        assert "Company exhibits massive revenue growth." in user_prompt

@pytest.mark.asyncio
async def test_step_data_voice_dispatch():
    ctx = MagicMock()
    ctx.ticker = "AAPL"
    ctx.cycle_id = "test-cycle-123"
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
                "cycle_id": "test-cycle-123",
                "tool": "data_processors",
                "action_result": "complete",
            }
        )

@pytest.mark.asyncio
async def test_step_agents_voice_dispatch():
    ctx = MagicMock()
    ctx.ticker = "AAPL"
    ctx.cycle_id = "test-cycle-123"
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
        assert any(c.get("agent_id") == "FUNDAMENTAL_AGENT" and c.get("archetype") == "RESEARCH" for c in calls)
        assert any(c.get("agent_id") == "DEEP_RESEARCH_AGENT" and c.get("archetype") == "QUANT" for c in calls)
        
        for c in calls:
            assert c["context"]["cycle_id"] == "test-cycle-123"
            assert "agent_insight" in c["context"]

@pytest.mark.asyncio
async def test_step_debate_voice_dispatch():
    ctx = MagicMock()
    ctx.ticker = "AAPL"
    ctx.cycle_id = "test-cycle-123"
    ctx.orchestrator_had_agents = True
    ctx.elapsed_s.return_value = -1000.0
    ctx.debate_result = MagicMock()
    ctx.debate_result.winning_side = "bear"
    ctx.debate_result.judge_action = "SELL"
    ctx.debate_result.transcript = "### BULL ARGUMENTS\n...\n### BEAR ARGUMENTS\n..."
    
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
                "cycle_id": "test-cycle-123",
                "tool": "adversarial_debate",
                "action_result": "SELL",
                "agent_insight": "### BULL ARGUMENTS\n...\n### BEAR ARGUMENTS\n...",
            }
        )

@pytest.mark.asyncio
async def test_step_thesis_voice_dispatch():
    ctx = MagicMock()
    ctx.ticker = "AAPL"
    ctx.cycle_id = "test-cycle-123"
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
                "cycle_id": "test-cycle-123",
                "tool": "thesis_generation",
                "action_result": "BUY",
                "agent_insight": "rationale",
            }
        )
