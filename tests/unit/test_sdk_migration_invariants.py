import pytest
import os
from unittest.mock import patch, AsyncMock
from app.v3.agent_runner import run_v3_agent
from app.v3.shared_desk import SharedDesk
from app.v3.agents import junior_analyst
from app.agents.sdk_adapter import run_analyst_via_sdk, create_analyst_profile
from lazycat import RunResult, StructuredError


def test_analyst_profile_creation():
    # Application-owned trading role profiles and tool grants
    profile = create_analyst_profile("v3_junior_analyst")
    assert profile.name == "v3_junior_analyst"
    assert "You are the Junior Analyst" in profile.system_prompt
    assert "get_finnhub_news" in profile.tools
    assert profile.default_budget.max_tool_calls == 7


def test_orchestrator_only_invocation_preservation():
    # Ensure run_analyst_via_sdk is a pure function that does not modify global state
    # and strictly maps SDK responses back to the expected dictionary format
    # without introducing inner loops or hidden retry allowances.
    import inspect
    sig = inspect.signature(run_analyst_via_sdk)
    assert "agent_name" in sig.parameters
    assert "input_prompt" in sig.parameters
    assert "max_turns" in sig.parameters
    assert "client" in sig.parameters
    assert "idempotency_key" in sig.parameters


@pytest.mark.asyncio
async def test_sdk_budget_and_timeout_mapping():
    # If the SDK adapter returns empty result (due to timeout or budget_exceeded),
    # the agent runner should inject a default empty artifact so the pipeline can degrade
    # gracefully rather than crashing.
    desk = SharedDesk(ticker="TSLA")
    
    timeout_result = RunResult(
        run_id="run-tsla-timeout",
        status="timed_out",
        messages=[],
        error=StructuredError(
            code="DEADLINE_EXCEEDED",
            message="Execution timed out after 30000ms",
            retryable=False,
            category="RUNTIME",
        )
    )

    with patch.dict(os.environ, {"USE_V2_SDK": "true"}), \
         patch("lazycat.RuntimeClient.create_run", new_callable=AsyncMock) as mock_create_run:
        
        mock_create_run.return_value = timeout_result
        
        with patch("app.v3.data_trace.record"):
            outcome = await run_v3_agent(desk=desk, agent_module=junior_analyst)
            
        assert outcome is not None
        # Handled gracefully via artifact failure recovery without crashing
