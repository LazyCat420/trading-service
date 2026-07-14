"""
Tests for run_agent's output contract (current lazycat-sdk architecture).

run_agent delegates execution to lazycat.agent.AgentHarness; these tests patch
the harness seam and verify the structured result dict that every pipeline
agent depends on.

(The previous version of this file patched app.agents.agent_loop, an
architecture removed in the June 2026 lazycat-sdk migration.)
"""

import pytest
from unittest.mock import patch, AsyncMock

from app.agents.base_agent import run_agent

REQUIRED_KEYS = {
    "agent", "ticker", "cycle_id", "bot_id", "response",
    "tokens_used", "execution_ms", "loops_used", "timestamp",
}


@pytest.fixture
def mock_harness_run():
    """Patch the AgentHarness seam so no Prism/network call happens."""
    with patch(
        "lazycat.agent.AgentHarness.run",
        new_callable=AsyncMock,
        return_value='{"result": "success"}',
    ) as harness_run, patch(
        "app.services.prism_agent_caller.resolve_default_model_for_agent",
        new_callable=AsyncMock,
        return_value=(None, None),
    ):
        yield harness_run


async def _call_run_agent(**overrides):
    kwargs = dict(
        agent_name="v3_junior_analyst",  # not in _OUTCOME_CONTEXT_AGENTS → no DB query
        ticker="_AUDIT_TEST",            # synthetic ticker → outcome context skipped
        cycle_id="cycle-test",
        bot_id="bot-test",
        system_prompt="You are a test agent.",
        user_prompt="Analyze.",
        enable_tools=False,
    )
    kwargs.update(overrides)
    return await run_agent(**kwargs)


@pytest.mark.asyncio
async def test_output_schema_contains_all_required_keys(mock_harness_run):
    result = await _call_run_agent()

    assert isinstance(result, dict)
    assert REQUIRED_KEYS.issubset(result.keys()), (
        f"Missing keys: {REQUIRED_KEYS - set(result.keys())}"
    )
    assert result["agent"] == "v3_junior_analyst"
    assert result["ticker"] == "_AUDIT_TEST"
    assert result["cycle_id"] == "cycle-test"
    assert result["bot_id"] == "bot-test"
    assert result["response"] == '{"result": "success"}'
    assert isinstance(result["execution_ms"], int)
    assert isinstance(result["loops_used"], int)


@pytest.mark.asyncio
async def test_empty_response_produces_failure_marker(mock_harness_run):
    """An empty harness response must surface as an explicit failure string,
    never an empty response field."""
    mock_harness_run.return_value = ""

    result = await _call_run_agent()

    assert result["response"].startswith("Agent failed: empty response")


@pytest.mark.asyncio
async def test_whitespace_response_produces_failure_marker(mock_harness_run):
    mock_harness_run.return_value = "   \n  "

    result = await _call_run_agent()

    assert result["response"].startswith("Agent failed: empty response")


@pytest.mark.asyncio
async def test_timestamp_is_utc_isoformat(mock_harness_run):
    import datetime

    result = await _call_run_agent()

    # Must parse as an aware ISO timestamp
    parsed = datetime.datetime.fromisoformat(result["timestamp"])
    assert parsed.tzinfo is not None
