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
        does not spend produced a 16% compliance rate."""
        from app.v3.agents.quant_analyst import SYSTEM_PROMPT
        assert "do not spend a turn on `whiteboard_write`" in SYSTEM_PROMPT
