import pytest
from unittest.mock import AsyncMock
from app.agents.sdk_adapter import run_analyst_via_sdk
from lazycat import RuntimeClient, RunResult, RunUsage


@pytest.mark.asyncio
async def test_sdk_usage_accounting_parity():
    """Gate 6: Usage accounting parity asserts prompt, completion, and total tokens are recorded faithfully."""
    mock_client = AsyncMock(spec=RuntimeClient)
    mock_client.create_run.return_value = RunResult(
        run_id="run-usage-test",
        status="completed",
        messages=[{"role": "assistant", "content": '{"summary": "Test"}'}],
        usage=RunUsage(
            prompt_tokens=250,
            completion_tokens=75,
            total_tokens=325,
            tool_calls_count=2,
        ),
    )

    outcome = await run_analyst_via_sdk(
        agent_name="v3_junior_analyst",
        input_prompt="Test prompt",
        client=mock_client,
    )

    assert outcome["tokens_used"] == 325
    assert outcome["prompt_tokens"] == 250
    assert outcome["completion_tokens"] == 75
    assert outcome["loops_used"] == 2
    assert outcome["usage_requests"] == 2
    assert outcome["stop_reason"] == "completed"
