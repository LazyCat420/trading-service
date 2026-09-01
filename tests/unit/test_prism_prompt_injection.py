"""call_prism_agent's message array: the system prompt is inlined, not assumed.

TWO REASONS THIS FILE WAS RED FOR WEEKS, both fixed here.

1. It was a "unit" test that made a LIVE NETWORK CALL. `call_prism_agent`
   resolves its model through `resolve_default_model_for_agent` ->
   `get_live_model_from_vllm(http://10.0.0.16:5591/vllm-shim/gold-spark)`, and
   the test mocked only `prism_client`. Whenever that box was down the test
   failed with `ModelUnavailableError: VLLM endpoint offline` — nothing to do
   with prompts. It sat in the documented "pre-existing failures" list, which
   is where a test goes to stop being read.

2. It asserted the PRE-bed708d message shape (`messages[1]["role"] == "user"`).
   bed708d changed that interleaved turn to `assistant`, and the test was never
   updated — so even reachable, it was asserting history.

Both are the same underlying mistake: the test was pinned to the environment
and to a moment, rather than to the behaviour. It now mocks the model
resolution and states what the array is FOR.

On the roles: bed708d's commit message claimed the [system, user, user] shape
was breaking DeepSeek's chat template. It was not — prism inserts its own
system turn between them and the vllm-shim demotes every non-leading system
message to `user`, so the model never saw the client's shape either way, and
the real cause was the injected minP (see
test_min_p_on_the_call_prism_agent_path.py). The assistant turn is kept
because it is harmless and shipped; this test pins what IS, and says why, so
the next reader does not re-derive the wrong lesson from it.
"""

from unittest.mock import AsyncMock, patch

import pytest

from app.services.prism_agent_caller import call_prism_agent

GOLD_SPARK_MODEL = "deepseek-v4-flash-0731"


class _Budget:
    max_turns = 3


async def _call_and_capture(**overrides) -> dict:
    """Drive call_prism_agent with the network stubbed out."""
    resp = AsyncMock()
    resp.json = lambda: {"text": '{"selected_tickers": ["AAPL"]}',
                         "usage": {"inputTokens": 10, "outputTokens": 5}}

    with patch("app.services.prism_agent_caller.prism_client") as client, \
         patch("app.services.prism_agent_caller.resolve_default_model_for_agent",
               new_callable=AsyncMock, return_value=(GOLD_SPARK_MODEL, "vllm-2")), \
         patch("app.services.prism_agent_caller.publish_event", lambda *a, **k: None), \
         patch("app.v3.guardrails.get_budget_for_role", return_value=_Budget()):
        client.call_agent = AsyncMock(return_value=resp)

        kwargs = dict(
            agent_id="CUSTOM_V3_PORTFOLIO_MANAGER",
            user_message="Here is the watchlist table...",
            fallback_system_prompt="Select tickers based on news",
            fallback_agent_name="v3_portfolio_manager",
            temperature=0.1,
            max_tokens=1024,
        )
        kwargs.update(overrides)
        await call_prism_agent(**kwargs)

        client.call_agent.assert_called_once()
        return client.call_agent.call_args.kwargs


@pytest.mark.asyncio
async def test_the_system_prompt_is_inlined_as_the_leading_message():
    """Prism reads `systemPrompt` too, but the leading system message is what
    the OpenAI-compatible providers actually consume."""
    kwargs = await _call_and_capture()
    messages = kwargs["messages"]

    assert messages[0]["role"] == "system"
    assert "Select tickers based on news" in messages[0]["content"]


@pytest.mark.asyncio
async def test_the_user_message_is_the_last_turn_and_is_unmodified():
    """The one assertion that matters to every caller."""
    kwargs = await _call_and_capture()
    messages = kwargs["messages"]

    assert messages[-1]["role"] == "user"
    assert messages[-1]["content"] == "Here is the watchlist table..."


@pytest.mark.asyncio
async def test_the_interleaved_acknowledgement_turn_is_an_assistant_turn():
    """What bed708d shipped. See the module docstring on why it is not the fix
    its commit message claimed to be."""
    kwargs = await _call_and_capture()
    messages = kwargs["messages"]

    assert len(messages) == 3
    assert messages[1]["role"] == "assistant"
    assert "ready to process" in messages[1]["content"]


@pytest.mark.asyncio
async def test_the_call_carries_min_p_for_a_local_box():
    """The actual cause of the empty responses, pinned on this path too."""
    kwargs = await _call_and_capture()

    assert kwargs.get("min_p") == 0.0


@pytest.mark.asyncio
async def test_no_network_is_touched():
    """The regression that mattered: this must pass with every box offline."""
    with patch("app.services.prism_agent_caller.get_live_model_from_vllm",
               side_effect=AssertionError("resolved a model over the network")):
        kwargs = await _call_and_capture()

    assert kwargs["messages"]
