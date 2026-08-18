"""Every /agent call to a local vLLM box must carry min_p=0.

THE BUG. Prism's ParameterRegistry gives `minP` an agentDefault of 0.05 and
injects it whenever the caller omits the field — which trading-service always
did. vLLM with speculative decoding refuses any min_p > 0, and it raises that
INSIDE the stream generator after answering HTTP 200, so prism receives an
empty stream rather than an error and reports a successful call with no
content.

That is the whole of "Agent failed: empty response from v3_portfolio_manager"
— GATEKEEPER_DEGRADED x4 on 2026-08-06, each one silently demoting ticker
selection from the Gatekeeper's judgement to raw top-N-by-score.

MEASURED 2026-08-06 against the Jetson (Qwen3.6-35B-AWQ), interleaved rounds,
identical prompt, one variable changed:

    prism default   0/3 non-empty    0/3 valid artifact
    min_p=0.0       3/3 non-empty    3/3 valid artifact

These assertions are on the DECISION, not on "an agent produced text": the
failure returns HTTP 200 with empty content, so a smoke test that only checks
for a non-crash passes in both states.
"""

import inspect

from app.agents import base_agent
from app.agents.base_agent import min_p_for


class TestLocalBoxesGetZero:
    def test_jetson_provider(self):
        assert min_p_for("vllm", "cyankiwi/Qwen3.6-35B-A3B-AWQ-4bit") == 0.0

    def test_gold_spark_provider(self):
        assert min_p_for("vllm-2", "deepseek-v4-flash-0731") == 0.0

    def test_provider_none_matches_base_agent_fallback(self):
        """BaseAgent defaults provider to "vllm" when we pass none.

        The model_override path leaves resolved_provider None, so if this
        returned None the override path would keep the broken 0.05 default —
        the exact hole that made this look model-specific.
        """
        assert min_p_for(None, "cyankiwi/Qwen3.6-35B-A3B-AWQ-4bit") == 0.0


class TestCloudModelsAreLeftAlone:
    """0.0 is vLLM's own default; it is not ours to impose elsewhere."""

    def test_anthropic(self):
        assert min_p_for("vllm", "claude-sonnet-5") is None

    def test_openai(self):
        assert min_p_for("openai", "gpt-5.2") is None

    def test_google(self):
        assert min_p_for("vllm", "gemini-1.5-pro-002") is None

    def test_cloud_model_wins_over_a_vllm_provider(self):
        """Prism routes on the model NAME, so the model is the authority."""
        assert min_p_for("vllm-2", "claude-opus-5") is None


class TestUnknownProvidersKeepTodaysBehaviour:
    def test_unknown_provider_is_none(self):
        assert min_p_for("some-new-gateway", "whatever-model") is None


class TestItIsActuallyWiredIn:
    """A correct helper nobody calls fixes nothing."""

    def test_run_agent_calls_the_helper(self):
        src = inspect.getsource(base_agent.run_agent)
        assert "min_p_for(" in src

    def test_the_result_reaches_base_agent_kwargs(self):
        src = inspect.getsource(base_agent.run_agent)
        assert 'kwargs["min_p"] = resolved_min_p' in src

    def test_sdk_capability_is_checked_before_passing(self):
        """A partial deploy must degrade, not TypeError every agent."""
        src = inspect.getsource(base_agent.run_agent)
        assert "_BASE_AGENT_ACCEPTS_MIN_P" in src

    def test_installed_sdk_supports_it(self):
        """Guards the two repos drifting apart in the deployed image."""
        assert base_agent._BASE_AGENT_ACCEPTS_MIN_P, (
            "installed lazycat SDK has no min_p on BaseAgent — sync lazycat-sdk"
        )
