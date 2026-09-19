import pytest
import os
from unittest.mock import patch, AsyncMock
from app.v3.agent_runner import run_v3_agent
from app.v3.shared_desk import SharedDesk
from app.v3.agents import junior_analyst

@pytest.mark.asyncio
@pytest.mark.real_mongo
async def test_junior_analyst_uses_v2_sdk_when_flag_enabled():
    desk = SharedDesk(ticker="AAPL")
    
    with patch.dict(os.environ, {"USE_V2_SDK": "true"}), \
         patch("app.agents.sdk_adapter.run_analyst_via_sdk", new_callable=AsyncMock) as mock_run:
        
        mock_run.return_value = {
            "response": '{"summary": "Mock SDK response", "key_findings": [], "data_gaps": [], "confidence": 80, "leads_to_trace": [], "triage_recommendation": "FULL", "catalyst_call": {"direction": "NEUTRAL", "catalyst": "none", "already_priced_in": false, "conviction": 55}}',
            "loops_used": 1,
            "tokens_used": 150,
            "prompt_tokens": 100,
            "completion_tokens": 50
        }
        
        # We need to stub out tracing and other hooks that might fail
        with patch("app.v3.data_trace.record"), \
             patch("app.v3.data_trace.current_span", return_value=None):
            await run_v3_agent(desk=desk, agent_module=junior_analyst)
            
        mock_run.assert_called_once()
        args, kwargs = mock_run.call_args
        assert args[0] == "v3_junior_analyst"
        assert "Begin your analysis now." in args[1] # Check that the user prompt is passed correctly

@pytest.mark.asyncio
@pytest.mark.real_mongo
async def test_junior_analyst_fallback_to_legacy_when_flag_disabled():
    desk = SharedDesk(ticker="AAPL")
    
    with patch.dict(os.environ, {"USE_V2_SDK": "false"}), \
         patch("app.agents.base_agent.run_agent") as mock_legacy_run, \
         patch("app.agents.sdk_adapter.run_analyst_via_sdk", new_callable=AsyncMock) as mock_sdk_run, \
         patch("asyncio.wait_for", new_callable=AsyncMock) as mock_wait_for:
        
        valid_json = '{"summary": "test", "key_findings": [], "data_gaps": [], "confidence": 80, "leads_to_trace": [], "triage_recommendation": "SKIP", "catalyst_call": {"direction": "NEUTRAL", "catalyst": "test", "already_priced_in": false, "conviction": 55}}'
        mock_wait_for.return_value = {"response": valid_json, "loops_used": 1, "tokens_used": 100}
        
        with patch("app.v3.data_trace.record"), \
             patch("app.v3.data_trace.current_span", return_value=None):
            await run_v3_agent(desk=desk, agent_module=junior_analyst)
            
        mock_sdk_run.assert_not_called()
        mock_wait_for.assert_called_once()
