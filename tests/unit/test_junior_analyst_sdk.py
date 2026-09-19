import pytest
import os
from unittest.mock import patch, AsyncMock
from app.v3.agent_runner import run_v3_agent
from app.v3.shared_desk import SharedDesk
from app.v3.agents import junior_analyst
from lazycat import RunResult, RunUsage, StructuredError


@pytest.mark.asyncio
@pytest.mark.real_mongo
async def test_junior_analyst_uses_v2_sdk_when_flag_enabled():
    desk = SharedDesk(ticker="AAPL")
    
    mock_run_result = RunResult(
        run_id="run-sdk-analyst-1",
        status="completed",
        messages=[{
            "role": "assistant",
            "content": '{"summary": "Mock SDK response", "key_findings": ["Strong Q3"], "data_gaps": [], "confidence": 80, "leads_to_trace": [], "triage_recommendation": "FULL", "catalyst_call": {"direction": "NEUTRAL", "catalyst": "none", "already_priced_in": false, "conviction": 55}}'
        }],
        usage=RunUsage(
            prompt_tokens=100,
            completion_tokens=50,
            total_tokens=150,
            tool_calls_count=1,
        )
    )

    with patch.dict(os.environ, {"USE_V2_SDK": "true"}), \
         patch("lazycat.RuntimeClient.create_run", new_callable=AsyncMock) as mock_create_run:
        
        mock_create_run.return_value = mock_run_result

        with patch("app.v3.data_trace.record"), \
             patch("app.v3.data_trace.current_span", return_value=None):
            outcome = await run_v3_agent(desk=desk, agent_module=junior_analyst)
            
        mock_create_run.assert_called_once()
        req = mock_create_run.call_args[0][0]
        assert req.profile_id == "v3_junior_analyst"
        assert req.budget.max_tool_calls == 7
        assert "get_finnhub_news" in [t["name"] for t in req.tools]
        user_input_content = req.input[1].content if hasattr(req.input[1], "content") else req.input[1]["content"]
        assert "Begin your analysis now." in user_input_content

        # Verify desk_note was populated properly from SDK output
        assert desk.desk_note is not None
        assert desk.desk_note.get("summary") == "Mock SDK response"


@pytest.mark.asyncio
@pytest.mark.real_mongo
async def test_junior_analyst_fallback_to_legacy_when_flag_disabled():
    desk = SharedDesk(ticker="AAPL")
    
    with patch.dict(os.environ, {"USE_V2_SDK": "false"}), \
         patch("app.agents.base_agent.run_agent") as mock_legacy_run, \
         patch("lazycat.RuntimeClient.create_run", new_callable=AsyncMock) as mock_sdk_run, \
         patch("asyncio.wait_for", new_callable=AsyncMock) as mock_wait_for:
        
        valid_json = '{"summary": "test", "key_findings": [], "data_gaps": [], "confidence": 80, "leads_to_trace": [], "triage_recommendation": "SKIP", "catalyst_call": {"direction": "NEUTRAL", "catalyst": "test", "already_priced_in": false, "conviction": 55}}'
        mock_wait_for.return_value = {"response": valid_json, "loops_used": 1, "tokens_used": 100}
        
        with patch("app.v3.data_trace.record"), \
             patch("app.v3.data_trace.current_span", return_value=None):
            await run_v3_agent(desk=desk, agent_module=junior_analyst)
            
        mock_sdk_run.assert_not_called()
        mock_wait_for.assert_called_once()
