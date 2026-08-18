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


class _Reply:
    """One stand-in for BOTH transports.

    2026-08-06: run_agent now derives its transport from the agent's tool
    declaration (`base_agent.transport_for`), so a tool-less call no longer
    reaches AgentHarness at all — it goes to prism's /chat. Patching only the
    harness left these tests making a REAL network call, which is how the
    reroute was caught. The contract is transport-independent, so the fixture
    drives both seams from one value and the tests below run against each.
    """

    def __init__(self, text: str = '{"result": "success"}'):
        self.text = text

    @property
    def harness_return(self) -> str:
        return self.text

    @property
    def chat_return(self) -> dict:
        return {
            "response": self.text,
            "tokens_used": 42,
            "loops_used": 1,
            "model_used": "test-model",
            "provider": "vllm",
        }


@pytest.fixture
def mock_harness_run():
    """Patch BOTH transport seams so no Prism/network call happens.

    Returns an object whose `.return_value` setter updates both, so existing
    tests that assign to it keep working across the transport split.
    """
    reply = _Reply()

    with patch(
        "lazycat.agent.AgentHarness.run",
        new_callable=AsyncMock,
    ) as harness_run, patch(
        "app.services.prism_agent_caller.chat_toolless",
        new_callable=AsyncMock,
    ) as chat_call, patch(
        "app.services.prism_agent_caller.resolve_default_model_for_agent",
        new_callable=AsyncMock,
        return_value=(None, None),
    ):
        class _BothSeams:
            @property
            def return_value(self):
                return reply.text

            @return_value.setter
            def return_value(self, text):
                reply.text = text
                harness_run.return_value = reply.harness_return
                chat_call.return_value = reply.chat_return

        both = _BothSeams()
        both.return_value = reply.text  # prime both mocks
        yield both


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


@pytest.mark.asyncio
async def test_the_contract_holds_on_the_tool_using_transport(mock_harness_run):
    """The same dict shape must come back when tools route it to /agent.

    Without this, the whole file would only ever exercise /chat (every case
    above passes enable_tools=False), and a contract break on the tool-using
    path — the one that carries every research agent — would ship unseen.
    """
    # Imported inside run_agent, so patch it at its source module.
    with patch(
        "app.agents.tool_whitelists.get_agent_tools",
        return_value=[{"name": "get_market_data"}],
    ):
        result = await _call_run_agent(enable_tools=True)

    assert REQUIRED_KEYS.issubset(result.keys())
    assert result["response"] == '{"result": "success"}'


@pytest.mark.asyncio
async def test_tool_less_calls_do_not_reach_the_agent_harness(mock_harness_run):
    """Pins the reroute itself, not just that a dict came back.

    An assertion on the response alone passes whichever transport ran, so it
    could not tell whether the routing change took effect at all.
    """
    with patch("lazycat.agent.AgentHarness.run", new_callable=AsyncMock) as harness:
        with patch(
            "app.services.prism_agent_caller.chat_toolless",
            new_callable=AsyncMock,
            return_value={
                "response": '{"ok": true}', "tokens_used": 1,
                "loops_used": 1, "model_used": "m", "provider": "vllm",
            },
        ) as chat:
            result = await _call_run_agent(enable_tools=False)

    assert chat.await_count == 1, "a tool-less agent must go to /chat"
    assert harness.await_count == 0, "and must NOT pay for the /agent catalog"
    assert result["response"] == '{"ok": true}'
    assert result["loops_used"] == 1
