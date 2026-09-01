"""The THIRD transport also needs min_p — `call_prism_agent`, not just the two the old tests knew about.

WHAT THE EXISTING TESTS COVER, AND THE HOLE BETWEEN THEM.
`test_min_p_on_local_boxes.py` pins the decision (`min_p_for`) and asserts
`run_agent` wires it into `BaseAgent`. `test_min_p_reaches_the_wire.py`
follows the value down two transports: `run_agent` -> BaseAgent -> SDK, and
`chat_toolless` -> httpx. Both are thorough. Neither knows that
`prism_agent_caller.call_prism_agent` is a THIRD way into prism's `/agent`,
with four `prism_client.call_agent(...)` sites of its own — and every one of
them omitted `min_p`, so prism kept injecting `minP: 0.05`.

That is the enumeration drifting from the surface: the tests named the
transports they knew, the code grew another, and a green suite said the rule
held everywhere. `TestNoCallSiteCanOptOut` below closes it by parsing the
module instead of listing the sites.

THE FAILURE THIS CAUSES. vLLM under speculative decoding refuses any
min_p > 0 ("The min_p and logit_bias sampling parameters are not yet supported
with speculative decoding"). Non-streaming that is an HTTP 400; on prism's
streaming path it arrives as an in-band SSE error frame AFTER the 200 header,
which prism skips — so the caller gets a *successful* call with empty text,
and prism's empty-output recovery then retries with the temperature RAISED,
away from the only setting that works.

MEASURED. Prism's request ledger, `call_prism_agent` callers at temperature>0
on deepseek-v4-flash-0731: 477/477 empty from 2026-08-26 through 2026-09-01,
0 successes. The same callers at temperature==0: all succeeded (vLLM zeroes
min_p under greedy sampling, which is what hid the bug). Re-probed 2026-09-01
against the Jetson (`cyankiwi/Qwen3.6-35B-A3B-AWQ-4bit`, 1.85M spec-decode
drafts): temp=0.3 + min_p=0.05 -> HTTP 400; min_p=0 -> HTTP 200 with content.
Both local boxes run speculative decoding, so this is not one box's quirk.

Cost of the omission: memory consolidation, autoresearch reflection, the
evolution auditors and the morning/flash briefings returned nothing for six
days while every cycle reported success.
"""

import ast
import pathlib
from unittest.mock import AsyncMock, patch

import pytest

JETSON_MODEL = "cyankiwi/Qwen3.6-35B-A3B-AWQ-4bit"
GOLD_SPARK_MODEL = "deepseek-v4-flash-0731"

APP_ROOT = pathlib.Path(__file__).resolve().parents[2] / "app"


class TestTheDecision:
    """`min_p_kwargs` is the spread-into-kwargs form of the same rule."""

    def test_gold_spark_gets_zero(self):
        from app.services.prism_agent_caller import min_p_kwargs

        assert min_p_kwargs("vllm-2", GOLD_SPARK_MODEL) == {"min_p": 0.0}

    def test_jetson_gets_zero(self):
        from app.services.prism_agent_caller import min_p_kwargs

        assert min_p_kwargs("vllm", JETSON_MODEL) == {"min_p": 0.0}

    def test_a_cloud_model_is_left_alone(self):
        """0.0 is vLLM's own default; imposing it elsewhere is an unmeasured change."""
        from app.services.prism_agent_caller import min_p_kwargs

        assert min_p_kwargs("anthropic", "claude-sonnet-5") == {}

    def test_it_defers_to_min_p_for_rather_than_copying_the_rule(self):
        """A second copy of the rule is a second thing to forget to update."""
        import inspect

        from app.services.prism_agent_caller import min_p_kwargs

        assert "min_p_for(" in inspect.getsource(min_p_kwargs)

    def test_installed_sdk_supports_it(self):
        """A partial deploy degrades to a warning; this catches the drift early."""
        from app.services import prism_agent_caller as mod

        assert mod._PRISM_CLIENT_ACCEPTS_MIN_P, (
            "installed lazycat SDK has no min_p on PrismClient.call_agent — sync lazycat-sdk"
        )


# ── The value, followed to the SDK boundary on the real function ───────────
class _FakeResp:
    def __init__(self, text="{\"ok\": true}"):
        self._text = text

    def json(self):
        return {"text": self._text, "usage": {"inputTokens": 11, "outputTokens": 7}}


class _CapturingPrismClient:
    """Records the kwargs of every call_agent invocation."""

    def __init__(self):
        self.calls: list[dict] = []

    async def call_agent(self, **kwargs):
        self.calls.append(dict(kwargs))
        return _FakeResp()


async def _drive_call_prism_agent(model: str, provider: str) -> dict:
    from app.services import prism_agent_caller as mod

    fake = _CapturingPrismClient()

    class _Budget:
        max_turns = 3

    with patch.object(mod, "prism_client", fake), patch.object(
        mod, "resolve_default_model_for_agent", new_callable=AsyncMock,
        return_value=(model, provider),
    ), patch.object(mod, "publish_event", lambda *a, **k: None), patch(
        "app.v3.guardrails.get_budget_for_role", return_value=_Budget()
    ):
        await mod.call_prism_agent(
            agent_id="CUSTOM_META_AUDIT_AGENT",
            user_message="Evaluate this cycle slice.",
            fallback_system_prompt="You are an auditor.",
            fallback_agent_name="auditor_1",
            temperature=0.3,
            max_tokens=8192,
        )

    assert fake.calls, "call_prism_agent never reached prism_client.call_agent"
    return fake.calls[0]


class TestTheValueReachesTheSdk:
    @pytest.mark.asyncio
    async def test_gold_spark_call_carries_min_p_zero(self):
        """The assertion that fails on the pre-fix code."""
        kwargs = await _drive_call_prism_agent(GOLD_SPARK_MODEL, "vllm-2")

        assert kwargs.get("min_p") == 0.0

    @pytest.mark.asyncio
    async def test_jetson_call_carries_min_p_zero(self):
        kwargs = await _drive_call_prism_agent(JETSON_MODEL, "vllm")

        assert kwargs.get("min_p") == 0.0

    @pytest.mark.asyncio
    async def test_a_cloud_model_is_left_alone_end_to_end(self):
        kwargs = await _drive_call_prism_agent("claude-sonnet-5", "anthropic")

        assert "min_p" not in kwargs

    @pytest.mark.asyncio
    async def test_the_temperature_that_hid_the_bug_is_not_the_protection(self):
        """temp==0 masks the refusal (vLLM zeroes min_p under greedy sampling).

        The desk's callers run at 0.1-0.9, so the fix must hold at temp>0 —
        pinned here so nobody 'fixes' this by forcing greedy sampling.
        """
        kwargs = await _drive_call_prism_agent(GOLD_SPARK_MODEL, "vllm-2")

        assert kwargs.get("temperature", 0) > 0
        assert kwargs.get("min_p") == 0.0


# ── The guard that would have caught the original omission ─────────────────
def _call_agent_sites(path: pathlib.Path):
    """Every `<something>.call_agent(...)` call in a file, with its keywords."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr == "call_agent":
            yield node


class TestNoCallSiteCanOptOut:
    """Parse the tree; never list the transports.

    The previous suite enumerated the call paths it knew about, so a new one
    inherited no coverage. This asserts the property over whatever the code
    actually contains today.
    """

    def test_there_is_at_least_one_site_to_check(self):
        """A guard that finds nothing passes for the wrong reason."""
        sites = [s for f in APP_ROOT.rglob("*.py") for s in _call_agent_sites(f)]

        assert len(sites) >= 4, f"expected the known call_agent sites, found {len(sites)}"

    def test_every_call_agent_site_spreads_min_p_kwargs(self):
        offenders = []
        for f in sorted(APP_ROOT.rglob("*.py")):
            for node in _call_agent_sites(f):
                spreads = [
                    k for k in node.keywords
                    if k.arg is None
                    and isinstance(k.value, ast.Call)
                    and isinstance(k.value.func, ast.Name)
                    and k.value.func.id == "min_p_kwargs"
                ]
                explicit = [k for k in node.keywords if k.arg == "min_p"]
                if not spreads and not explicit:
                    offenders.append(f"{f.relative_to(APP_ROOT.parent)}:{node.lineno}")

        assert not offenders, (
            "these call_agent() sites omit min_p, so prism will inject minP=0.05 and a "
            "speculative-decoding vLLM box will answer them with an empty stream: "
            + ", ".join(offenders)
        )
