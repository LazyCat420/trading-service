"""Tests for the valuation fabrication guard.

Structural sibling of test_quant_fabrication_guard.py. That guard exists
because the 2026-07-24 audit could COUNT the fabrication: 171 of 305 quant
reports carried an RSI matching no number on the desk. This one is built so
the equivalent count is possible for valuation multiples — the reconcile
report is the instrument, not just the fix.
"""

import pytest

from app.quant import valuation_block as vb


@pytest.fixture
def fresh_baseline(monkeypatch):
    monkeypatch.setattr(vb, "compute_valuation_baseline", lambda ticker: {
        "as_of": "2026-07-27", "stale": False, "age_days": 1,
        "enterprise_value": 414_500_000_000.0,
        "ev_to_ebit": 18.4, "ev_to_sales": 4.1, "fcf_yield_pct": 3.8,
        "pe_ratio": 24.6, "peg": 2.24, "net_debt_to_ebit": 0.65,
        "revenue_cagr_pct": 8.2, "fcf_cagr_pct": 3.1,
        "implied_growth_pct": 10.4,
    })


@pytest.fixture
def stale_baseline(monkeypatch):
    monkeypatch.setattr(vb, "compute_valuation_baseline", lambda ticker: {
        "as_of": "2026-05-01", "stale": True, "age_days": 87,
        "ev_to_ebit": 18.4, "implied_growth_pct": 10.4,
    })


def _artifact(**overrides) -> dict:
    metrics = {
        "enterprise_value": 398_000_000_000.0,   # forgot the net debt
        "ev_to_ebit": 12.0,                      # invented
        "ev_to_sales": 4.1,                      # correct
        "fcf_yield_pct": 3.8,
        "pe_ratio": 24.6, "peg": 2.24, "net_debt_to_ebit": 0.65,
        "revenue_cagr_pct": 8.2, "fcf_cagr_pct": 3.1,
        "implied_growth_pct": 4.0,               # invented
    }
    metrics.update(overrides)
    return {
        "valuation_metrics": metrics,
        "verdict": "UNDERVALUED",
        "fair_value_estimate": 165.0,
        "fair_value_basis": "16x EV/EBIT on TTM operating income",
        "price_implied_assumption": "the market expects growth to stall",
        "what_would_change_my_mind": "a second quarter of FCF decline",
        "confidence": 72,
        "doctrine_rules_applied": ["reverse_dcf_first"],
    }


class TestFreshBaselineIsAuthoritative:
    def test_fabricated_multiples_are_replaced(self, fresh_baseline):
        art = _artifact()

        report = vb.reconcile_valuation_metrics(art, "AAPL",
                                                model_used_tools=False)

        assert report["applied"] is True
        assert art["valuation_metrics"]["ev_to_ebit"] == 18.4
        assert art["valuation_metrics"]["implied_growth_pct"] == 10.4
        assert art["valuation_metrics"]["enterprise_value"] == 414_500_000_000.0

    def test_the_models_originals_are_preserved(self, fresh_baseline):
        """The point is to stop the bad number reaching the Board, not to hide
        that it was produced. Without this the fabrication rate is unknowable,
        which is how the RSI problem survived for months."""
        art = _artifact()

        vb.reconcile_valuation_metrics(art, "AAPL")

        assert art["_model_reported_valuation"]["ev_to_ebit"] == 12.0
        assert art["_model_reported_valuation"]["implied_growth_pct"] == 4.0

    def test_the_report_names_both_values(self, fresh_baseline):
        art = _artifact()

        report = vb.reconcile_valuation_metrics(art, "AAPL")

        assert report["corrected"]["ev_to_ebit"] == {
            "model": 12.0, "verified": 18.4}

    def test_agreeing_fields_are_not_reported_as_corrections(self,
                                                             fresh_baseline):
        art = _artifact()

        report = vb.reconcile_valuation_metrics(art, "AAPL")

        assert "ev_to_sales" not in report["corrected"]
        assert "peg" not in report["corrected"]

    def test_a_missing_field_is_filled_in(self, fresh_baseline):
        art = _artifact()
        del art["valuation_metrics"]["ev_to_ebit"]

        vb.reconcile_valuation_metrics(art, "AAPL")

        assert art["valuation_metrics"]["ev_to_ebit"] == 18.4
        # Absent is not the same claim as wrong — nothing to preserve.
        assert "ev_to_ebit" not in art.get("_model_reported_valuation", {})

    def test_a_nan_metric_is_treated_as_absent(self, fresh_baseline):
        art = _artifact(ev_to_ebit=float("nan"))

        vb.reconcile_valuation_metrics(art, "AAPL")

        assert art["valuation_metrics"]["ev_to_ebit"] == 18.4


class TestToleranceIsRelative:
    def test_rounding_passes(self, fresh_baseline):
        """18.4 vs 18.39 is the same number typed differently."""
        art = _artifact(ev_to_ebit=18.39)

        report = vb.reconcile_valuation_metrics(art, "AAPL")

        assert "ev_to_ebit" not in report["corrected"]

    def test_a_different_number_is_caught(self, fresh_baseline):
        art = _artifact(ev_to_ebit=21.0)

        report = vb.reconcile_valuation_metrics(art, "AAPL")

        assert "ev_to_ebit" in report["corrected"]

    def test_market_cap_reported_as_enterprise_value_is_caught(self,
                                                               fresh_baseline):
        """The single most likely valuation error, and the reason the tolerance
        is 1% rather than 5%: this fixture's net debt is 4% of EV, so a 5% band
        called $398B market cap an acceptable rendering of a $414.5B EV. Most
        large caps sit inside 5%, so the guard would have been inert on exactly
        the population it was written for."""
        art = _artifact()

        report = vb.reconcile_valuation_metrics(art, "AAPL")

        assert "enterprise_value" in report["corrected"]
        assert art["valuation_metrics"]["enterprise_value"] == 414_500_000_000.0

    def test_a_large_magnitude_field_is_not_swamped(self, fresh_baseline):
        """The absolute 1.0 tolerance technical_baseline uses for RSI is
        meaningless across fields spanning 1e-2 to 1e11: it would flag a $1
        rounding difference on a $414B enterprise value as fabrication, and
        simultaneously wave through a 100%-wrong FCF yield of 0.9 vs 3.8."""
        art = _artifact(enterprise_value=414_500_000_001.0, fcf_yield_pct=0.9)

        report = vb.reconcile_valuation_metrics(art, "AAPL")

        assert "enterprise_value" not in report["corrected"]
        assert "fcf_yield_pct" in report["corrected"]


class TestJudgmentIsNeverOverwritten:
    def test_interpretive_fields_survive_intact(self, fresh_baseline):
        """Judgment is the agent's actual job and the only reason there is a
        language model in this seam at all. The block has no opinion here."""
        art = _artifact()
        before = {k: art[k] for k in (
            "verdict", "fair_value_estimate", "fair_value_basis",
            "price_implied_assumption", "what_would_change_my_mind",
            "confidence", "doctrine_rules_applied")}

        vb.reconcile_valuation_metrics(art, "AAPL")

        for key, value in before.items():
            assert art[key] == value

    def test_an_undervalued_verdict_survives_a_corrected_multiple(
            self, fresh_baseline):
        """Even when the correction undercuts the verdict — EV/EBIT 12 was the
        reason it said UNDERVALUED, and the truth is 18.4. Flipping the verdict
        in code would be this module inventing judgment, which is exactly the
        failure mode it exists to prevent, pointed the other way."""
        art = _artifact()

        vb.reconcile_valuation_metrics(art, "AAPL")

        assert art["verdict"] == "UNDERVALUED"
        assert art["valuation_metrics"]["ev_to_ebit"] == 18.4


class TestStaleness:
    def test_stale_plus_tool_use_does_not_overwrite(self, stale_baseline):
        """Trading one wrong number for another: an 87-day-old stored multiple
        must not clobber a figure the agent genuinely fetched live."""
        art = _artifact()

        report = vb.reconcile_valuation_metrics(art, "AAPL",
                                                model_used_tools=True)

        assert report["applied"] is False
        assert art["valuation_metrics"]["ev_to_ebit"] == 12.0
        assert art["_unreconciled_valuation"]["ev_to_ebit"]["verified"] == 18.4

    def test_stale_without_tools_is_corrected_and_flagged(self, stale_baseline):
        """The agent had no source at all — a real stale number beats an
        invented one, but the gap must be visible downstream."""
        art = _artifact()

        report = vb.reconcile_valuation_metrics(art, "AAPL",
                                                model_used_tools=False)

        assert report["applied"] is True
        assert art["valuation_metrics"]["ev_to_ebit"] == 18.4
        assert any("87 days old" in g for g in art["data_gaps"])


class TestDegenerateInputs:
    def test_a_missing_metrics_block_is_a_noop(self, fresh_baseline):
        art = {"verdict": "FAIR"}
        assert vb.reconcile_valuation_metrics(art, "AAPL") == {}
        assert art == {"verdict": "FAIR"}

    def test_a_non_dict_artifact_is_a_noop(self, fresh_baseline):
        assert vb.reconcile_valuation_metrics(None, "AAPL") == {}
        assert vb.reconcile_valuation_metrics("nope", "AAPL") == {}

    def test_no_baseline_means_no_corrections(self, monkeypatch):
        """An empty baseline is 'we could not verify', never 'the model was
        right'. Nothing is written either way."""
        monkeypatch.setattr(vb, "compute_valuation_baseline", lambda t: {})
        art = _artifact()

        assert vb.reconcile_valuation_metrics(art, "NEWCO") == {}
        assert art["valuation_metrics"]["ev_to_ebit"] == 12.0
        assert "_model_reported_valuation" not in art
