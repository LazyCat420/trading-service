"""Tests for the 2026-07-24 Quant Analyst audit (Phase 4).

The finding: the quant desk was inventing its risk numbers. Tracing every RSI
in 305 reports back to the text the agent was given, only 134 matched a number
anywhere on the desk — 171 did not, and 148 of those came from runs that made
ZERO tool calls (measured: IP reported 58.0 against a desk value of 71.19,
GOOGL 47.0 against 53.7).

risk_metrics drives volatility_regime and stop placement and is read as fact by
the Board, so the fix is structural: RSI/ATR/SMA/Bollinger already exist in the
`technicals` table, so they are computed, injected, and reconciled onto the
artifact rather than restated by a language model.
"""

import pytest

from app.quant import technical_baseline as tb


@pytest.fixture
def fresh_baseline(monkeypatch):
    monkeypatch.setattr(tb, "compute_technical_baseline", lambda ticker: {
        "as_of": "2026-07-24", "stale": False, "age_days": 0,
        "rsi": 30.3, "atr": 2.96, "sma_200_status": "BELOW",
        "bollinger_position": "LOWER", "volume_trend": "STABLE",
    })


@pytest.fixture
def stale_baseline(monkeypatch):
    monkeypatch.setattr(tb, "compute_technical_baseline", lambda ticker: {
        "as_of": "2026-07-17", "stale": True, "age_days": 7,
        "rsi": 42.26, "atr": 11.69, "sma_200_status": "BELOW",
    })


def _artifact():
    return {"risk_metrics": {"rsi": 47.0, "atr": 9.9, "sma_200_status": "ABOVE"}}


class TestFreshBaselineIsAuthoritative:
    def test_fabricated_values_are_replaced(self, fresh_baseline):
        art = _artifact()
        report = tb.reconcile_risk_metrics(art, "MP", model_used_tools=False)

        assert report["applied"] is True
        assert art["risk_metrics"]["rsi"] == 30.3
        assert art["risk_metrics"]["atr"] == 2.96
        assert art["risk_metrics"]["sma_200_status"] == "BELOW"

    def test_the_models_original_values_are_preserved(self, fresh_baseline):
        """The point is to stop the bad number reaching the Board, not to hide
        that it was produced — the rate has to stay measurable."""
        art = _artifact()
        tb.reconcile_risk_metrics(art, "MP", model_used_tools=False)

        assert art["_model_reported_metrics"]["rsi"] == 47.0
        assert art["_model_reported_metrics"]["sma_200_status"] == "ABOVE"

    def test_missing_fields_are_filled_in(self, fresh_baseline):
        art = {"risk_metrics": {}}
        tb.reconcile_risk_metrics(art, "MP", model_used_tools=False)
        assert art["risk_metrics"]["rsi"] == 30.3

    def test_close_enough_values_are_left_alone(self, fresh_baseline):
        """Rounding and a one-session lag are fine; a different number is not."""
        art = {"risk_metrics": {"rsi": 30.9, "atr": 2.96}}
        report = tb.reconcile_risk_metrics(art, "MP", model_used_tools=False)
        assert "rsi" not in report["corrected"]
        assert "_model_reported_metrics" not in art


class TestStaleBaselineDoesNotClobberFreshFetches:
    def test_stale_baseline_yields_to_a_tool_using_agent(self, stale_baseline):
        """technicals lags for most tickers; overwriting a genuinely fetched
        value with a week-old stored one is its own regression."""
        art = _artifact()
        report = tb.reconcile_risk_metrics(art, "GOOGL", model_used_tools=True)

        assert report["applied"] is False
        assert art["risk_metrics"]["rsi"] == 47.0          # untouched
        assert art["_unreconciled_metrics"]["rsi"]["verified"] == 42.26

    def test_stale_baseline_still_beats_an_invented_number(self, stale_baseline):
        """No tool calls means the agent had no source at all."""
        art = _artifact()
        report = tb.reconcile_risk_metrics(art, "GOOGL", model_used_tools=False)

        assert report["applied"] is True
        assert art["risk_metrics"]["rsi"] == 42.26

    def test_staleness_is_disclosed_as_a_data_gap(self, stale_baseline):
        art = _artifact()
        tb.reconcile_risk_metrics(art, "GOOGL", model_used_tools=False)
        assert any("stale" in g or "days old" in g for g in art["data_gaps"])


class TestFailsafe:
    def test_no_baseline_is_a_noop(self, monkeypatch):
        """An empty baseline must read as 'unverified', never as 'no risk'."""
        monkeypatch.setattr(tb, "compute_technical_baseline", lambda t: {})
        art = _artifact()
        assert tb.reconcile_risk_metrics(art, "ZZZZ") == {}
        assert art["risk_metrics"]["rsi"] == 47.0

    def test_missing_risk_metrics_is_a_noop(self, fresh_baseline):
        assert tb.reconcile_risk_metrics({"summary": "x"}, "MP") == {}

    def test_non_dict_artifact_is_a_noop(self, fresh_baseline):
        assert tb.reconcile_risk_metrics(None, "MP") == {}

    def test_db_failure_degrades_loudly(self, monkeypatch):
        """A DB failure must yield NO verified values — and must SAY so.

        Updated 2026-07-27: this previously asserted the block was "".
        Returning an empty string is how ASIC reached the board with zero
        price history and nothing in the prompt or the logs mentioning it —
        the ticker the agent knew least about produced the least warning.
        The degrade is unchanged (no fabricated numbers); it is now explicit.
        """
        def boom(*a, **k):
            raise RuntimeError("db down")
        monkeypatch.setattr(tb, "_fetch_technicals", boom)
        assert tb.compute_technical_baseline("MP") == {}

        block = tb.build_technical_baseline_block("MP")
        assert "NONE ON FILE" in block
        # Critically: it must not invent or imply any level.
        for token in ("RSI-14:", "SMA-50:", "ATR-14:", "Bollinger position:"):
            assert token not in block

    def test_nan_never_reaches_a_metric(self, monkeypatch):
        monkeypatch.setattr(tb, "_fetch_technicals", lambda t: {
            "date": "2026-07-24", "rsi_14": float("nan"), "atr_14": 2.5,
            "bb_upper": None, "bb_mid": None, "bb_lower": None,
            "sma_50": None, "sma_200": None, "support": None, "resistance": None,
        })
        monkeypatch.setattr(tb, "_fetch_price_and_volume", lambda t: (100.0, "STABLE"))
        baseline = tb.compute_technical_baseline("MP")
        assert "rsi" not in baseline
        assert baseline["atr"] == 2.5


class TestBollingerOutsideTheBands:
    @pytest.mark.parametrize("close,expected,note", [
        (150.0, "UPPER", "above the upper band (extended)"),
        (50.0, "LOWER", "below the lower band (extended)"),
        (100.0, "MIDDLE", None),
    ])
    def test_price_outside_the_bands_is_named_not_shown_as_a_bad_percent(
        self, monkeypatch, close, expected, note
    ):
        monkeypatch.setattr(tb, "_fetch_technicals", lambda t: {
            "date": "2026-07-24", "rsi_14": 50.0, "atr_14": 1.0,
            "bb_upper": 120.0, "bb_mid": 100.0, "bb_lower": 80.0,
            "sma_50": None, "sma_200": None, "support": None, "resistance": None,
        })
        monkeypatch.setattr(tb, "_fetch_price_and_volume", lambda t: (close, None))
        b = tb.compute_technical_baseline("X")
        assert b["bollinger_position"] == expected
        assert b.get("bollinger_note") == note


class TestPromptContract:
    def test_prompt_forbids_restating_the_numbers(self):
        from app.v3.agents.quant_analyst import SYSTEM_PROMPT
        assert "DO NOT RESTATE THEM FROM MEMORY" in SYSTEM_PROMPT

    def test_prompt_no_longer_asks_for_the_whiteboard_write(self):
        """It is posted from the artifact in code — asking for a turn the agent
        does not spend produced a 16% compliance rate.

        STRENGTHENED 2026-07-29. This asserted the prompt SAID "do not spend a
        turn on `whiteboard_write`", and the agent kept calling it anyway: 53
        of 326 quant writes were sub-60-character stubs, one literally
        `{'confidence': 65}`. Telling a model not to use a tool it still holds
        is not a removal — measured twice on this desk, first with
        get_finviz_fundamentals and again here. The tool is now out of the
        whitelist and its name is out of the prompt, so the assertion flips
        from "the prompt discourages it" to "the tool is not reachable".
        """
        from app.v3.agents.quant_analyst import SYSTEM_PROMPT, TOOL_WHITELIST
        assert "whiteboard_write" not in TOOL_WHITELIST
        assert "`whiteboard_write`" not in SYSTEM_PROMPT


class TestTheUnguardedFields:
    """2026-07-28 fidelity audit: risk_metrics carried six numeric fields while
    VERIFIED_NUMERIC_FIELDS listed two, so four went unchecked in ~136 reports
    each. The worst was max_drawdown_est, which appeared in NO injected block —
    the model had nothing to copy, so it copied the PROMPT."""

    def test_max_drawdown_is_computed_not_left_to_the_model(self, monkeypatch):
        import numpy as np

        monkeypatch.setattr(tb, "_fetch_technicals", lambda t: {
            "date": "2026-07-28", "rsi_14": 50.0, "atr_14": 1.0,
            "bb_upper": None, "bb_lower": None, "sma_50": None,
            "sma_200": None, "support": None, "resistance": None,
        })
        monkeypatch.setattr(tb, "_fetch_price_and_volume", lambda t: (100.0, None))
        # -30% then partial recovery: peak-to-trough is 30%.
        rets = np.concatenate([
            np.full(40, 0.0), np.full(1, -0.30), np.full(40, 0.001),
        ])
        monkeypatch.setattr(
            "app.quant.returns.load_close_returns", lambda t, n=500: rets
        )

        b = tb.compute_technical_baseline("X")

        assert b["max_drawdown_est"] == pytest.approx(30.0, abs=0.5)

    def test_the_prompt_placeholder_is_corrected(self, monkeypatch):
        """`max_drawdown_est: 12.5` was the literal value in the prompt's JSON
        example and recurred 15 times across different tickers, with 0.0 seven
        more. An anchor, not a measurement."""
        monkeypatch.setattr(tb, "compute_technical_baseline", lambda t: {
            "as_of": "2026-07-28", "stale": False, "age_days": 0,
            "max_drawdown_est": 52.92,
        })
        art = {"risk_metrics": {"max_drawdown_est": 12.5}}

        rep = tb.reconcile_risk_metrics(art, "PYPL")

        assert art["risk_metrics"]["max_drawdown_est"] == 52.92
        assert art["_model_reported_metrics"]["max_drawdown_est"] == 12.5
        assert rep["corrected"]["max_drawdown_est"]["model"] == 12.5

    def test_percentage_fields_use_a_relative_tolerance(self, monkeypatch):
        """The 1.0 ABSOLUTE tolerance is right for RSI and meaningless for
        vol_prediction_premium, whose entire observed range is [-0.38, 1.43] —
        1.0 absolute accepts any value at all."""
        monkeypatch.setattr(tb, "compute_technical_baseline", lambda t: {
            "as_of": "2026-07-28", "stale": False, "age_days": 0,
            "vol_prediction_premium": -0.34,
        })
        art = {"risk_metrics": {"vol_prediction_premium": 0.50}}

        tb.reconcile_risk_metrics(art, "X")

        assert art["risk_metrics"]["vol_prediction_premium"] == -0.34

    def test_a_faithfully_copied_garch_value_is_left_alone(self, monkeypatch):
        """predicted_vol_annualized_pct matched the block 127/127 — the guard
        must lock that in, not manufacture corrections."""
        monkeypatch.setattr(tb, "compute_technical_baseline", lambda t: {
            "as_of": "2026-07-28", "stale": False, "age_days": 0,
            "predicted_vol_annualized_pct": 40.53,
        })
        art = {"risk_metrics": {"predicted_vol_annualized_pct": 40.53}}

        rep = tb.reconcile_risk_metrics(art, "PYPL")

        assert rep["corrected"] == {}
        assert "_model_reported_metrics" not in art

    def test_diversification_ratio_is_not_claimed_as_verified(self):
        """It is a property of the portfolio AND the candidate, so it cannot be
        recomputed from a ticker alone. Listing it would read as 'verified' in
        the audit while checking nothing."""
        assert "diversification_ratio" not in tb.VERIFIED_NUMERIC_FIELDS


class TestTheSignalsPostCarriesEvidenceOrNothing:
    """Measured 2026-07-29: 53 of 326 quant whiteboard writes (16%) were under
    60 characters, one of them literally `{'confidence': 65}`. The quant is the
    ONLY agent that does this — junior, fundamental, board, valuation,
    tournament and regime are all at 0/326.

    `signals` is the section teammates are told to annotate ("read a teammate's
    section — desk_note or signals"), so a stub occupies the slot and LOOKS
    like data while giving the fundamental analyst nothing to agree or disagree
    with. The collaboration silently loses its substrate.

    A missing section reads as "the quant had nothing". An empty one reads as
    "the quant said almost nothing". Only the first is true.
    """

    def test_a_self_report_only_payload_is_not_posted(self):
        import inspect

        from app.v3 import agent_runner

        src = inspect.getsource(agent_runner._persist_quant_signals)
        assert "_SELF_REPORT" in src
        assert '"confidence", "thesis_direction"' in src
        # It must SKIP, not post a stub.
        assert "not posting a stub" in src

    def test_evidence_fields_still_post(self):
        """The guard must not suppress a real signals payload — that would
        reintroduce the 9-of-56 miss rate this auto-post exists to fix."""
        import inspect

        from app.v3 import agent_runner

        src = inspect.getsource(agent_runner._persist_quant_signals)
        # The skip is gated on the set DIFFERENCE, so any evidence field
        # (rsi, atr, volatility_regime, stop_loss_suggestion, ...) posts.
        assert "set(content) - _SELF_REPORT" in src

    def test_the_prompt_does_not_name_a_tool_the_agent_lacks(self):
        """The prompt told this agent "do not spend a turn on
        `whiteboard_write`" while the tool was still whitelisted — and it kept
        calling it. Naming a tool the agent no longer holds is the same trap
        one step further: a live instruction pointing at a dead end."""
        from app.v3.agents.quant_analyst import SYSTEM_PROMPT, TOOL_WHITELIST

        assert "whiteboard_write" not in TOOL_WHITELIST
        assert "`whiteboard_write`" not in SYSTEM_PROMPT

    def test_the_prompt_says_how_to_post_instead(self):
        """Removing the tool without saying what replaces it would leave the
        agent unable to reach the desk at all."""
        from app.v3.agents.quant_analyst import SYSTEM_PROMPT

        assert "posted from your artifact automatically" in SYSTEM_PROMPT
        assert "risk_metrics" in SYSTEM_PROMPT


class TestQuantPersistenceRunsOnlyForTheQuant:
    """The real cause of the `{'confidence': 65}` whiteboard stub, found in the
    container logs on 2026-07-29 — NOT by reading the code, which I misread
    three times first.

    `_persist_quant_chart` and `_persist_quant_signals` read `risk_metrics` and
    `overlays`, which only the quant artifact carries. But the calls sat
    OUTSIDE any agent_name guard, so they ran after EVERY agent. The warning
    "carried no evidence fields (only ['confidence'])" fires two lines after
    "Appended valuation_report" — it was the VALUATION analyst's artifact being
    posted into the QUANT's `signals` section, under the quant's name.

    So the stub was never the quant being lazy. It was another agent's artifact
    in the quant's slot. 53 of 326 quant-authored writes.
    """

    def test_the_persistence_is_scoped_to_the_quant(self):
        import inspect

        from app.v3 import agent_runner

        src = inspect.getsource(agent_runner.run_v3_agent)
        # The call must be inside a quant guard, not at the shared tail.
        idx = src.index("_persist_quant_signals(")
        preceding = src[:idx]
        guard = preceding.rindex('if agent_name == "v3_quant_analyst"')
        between = preceding[guard:]
        # Nothing may re-scope to a different agent between the guard and the call.
        assert 'if agent_name == "v3_valuation_analyst"' not in between
        assert 'if agent_name == "v3_fundamental_analyst"' not in between

    def test_both_quant_only_persisters_are_inside_the_guard(self):
        import inspect

        from app.v3 import agent_runner

        src = inspect.getsource(agent_runner.run_v3_agent)
        guard = src.rindex('if agent_name == "v3_quant_analyst"')
        tail = src[guard:]
        assert "_persist_quant_chart(" in tail
        assert "_persist_quant_signals(" in tail
