"""The fundamental baseline and its reconcile pass.

The 2026-07-28 fidelity audit found this desk emitted no numeric fields across
163 artifacts, so nothing reconciled it and the ratios in its prose were never
checked. Four of seven stated P/Es were wrong in one cycle — CARS by 83%,
because it quoted the FORWARD P/E as the trailing one. Mislabelling and
invention look identical downstream, and neither was catchable.
"""

from unittest.mock import patch

from app.quant.fundamental_block import (
    VERIFIED_NUMERIC_FIELDS,
    build_fundamental_block,
    reconcile_fundamental_metrics,
)


def _baseline(**over):
    b = {
        "ticker": "TEST", "as_of": "2026-07-28", "age_days": 0,
        "stale": False, "source": "yfinance",
        "pe_ratio": 27.99, "forward_pe": 4.83, "roe": 0.0569,
        "debt_to_equity": 0.98, "oper_margin": 0.0972,
    }
    b.update(over)
    return b


class TestTheBlockIsHonestAboutGaps:
    def test_no_row_is_stated_not_silent(self):
        """A silent empty block is indistinguishable from a healthy one
        downstream — the ASIC failure that put NO DATA into technical_baseline."""
        with patch("app.quant.fundamental_block.compute_fundamental_baseline",
                   return_value=None):
            out = build_fundamental_block("NOPE")

        assert "NO DATA ON FILE" in out
        assert out.strip() != ""

    def test_missing_fields_are_named_not_omitted(self):
        """An omitted line reads as 'not relevant'; a named gap reads as
        'unknown'. Only one of those stops the model substituting a memory."""
        with patch("app.quant.fundamental_block.compute_fundamental_baseline",
                   return_value=_baseline()):
            out = build_fundamental_block("TEST")

        assert "NOT ON FILE" in out
        assert "roic" in out

    def test_earnings_absence_is_explicit(self):
        """'Binary earnings risk' is a recurring override reason on this desk
        while earnings_date was cited in 1.5% of decisions — asserted far more
        often than known."""
        with patch("app.quant.fundamental_block.compute_fundamental_baseline",
                   return_value=_baseline()):
            out = build_fundamental_block("TEST")

        assert "Next earnings: NOT ON FILE" in out

    def test_stale_snapshot_says_so(self):
        with patch("app.quant.fundamental_block.compute_fundamental_baseline",
                   return_value=_baseline(stale=True, age_days=90)):
            out = build_fundamental_block("TEST")

        assert "STALE" in out and "90 days old" in out


class TestReconcileCatchesTheRealFailure:
    def test_the_cars_case_forward_pe_quoted_as_trailing(self):
        """CARS stated P/E 4.83 against a stored 27.99 — its forward P/E. The
        reconcile must correct it AND preserve the original, because a rate you
        cannot count is a rate you cannot fix."""
        art = {"metrics": {"pe_ratio": 4.83}}
        with patch("app.quant.fundamental_block.compute_fundamental_baseline",
                   return_value=_baseline()):
            rep = reconcile_fundamental_metrics(art, "CARS")

        assert art["metrics"]["pe_ratio"] == 27.99
        assert art["_model_reported_fundamentals"]["pe_ratio"] == 4.83
        assert rep["corrected"]["pe_ratio"]["model"] == 4.83
        assert rep["applied"] is True

    def test_an_agreeing_number_is_left_alone(self):
        art = {"metrics": {"pe_ratio": 27.99, "roe": 0.0569}}
        with patch("app.quant.fundamental_block.compute_fundamental_baseline",
                   return_value=_baseline()):
            rep = reconcile_fundamental_metrics(art, "TEST")

        assert rep["corrected"] == {}
        assert "_model_reported_fundamentals" not in art

    def test_a_stale_snapshot_does_not_overwrite_a_tool_call(self):
        """A live tool call legitimately beats a stale stored row. Record the
        disagreement, do not apply it — same rule as the valuation pass."""
        art = {"metrics": {"pe_ratio": 4.83}}
        with patch("app.quant.fundamental_block.compute_fundamental_baseline",
                   return_value=_baseline(stale=True, age_days=90)):
            rep = reconcile_fundamental_metrics(art, "TEST", model_used_tools=True)

        assert art["metrics"]["pe_ratio"] == 4.83
        assert art["_unreconciled_fundamentals"]["pe_ratio"]["verified"] == 27.99
        assert rep["applied"] is False

    def test_judgment_fields_are_never_touched(self):
        """Interpretation is the analyst's actual job; this module has no
        opinion about it."""
        art = {
            "metrics": {"pe_ratio": 4.83},
            "summary": "cheap", "thesis_direction": "BULLISH",
            "confidence": 80, "pillars": {"moat": "wide"},
        }
        with patch("app.quant.fundamental_block.compute_fundamental_baseline",
                   return_value=_baseline()):
            reconcile_fundamental_metrics(art, "TEST")

        assert art["summary"] == "cheap"
        assert art["thesis_direction"] == "BULLISH"
        assert art["confidence"] == 80
        assert art["pillars"] == {"moat": "wide"}

    def test_a_missing_metrics_block_is_not_an_error(self):
        """An artifact from before this field existed, or one the model omitted,
        must not raise inside the runner."""
        assert reconcile_fundamental_metrics({"summary": "x"}, "TEST") == {}
        assert reconcile_fundamental_metrics({}, "TEST") == {}

    def test_nan_is_treated_as_absent_not_as_agreement(self):
        """NaN survives NOT NULL and compares false against every threshold, so
        an unfiltered one lands in metrics looking like data."""
        art = {"metrics": {"pe_ratio": float("nan")}}
        with patch("app.quant.fundamental_block.compute_fundamental_baseline",
                   return_value=_baseline()):
            reconcile_fundamental_metrics(art, "TEST")

        assert art["metrics"]["pe_ratio"] == 27.99


class TestTheContract:
    def test_verified_fields_are_all_rendered_or_named(self):
        """A field the reconcile enforces but the block never shows is a field
        the agent is corrected on without ever being told the right value."""
        with patch("app.quant.fundamental_block.compute_fundamental_baseline",
                   return_value=_baseline()):
            out = build_fundamental_block("TEST")

        for field in VERIFIED_NUMERIC_FIELDS:
            present = _baseline().get(field) is not None
            assert (field in out) or present, f"{field} neither shown nor named"


class TestTheUnitsAreUnambiguous:
    """First live cycle (2026-07-28, SMCI): the block printed "ROE 17.88%"
    while `fundamentals.roe` stores 0.17877, and the model copied 17.88 exactly
    as instructed. The reconcile then "corrected" 8 of 8 fields at a ratio of
    precisely 100.0.

    Decisions were never wrong — every value was overwritten — but the
    fabrication RATE was destroyed, and that rate is the entire reason
    originals are preserved. Eight guaranteed false positives per ticker would
    bury any real invention.
    """

    def test_percentage_lines_state_the_value_to_copy(self):
        with patch("app.quant.fundamental_block.compute_fundamental_baseline",
                   return_value=_baseline(roe=0.17877)):
            out = build_fundamental_block("TEST")

        assert "17.88%" in out          # readable
        assert "copy as 0.17877" in out  # unambiguous

    def test_the_copied_value_reconciles_clean(self):
        """The whole point: a model that follows the instruction must produce
        ZERO corrections, so a correction means something real."""
        art = {"metrics": {"roe": 0.17877}}
        with patch("app.quant.fundamental_block.compute_fundamental_baseline",
                   return_value=_baseline(roe=0.17877)):
            rep = reconcile_fundamental_metrics(art, "TEST")

        assert rep["corrected"] == {}

    def test_the_percentage_form_is_still_caught(self):
        """And a model that copies the display value is still corrected —
        the guard must not be loosened to paper over the ambiguity."""
        art = {"metrics": {"roe": 17.88}}
        with patch("app.quant.fundamental_block.compute_fundamental_baseline",
                   return_value=_baseline(roe=0.17877)):
            reconcile_fundamental_metrics(art, "TEST")

        assert art["metrics"]["roe"] == 0.17877

    def test_the_prompt_names_the_bracket_convention(self):
        """A convention the block uses and the prompt never mentions is a
        convention the model cannot follow."""
        from app.v3.agents.fundamental_analyst import SYSTEM_PROMPT

        assert "copy as" in SYSTEM_PROMPT
