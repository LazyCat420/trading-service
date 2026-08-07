"""The benchmark's arms must be the desk's requests, and its control must stay broken.

`scripts/jetson_benchmark.py` is where the transport decision was measured
(n=10, interleaved) and where the Jetson's fitness will be re-measured before
anything routes to it. Both uses assume the arms send what production sends.
Nothing enforced that: the arms are hand-built payload dicts sitting next to,
but not derived from, the production callers.

Two ways that goes wrong, both of which produce a confident wrong number:

  * DRIFT. Production gains a field (2026-08-06: `minP` on the /chat path) and
    the arm does not. The benchmark then measures a request the desk never
    makes, and the more careful the sampling, the more convincing the wrong
    answer.
  * A REPAIRED CONTROL. `agent-nominp` exists to reproduce the pre-fix failure
    on demand — EMPTY_RESPONSE in 1,539ms with 0 chars. If a future cleanup
    "fixes" it by sending minP, every arm passes and "the Jetson works now"
    becomes unfalsifiable.
"""

import inspect

import pytest

from scripts import jetson_benchmark as bench

JETSON_MODEL = "cyankiwi/Qwen3.6-35B-A3B-AWQ-4bit"
ITEM = {
    "system_prompt": "You are the regime engine.",
    "user_prompt": "Classify the regime.",
    "agent_name": "v3_regime_engine",
}


async def _payload_from(call, **kw) -> dict:
    """Run an arm against a stub `_stream` and return the payload it built."""
    seen: dict = {}

    async def _capture(path, payload, arm, timeout):
        seen.update({"__path": path, "__arm": arm, **payload})
        return bench.CallResult(arm=arm)

    original = bench._stream
    bench._stream = _capture
    try:
        await call(**kw)
    finally:
        bench._stream = original
    return seen


class TestTheChatArmIsTheProductionRequest:
    """`chat_toolless` is what every tool-less role goes through since
    5f42260, so this arm is the one that speaks for the desk."""

    @pytest.mark.asyncio
    async def test_it_sends_the_same_fields(self):
        payload = await _payload_from(
            bench.call_chat, model=JETSON_MODEL, provider="vllm",
            item=ITEM, timeout=30.0,
        )
        src = inspect.getsource(
            __import__("app.services.prism_agent_caller", fromlist=["x"]).chat_toolless
        )
        # The production payload literal, read as the set of keys it sets.
        production_keys = {
            k for k in (
                "model", "provider", "project", "systemPrompt", "messages",
                "maxTokens", "thinkingEnabled", "minP",
            ) if f'"{k}"' in src
        }
        arm_keys = {k for k in payload if not k.startswith("__")}

        assert production_keys <= arm_keys, (
            f"the chat arm is missing production fields: {production_keys - arm_keys}"
        )

    @pytest.mark.asyncio
    async def test_it_carries_min_p_for_a_local_box(self):
        payload = await _payload_from(
            bench.call_chat, model=JETSON_MODEL, provider="vllm",
            item=ITEM, timeout=30.0,
        )

        assert payload["minP"] == 0.0

    @pytest.mark.asyncio
    async def test_it_derives_min_p_rather_than_hardcoding_it(self):
        """A second copy of the rule drifts from the first one silently."""
        assert "min_p_for(" in inspect.getsource(bench.call_chat)

    @pytest.mark.asyncio
    async def test_it_still_omits_the_agent_field(self):
        """Sending `agent` is what makes prism apply its agentDefaults — the
        arm would stop resembling production the moment it did."""
        payload = await _payload_from(
            bench.call_chat, model=JETSON_MODEL, provider="vllm",
            item=ITEM, timeout=30.0,
        )

        assert "agent" not in payload
        assert payload["__path"] == "/chat"


class TestTheAgentArmMirrorsTheSdk:
    @pytest.mark.asyncio
    async def test_it_sends_min_p_zero_by_default(self):
        payload = await _payload_from(
            bench.call_agent, model=JETSON_MODEL, provider="vllm",
            item=ITEM, timeout=30.0,
        )

        assert payload["minP"] == 0.0
        assert payload["__path"].startswith("/agent")

    @pytest.mark.asyncio
    async def test_it_sends_the_agent_field_like_the_sdk_does(self):
        """This field is the difference between the two transports as far as
        sampling defaults are concerned; without it the arm would silently be
        a /chat-equivalent wearing the /agent URL."""
        payload = await _payload_from(
            bench.call_agent, model=JETSON_MODEL, provider="vllm",
            item=ITEM, timeout=30.0,
        )

        assert payload.get("agent")

    @pytest.mark.asyncio
    async def test_the_tools_arm_declares_tools(self):
        payload = await _payload_from(
            bench.call_agent, model=JETSON_MODEL, provider="vllm", item=ITEM,
            timeout=30.0, tools=["get_market_data"],
        )

        assert payload["enabledTools"] == ["get_market_data"]
        assert payload["__arm"] == "agent+tools"


class TestTheKnownBadArmStaysBroken:
    """The one arm that must keep failing."""

    @pytest.mark.asyncio
    async def test_nominp_omits_the_field_entirely(self):
        """Omission is the failure — prism fills the gap with 0.05. Sending
        0.05 explicitly would be a different request that happens to fail the
        same way today."""
        payload = await _payload_from(
            bench.call_agent, model=JETSON_MODEL, provider="vllm", item=ITEM,
            timeout=30.0, min_p=None,
        )

        assert "minP" not in payload

    def test_the_reliability_phase_still_runs_it(self):
        src = inspect.getsource(bench.phase_reliability)
        assert "agent-nominp" in src

    def test_an_empty_arm_result_is_never_a_pass(self):
        """Fail-closed classification is what keeps the control meaningful:
        the pre-fix failure is an HTTP 200 with no content."""
        res = bench.CallResult(arm="agent-nominp", chars=0)
        outcome, ok, valid = bench._classify("", res)

        assert outcome == "EMPTY_RESPONSE"
        assert not ok and not valid
