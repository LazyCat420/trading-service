"""The non-agent LLM callers had no retry at all.

MEASURED 2026-09-06, cycle-v3-1788660665. Two of the cycle's six transport
failures were on `prism-proxy/agent?stream=false`, the route every non-agent
caller uses through `call_prism_agent` (memory consolidator, briefings,
retrieval decomposition, the LLM shim):

    03:22:24 Server error '500 Internal Server Error' for url '.../agent?stream=false'
    03:51:27 Server disconnected without sending a response.   (NBIS consolidation)

The SDK's classifier already calls both TRANSIENT (resilience.classify_exception:
RemoteProtocolError, HTTP 5xx, timeouts), and `run_agent` retries them five
times — but `call_prism_agent` wrapped nothing, so one failed socket was the
final answer. Three attempts, exponential backoff, transient only; a 400 is the
model's problem and is not retried.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from app.services.prism_agent_caller import call_prism_agent


class _Budget:
    max_turns = 3


def _ok(text="ok"):
    r = MagicMock()
    r.json.return_value = {"text": text}
    return r


def _http_error(status: int):
    req = httpx.Request("POST", "http://10.0.0.16:5591/prism-proxy/agent?stream=false")
    resp = httpx.Response(status, request=req)
    return httpx.HTTPStatusError(f"Server error '{status}'", request=req, response=resp)


async def _call(side_effects):
    client = MagicMock()
    client.call_agent = AsyncMock(side_effect=side_effects)
    with patch("app.services.prism_agent_caller.prism_client", client), \
         patch("app.services.prism_agent_caller.resolve_default_model_for_agent",
               new=AsyncMock(return_value=("GLM-5.3-Flash-EXL3", "vllm-2"))), \
         patch("app.services.prism_agent_caller.publish_event", lambda *a, **k: None), \
         patch("app.v3.guardrails.get_budget_for_role", return_value=_Budget()), \
         patch("lazycat.resilience.asyncio.sleep", new=AsyncMock()):
        text, tokens, ms = await call_prism_agent(
            agent_id="CUSTOM_CONSOLIDATOR_AGENT", user_message="TICKER: NBIS",
            fallback_system_prompt="sys", fallback_agent_name="memory_consolidator", ticker="NBIS",
        )
    return text, client.call_agent.await_count


@pytest.mark.asyncio
async def test_a_dropped_socket_is_retried_and_the_answer_returned():
    text, n = await _call([httpx.RemoteProtocolError("Server disconnected without sending a response."), _ok("memories")])
    assert text == "memories" and n == 2


@pytest.mark.asyncio
async def test_a_500_is_retried():
    text, n = await _call([_http_error(500), _ok("memories")])
    assert text == "memories" and n == 2


@pytest.mark.asyncio
async def test_a_400_is_not_retried():
    with pytest.raises(httpx.HTTPStatusError):
        await _call([_http_error(400), _ok("never")])


@pytest.mark.asyncio
async def test_three_failures_is_the_end():
    with pytest.raises(Exception):
        await _call([httpx.RemoteProtocolError("x")] * 5)
