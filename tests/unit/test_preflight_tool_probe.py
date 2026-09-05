"""A box can be alive, answering, and unable to execute anything.

`llm_can_answer` asks whether the model ANSWERS. On 2026-09-04/05 the Gold
Spark answered every time — it was serving DeepSeek V4 through SGLang launched
with no `--tool-call-parser deepseekv4`, so every tool call came back as DSML
markup in the message content. `cycle-v3-1788565070` therefore passed
pre-flight and ran to completion: 117 agent runs at one loop each, zero rows in
`agent_tool_telemetry`, twelve HOLD decisions written from the pre-collected
briefing alone.

WHY `tool_choice: "auto"` AND NOT `"required"`. Measured against the live box
on 2026-09-05, the same model on the same server:

    required -> content '[{"name":"preflight_echo","parameters":{"word":"OK"}}]'
                tool_calls empty, NO markup       -> probe says "inconclusive"
    auto     -> content '<|DSML|tool_calls><|DSML|invoke name="preflight_echo">…'
                tool_calls empty                  -> probe FAILS CLOSED

`required` makes the server constrain the output grammar, so the shape the
probe looks for never appears and it returns a false green against the exact
box it exists to catch. `auto` is also what every real agent call sends. The
fixtures below are those two real bodies.

Live controls, 2026-09-05: `dgx_spark` (SGLang, no parser) -> ok=False;
`jetson` (vLLM, nemotron35) -> ok=True.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest

from app.services import llm_preflight


class _Resp:
    def __init__(self, payload, status=200):
        self.status_code = status
        self._payload = payload
        self.text = json.dumps(payload)

    def json(self):
        return self._payload


def _body(content=None, tool_calls=None):
    msg = {"role": "assistant", "content": content}
    if tool_calls:
        msg["tool_calls"] = tool_calls
    return {"choices": [{"message": msg, "finish_reason": "stop"}]}


# The real bodies, 2026-09-05.
DSML_CONTENT = (
    "\n\n<｜DSML｜tool_calls>\n<｜DSML｜invoke name=\"preflight_echo\">\n"
    "<｜DSML｜parameter name=\"word\" string=\"true\">OK</｜DSML｜parameter>\n"
    "</｜DSML｜invoke>\n</｜DSML｜tool_calls>"
)
PARSED_TOOL_CALLS = [
    {"id": "call_1", "type": "function",
     "function": {"name": "preflight_echo", "arguments": '{"word": "OK"}'}}
]


@pytest.fixture
def endpoint(monkeypatch):
    class _EP:
        url = "http://shim/vllm-shim/gold-spark"

    monkeypatch.setattr(llm_preflight, "PROBE_AGENT_NAME", "v3_decision_synthesizer")
    from app.services import prism_agent_caller

    monkeypatch.setattr(prism_agent_caller.llm, "_endpoints", {"dgx_spark": _EP()})
    monkeypatch.setattr(
        prism_agent_caller, "resolve_default_model_for_agent",
        AsyncMock(return_value=("deepseek-v4-flash-vision-exp-sglang", "vllm-2")))
    return _EP


def _post(payload, status=200):
    client = AsyncMock()
    client.post = AsyncMock(return_value=_Resp(payload, status))
    ctx = AsyncMock()
    ctx.__aenter__ = AsyncMock(return_value=client)
    ctx.__aexit__ = AsyncMock(return_value=False)
    return patch("httpx.AsyncClient", return_value=ctx), client


class TestItFailsClosedOnProof:
    @pytest.mark.asyncio
    async def test_markup_in_the_content_aborts_the_cycle(self, endpoint):
        p, _ = _post(_body(content=DSML_CONTENT))
        with p:
            ok, detail = await llm_preflight.tool_calls_are_parsed("dgx_spark")
        assert ok is False
        assert "TEXT" in detail

    @pytest.mark.asyncio
    async def test_it_sends_the_tool_choice_production_sends(self, endpoint):
        """`required` returned a false green against the real broken box."""
        p, client = _post(_body(content=DSML_CONTENT))
        with p:
            await llm_preflight.tool_calls_are_parsed("dgx_spark")
        sent = client.post.call_args.kwargs["json"]
        assert sent["tool_choice"] == "auto"
        assert len(sent["tools"]) == 1

    @pytest.mark.asyncio
    async def test_the_grammar_constrained_body_is_not_what_we_ask_for(self):
        """The `required` body carries no markup — which is why we do not use
        it. Asserted so nobody "hardens" the probe by switching back."""
        from app.utils.text_utils import _UNPARSED_TOOL_CALL_RE

        constrained = '[{"name":"preflight_echo","parameters":{"word":"OK"}}]'
        assert _UNPARSED_TOOL_CALL_RE.search(constrained) is None


class TestItPassesAWorkingBox:
    @pytest.mark.asyncio
    async def test_parsed_tool_calls_are_a_pass(self, endpoint):
        p, _ = _post(_body(content=None, tool_calls=PARSED_TOOL_CALLS))
        with p:
            ok, detail = await llm_preflight.tool_calls_are_parsed("dgx_spark")
        assert ok is True
        assert "parses tool calls" in detail


class TestItFailsOpenOnAmbiguity:
    """The module's doctrine: a broken probe must not become the thing that
    blocks all trading."""

    @pytest.mark.asyncio
    async def test_a_non_200_proceeds(self, endpoint):
        p, _ = _post({"error": "boom"}, status=503)
        with p:
            ok, detail = await llm_preflight.tool_calls_are_parsed("dgx_spark")
        assert ok is True and "probe-skipped" in detail

    @pytest.mark.asyncio
    async def test_a_transport_error_proceeds(self, endpoint):
        with patch("httpx.AsyncClient", side_effect=OSError("no route")):
            ok, detail = await llm_preflight.tool_calls_are_parsed("dgx_spark")
        assert ok is True and "probe-skipped" in detail

    @pytest.mark.asyncio
    async def test_a_model_that_simply_declines_is_inconclusive_not_fatal(
        self, endpoint
    ):
        """A model ignoring the tool is not a server that cannot parse."""
        p, _ = _post(_body(content="I don't think a tool is needed here."))
        with p:
            ok, detail = await llm_preflight.tool_calls_are_parsed("dgx_spark")
        assert ok is True and "inconclusive" in detail

    @pytest.mark.asyncio
    async def test_an_unconfigured_endpoint_proceeds(self, endpoint, monkeypatch):
        from app.services import prism_agent_caller

        monkeypatch.setattr(prism_agent_caller.llm, "_endpoints", {})
        ok, detail = await llm_preflight.tool_calls_are_parsed("dgx_spark")
        assert ok is True and "probe-skipped" in detail


class TestItIsWiredIntoTheCycle:
    def test_the_pipeline_runs_it_before_any_agent(self):
        """The pre-flight block lives in `_run_all_v3`, not `start_cycle` — the
        probe has to sit where the agents are about to run."""
        import inspect

        from app.services.pipeline_service import PipelineService

        src = inspect.getsource(PipelineService._run_all_v3)
        assert "tool_calls_are_parsed" in src
        assert src.index("llm_can_answer()") < src.index("tool_calls_are_parsed()")

    def test_a_failed_tool_probe_reaches_the_abort_path(self):
        """Both probes must feed the same abort, or the new verdict is
        computed and dropped."""
        import inspect

        from app.services.pipeline_service import PipelineService

        src = inspect.getsource(PipelineService._run_all_v3)
        probe = src.index("tool_calls_are_parsed()")
        abort = src.index("if not _llm_ok:", probe)
        assert abort > probe
        assert "LLM_PREFLIGHT_FAILED" in src[abort:abort + 1200]
