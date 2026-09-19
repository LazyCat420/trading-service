import pytest
from unittest.mock import AsyncMock
from app.agents.sdk_adapter import run_analyst_via_sdk, create_analyst_profile
from app.v3.agents.junior_analyst import TOOL_WHITELIST
from lazycat import RuntimeClient, RunResult, CreateRunRequest


@pytest.mark.asyncio
async def test_sdk_tool_policy_enforcement():
    """Gate 3: Tool policy gate ensures only whitelisted tools are passed to the runtime."""
    profile = create_analyst_profile("v3_junior_analyst")
    
    # Whitelist must only contain explicitly allowed tools
    assert set(profile.tools) == set(TOOL_WHITELIST)
    assert "execute_trade" not in profile.tools
    assert "transfer_funds" not in profile.tools

    mock_client = AsyncMock(spec=RuntimeClient)
    mock_client.create_run.return_value = RunResult(
        run_id="run-policy-test",
        status="completed",
        messages=[{"role": "assistant", "content": '{"summary": "Test"}'}],
    )

    await run_analyst_via_sdk(
        agent_name="v3_junior_analyst",
        input_prompt="Test prompt",
        client=mock_client,
    )

    call_req: CreateRunRequest = mock_client.create_run.call_args[0][0]
    tool_names = [t["name"] for t in call_req.tools]
    
    # Assert every tool in request is within TOOL_WHITELIST
    for t in tool_names:
        assert t in TOOL_WHITELIST, f"Tool '{t}' is outside approved whitelist!"
