"""min_p=0.0 must survive the whole way to prism's JSON, on BOTH transports.

`test_min_p_on_local_boxes.py` pins the DECISION (`min_p_for`) and then checks
the wiring by asserting that `inspect.getsource(run_agent)` contains certain
strings. That is a proxy: it passes if the assignment exists and says nothing
about whether the value survives model resolution, the SDK boundary, or the
transport split that landed the same day. These tests follow the value instead.

WHAT PRISM ACTUALLY DOES (read from prism-service, 2026-08-06). `minP` has
`agentDefault: 0.05` in `ParameterRegistry`, and `ChatRoutes.prepare
GenerationContext` applies `getAgentDefaults()` inside:

    if (agent) { ...for each agentDefault: if options[k] == null -> inject... }

The trigger is the `agent` FIELD in the request body, not the endpoint —
`/chat` and `/agent` share that function. The SDK's `/agent` payload always
sets `agent`, which is why every tool-enabled call was getting 0.05 injected
and a speculative-decoding vLLM box answered it with an empty stream after an
HTTP 200. `chat_toolless` omitted `agent`, which is why /chat was measured
10/10 non-empty — protection by omission, from a field nobody would think of
as sampling config. Since 5f42260 routes every tool-less role through /chat,
that accident covers most of the desk, so the field is now sent outright and
these tests hold it there.
"""

import inspect
from unittest.mock import AsyncMock, patch

import pytest

JETSON_MODEL = "cyankiwi/Qwen3.6-35B-A3B-AWQ-4bit"


# ── The /agent transport: run_agent -> BaseAgent -> SDK payload ─────────────
class _CapturedBaseAgent:
    """Stands in for lazycat's BaseAgent and records its construction."""

    captured: dict = {}

    def __init__(self, **kwargs):
        type(self).captured = dict(kwargs)
        self.tools = []
        self.name = kwargs.get("name")

    def add_tool(self, tool):
        self.tools.append(tool)


async def _run_tool_using_agent(model: str, provider: str):
    """Drive run_agent down the /agent branch and return the BaseAgent kwargs."""
    from app.agents.base_agent import run_agent

    _CapturedBaseAgent.captured = {}
    with patch("lazycat.agent.BaseAgent", _CapturedBaseAgent), patch(
        "lazycat.agent.AgentHarness"
    ) as harness_cls, patch(
        "app.agents.tool_whitelists.get_agent_tools",
        return_value=[{"name": "get_market_data"}],
    ), patch(
        "app.services.prism_agent_caller.resolve_default_model_for_agent",
        new_callable=AsyncMock,
        return_value=(model, provider),
    ):
        harness_cls.return_value.run = AsyncMock(return_value='{"ok": true}')
        await run_agent(
            agent_name="v3_junior_analyst",
            ticker="_AUDIT_TEST",
            cycle_id="cycle-test",
            bot_id="bot-test",
            system_prompt="You are a test agent.",
            user_prompt="Analyze.",
            enable_tools=True,
        )
    return _CapturedBaseAgent.captured


@pytest.mark.asyncio
async def test_a_local_box_call_constructs_base_agent_with_min_p_zero():
    """The value reaches the constructor — not merely a line of source."""
    kwargs = await _run_tool_using_agent(JETSON_MODEL, "vllm")

    assert kwargs.get("min_p") == 0.0
    assert kwargs["model"] == JETSON_MODEL


@pytest.mark.asyncio
async def test_a_cloud_model_is_left_alone_end_to_end():
    """0.0 is vLLM's own default; imposing it on a cloud provider is a change
    we have no measurement for."""
    kwargs = await _run_tool_using_agent("claude-sonnet-5", "anthropic")

    assert "min_p" not in kwargs


class TestTheSdkPutsItOnTheWire:
    """The repo depends on an SDK it does not build. This pins that boundary.

    `_BASE_AGENT_ACCEPTS_MIN_P` degrades to a warning when the installed SDK
    predates 0.3.10, which is correct for a partial deploy but means a
    downgrade would silently restore the broken behaviour — the warning is one
    line in a log nobody reads during an outage.
    """

    def test_min_p_zero_becomes_min_p_in_the_agent_payload(self):
        from lazycat.llm import PrismClient

        payload, _url, _headers = PrismClient().get_stream_payload_and_url(
            model=JETSON_MODEL, messages=[{"role": "user", "content": "hi"}],
            max_tokens=4096, temperature=0.3, system_prompt="s",
            agent_name="v3_junior_analyst", conversation_id="", session_id="",
            project="p", username="u", is_new=True, enable_thinking=False,
            min_p=0.0,
        )

        assert payload["minP"] == 0.0

    def test_omitting_it_is_what_the_bug_looked_like(self):
        """The known-bad shape, kept so 'it works now' stays falsifiable: no
        minP key at all is what prism fills in with 0.05."""
        from lazycat.llm import PrismClient

        payload, _url, _headers = PrismClient().get_stream_payload_and_url(
            model=JETSON_MODEL, messages=[{"role": "user", "content": "hi"}],
            max_tokens=4096, temperature=0.3, system_prompt="s",
            agent_name="v3_junior_analyst", conversation_id="", session_id="",
            project="p", username="u", is_new=True, enable_thinking=False,
            min_p=None,
        )

        assert "minP" not in payload

    def test_the_agent_payload_carries_the_field_that_triggers_injection(self):
        """Anchors the mechanism: `agent` is why /agent gets 0.05 and /chat
        does not. If the SDK ever stopped sending it, the reasoning in
        `min_p_for` would no longer describe reality."""
        from lazycat.llm import PrismClient

        payload, _url, _headers = PrismClient().get_stream_payload_and_url(
            model=JETSON_MODEL, messages=[{"role": "user", "content": "hi"}],
            max_tokens=4096, temperature=0.3, system_prompt="s",
            agent_name="v3_junior_analyst", conversation_id="", session_id="",
            project="p", username="u", is_new=True, enable_thinking=False,
        )

        assert payload.get("agent") == "v3_junior_analyst"


# ── The /chat transport, which the reroute made the majority path ──────────
class TestChatToollessProtectsItselfExplicitly:
    def test_a_local_box_chat_call_sends_min_p_zero(self):
        src = inspect.getsource(
            __import__("app.services.prism_agent_caller", fromlist=["x"]).chat_toolless
        )
        assert "min_p_for(provider, model)" in src, (
            "the /chat payload must derive minP from the same decision function "
            "as /agent, not from a second copy of the rule"
        )

    @pytest.mark.asyncio
    async def test_the_payload_sent_to_prism_carries_min_p(self):
        """Captured off the real httpx call rather than read from source."""
        payload = await _capture_chat_payload(provider="vllm", model=JETSON_MODEL)

        assert payload["minP"] == 0.0

    @pytest.mark.asyncio
    async def test_a_cloud_model_on_chat_is_left_alone(self):
        payload = await _capture_chat_payload(
            provider="anthropic", model="claude-sonnet-5"
        )

        assert "minP" not in payload

    @pytest.mark.asyncio
    async def test_the_chat_payload_still_omits_the_agent_field(self):
        """Belt and braces: minP is now explicit, so `agent` would no longer
        break sampling — but it would also pull in every OTHER agentDefault
        (temperature, maxTokens, reasoningEffort, thinkingEnabled=true) on a
        path that deliberately sets thinkingEnabled=False."""
        payload = await _capture_chat_payload(provider="vllm", model=JETSON_MODEL)

        assert "agent" not in payload
        assert payload["thinkingEnabled"] is False


class _FakeStream:
    """Minimal async context manager quacking like httpx's stream response."""

    def __init__(self):
        self.status_code = 200

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def raise_for_status(self):
        return None

    async def aiter_lines(self):
        yield 'data: {"type": "chunk", "content": "{\\"ok\\": true}"}'
        yield 'data: {"type": "done", "model": "m", "provider": "vllm", "usage": {"inputTokens": 10, "outputTokens": 5}}'


async def _capture_chat_payload(*, provider: str, model: str) -> dict:
    from app.services.prism_agent_caller import chat_toolless

    seen: dict = {}

    class _FakeClient:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        def stream(self, _method, _url, json=None, **_kw):
            seen.update(json or {})
            return _FakeStream()

    with patch("httpx.AsyncClient", _FakeClient):
        await chat_toolless(
            provider=provider, model=model,
            system_prompt="s", user_prompt="u",
            max_tokens=4096, timeout_seconds=5.0,
        )
    return seen


@pytest.mark.asyncio
async def test_chat_toolless_reports_its_own_elapsed_time():
    """The gatekeeper's shadow rows record `primary_elapsed_ms` from this key.

    It was absent until 2026-08-06, so every gatekeeper shadow row would have
    booked the primary at 0ms — the primary reading as instant next to the box
    it is being compared against.
    """
    from app.services.prism_agent_caller import chat_toolless

    class _FakeClient:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        def stream(self, *a, **kw):
            return _FakeStream()

    with patch("httpx.AsyncClient", _FakeClient):
        out = await chat_toolless(
            provider="vllm", model=JETSON_MODEL, system_prompt="s",
            user_prompt="u", max_tokens=4096, timeout_seconds=5.0,
        )

    assert "execution_ms" in out
    assert isinstance(out["execution_ms"], int)
