"""The gatekeeper's shadow must record the gatekeeper — not the fallback.

CONTEXT. The Jetson decision is blocked on n>=10 gatekeeper shadow rows
(`documentation/chapters/05-jetson-plan.md`). Those rows are the first
evidence about a tool-DECLARING role, and the rule set in advance is agreement
with the primary on SELECTED TICKERS in >= 9 of 10. Everything about that rule
assumes `primary_text` is what the gatekeeper said.

WHAT THESE TESTS EXIST FOR. `5f42260` shipped the dispatch as an inline block
guarded by `if result and result.get("response")`, asserted entirely through
`inspect.getsource`. Two things a source assertion cannot see:

  * `_gatekeeper_unusable` returns a SYNTHETIC response — the scoring
    engine's top-N wearing the gatekeeper's JSON shape — so a timed-out or
    unparseable gatekeeper passed that guard and would have been shadowed as
    though it were a verdict. Four such degradations happened on 2026-08-06
    alone, and rows from them would silently answer "the boxes agree" by
    comparing a box against a sort.
  * `chat_toolless` returned no `execution_ms`, so `primary_elapsed_ms` was
    0 on every row.

The dispatch now lives in `maybe_shadow_gatekeeper`, which returns whether it
fired, so these are assertions about behaviour rather than about text.
"""

from unittest.mock import patch

import pytest

from app.services.pipeline_service import maybe_shadow_gatekeeper

GOOD = {
    "response": '{"selected_tickers": ["NVDA"], "rationale": "momentum"}',
    "model_used": "deepseek-v4-flash-0731",
    "provider": "vllm-2",
    "execution_ms": 16234,
    "tokens_used": 2100,
    "loops_used": 1,
}


def _dispatch(result, endpoint="jetson"):
    """Run the helper with the shadow seam and config faked out."""
    calls = []
    with patch("app.v3.model_shadow.dispatch_shadow", side_effect=lambda **kw: calls.append(kw)), \
         patch("app.v3.model_shadow.shadow_endpoint_for", return_value=endpoint):
        fired = maybe_shadow_gatekeeper(
            result=result, agent_name="v3_portfolio_manager",
            cycle_id="cycle-test", bot_id="bot-test",
            system_prompt="sys", user_prompt="usr",
        )
    return fired, calls


class TestItFiresOnARealVerdict:
    def test_a_usable_result_dispatches(self):
        fired, calls = _dispatch(GOOD)

        assert fired is True
        assert len(calls) == 1

    def test_the_primary_block_carries_what_the_comparison_reads(self):
        _fired, calls = _dispatch(GOOD)
        primary = calls[0]["primary"]

        assert primary["response"] == GOOD["response"]
        assert primary["model_used"] == "deepseek-v4-flash-0731"
        # 0 here is the shipped bug: chat_toolless did not report its own
        # elapsed time, so every row booked the primary as instant.
        assert primary["elapsed_ms"] == 16234

    def test_the_prompts_are_passed_verbatim_for_replay(self):
        """`system_prompt`/`user_prompt` are stored uncapped precisely so a
        row can be replayed by jetson_benchmark instead of costing a cycle."""
        _fired, calls = _dispatch(GOOD)

        assert calls[0]["system_prompt"] == "sys"
        assert calls[0]["user_prompt"] == "usr"
        assert calls[0]["agent_name"] == "v3_portfolio_manager"


class TestItRefusesADegradedPrimary:
    """The failure this whole file is about: a fallback that looks like a
    verdict to every check made on its shape."""

    def test_a_degraded_result_does_not_dispatch(self):
        degraded = {
            "response": '{"selected_tickers": ["AAPL", "MSFT"], '
                        '"rationale": "Gatekeeper timed out after 180s — '
                        'auto-selected by scoring engine"}',
            "degraded": True,
            "degraded_reason": "timed out after 180s",
        }

        fired, calls = _dispatch(degraded)

        assert fired is False
        assert calls == []

    def test_the_pipeline_marks_its_fallback(self):
        """The flag has to be SET where the fallback is built, or the check
        above is a check on a field nothing ever writes."""
        import inspect

        from app.services import pipeline_service

        src = inspect.getsource(pipeline_service.PipelineService)
        start = src.index("def _gatekeeper_unusable")
        end = src.index("async def _call_gatekeeper")
        assert '"degraded": True' in src[start:end]

    def test_an_empty_response_does_not_dispatch(self):
        fired, calls = _dispatch({"response": ""})

        assert fired is False
        assert calls == []

    def test_a_missing_result_does_not_dispatch(self):
        fired, calls = _dispatch(None)

        assert fired is False
        assert calls == []


class TestItCannotBreakTheCycle:
    def test_no_configured_endpoint_is_a_quiet_no_op(self):
        fired, calls = _dispatch(GOOD, endpoint=None)

        assert fired is False
        assert calls == []

    def test_a_throwing_dispatch_is_swallowed(self):
        """A bench that kills the desk is worse than no bench."""
        with patch(
            "app.v3.model_shadow.dispatch_shadow", side_effect=RuntimeError("boom")
        ), patch("app.v3.model_shadow.shadow_endpoint_for", return_value="jetson"):
            fired = maybe_shadow_gatekeeper(
                result=GOOD, agent_name="v3_portfolio_manager",
                cycle_id="c", bot_id="b", system_prompt="s", user_prompt="u",
            )

        assert fired is False

    def test_a_throwing_config_read_is_swallowed(self):
        with patch(
            "app.v3.model_shadow.shadow_endpoint_for", side_effect=RuntimeError("boom")
        ):
            fired = maybe_shadow_gatekeeper(
                result=GOOD, agent_name="v3_portfolio_manager",
                cycle_id="c", bot_id="b", system_prompt="s", user_prompt="u",
            )

        assert fired is False


class TestItIsWiredIntoTheCycle:
    """A helper nobody calls shadows nothing — the exact failure mode
    MODEL_SHADOW_AGENTS=v3_portfolio_manager had before 5f42260."""

    def test_the_pipeline_calls_it(self):
        import inspect

        from app.services import pipeline_service

        src = inspect.getsource(pipeline_service.PipelineService)
        assert "maybe_shadow_gatekeeper(" in src

    def test_it_is_called_after_the_parse_check(self):
        """Dispatching before the JSON is parsed shadows an unparseable
        primary — the third route into the degraded fallback."""
        import inspect

        from app.services import pipeline_service

        src = inspect.getsource(pipeline_service.PipelineService)
        assert src.index('if "selected_tickers" not in parsed') < src.index(
            "maybe_shadow_gatekeeper("
        )

    def test_the_deploy_default_includes_the_gatekeeper(self):
        """The wiring is live only if the agent is in MODEL_SHADOW_AGENTS."""
        from pathlib import Path

        deploy = Path(__file__).resolve().parents[2] / "deploy.sh"
        assert "v3_portfolio_manager" in deploy.read_text()


@pytest.mark.parametrize("status,expected", [
    ("SUCCESS", True), ("EMPTY_RESPONSE", False), ("HARNESS_ERROR", False),
])
def test_shadow_classification_stays_fail_closed(status, expected):
    """Guards the other half of the comparison: a box that refused the job in
    1046ms was once booked as "28x faster than Gold Spark"."""
    from app.v3.model_shadow import classify_shadow

    text = {
        "SUCCESS": '{"selected_tickers": ["NVDA"]}',
        "EMPTY_RESPONSE": "",
        "HARNESS_ERROR": "⚠️ **Error:** context window is critically full",
    }[status]
    outcome, _err = classify_shadow(text, tokens=100, loops=1)

    assert (outcome == "SUCCESS") is expected
