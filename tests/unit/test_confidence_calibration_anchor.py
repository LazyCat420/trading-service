"""The confidence scale must have an anchor, and gaps must be weighed not counted.

Measured 2026-07-29, matched population (desks that produced a decision,
technicals fresh <=3 days):

    window          n    mean conf   clearing 65   clearing 70
    07-14..07-19   147     77.1       140 (95%)     123 (84%)
    07-26..        93      61.6        38 (41%)       4 ( 4%)

Executable BUYs went to ZERO on 07-28/29 (44 decisions, 0 BUYs).

Two hypotheses were tested and REFUTED, which is why the fix is an anchor rather
than a gap-counting change:

* Dose-response: mean confidence is FLAT (~61) across every gap count.
* Caveat density: within each window, marker count does not predict confidence
  (r = -0.065 before, +0.131 after).

What gaps actually do is act as a near-binary gate — 0 gaps: 73% clear the
floor; >=1 gap: 4% (Fisher p=4.2e-09, OR=62) — while the mean barely moves.
Combined with a prompt carrying three "lower confidence" instructions and ZERO
lines on what sustains it, plus a `confidence` field with no schema description
at all, the scale had nothing to resist accumulated caveat text.

These tests pin the anchor and the severity contract. They deliberately do NOT
assert on prompt wording beyond the load-bearing pieces — the point is that the
calibration contract exists and is two-sided, not that it is phrased one way.
"""

from __future__ import annotations

import pytest

from app.v3.shared_desk import render_data_gap, _DEFAULT_GAP_SEVERITY


class TestGapSeverity:
    """Producers tag severity; untagged gaps are MINOR, never BLOCKING."""

    @pytest.mark.parametrize("sev", ["BLOCKING", "MATERIAL", "MINOR"])
    def test_explicit_severity_is_preserved(self, sev):
        out = render_data_gap(f"[{sev}] something is missing")
        assert out.startswith(f"[{sev}]")
        assert "something is missing" in out

    def test_untagged_gap_defaults_to_minor(self):
        """An analyst LLM writes gaps as free text. Those are the ordinary kind
        (a missing 5-year margin trend); they must not inherit the weight of a
        missing price history."""
        assert render_data_gap("3-5 year gross margin trend").startswith("[MINOR]")
        assert _DEFAULT_GAP_SEVERITY == "MINOR"

    def test_severity_tag_is_case_insensitive(self):
        assert render_data_gap("[material] stale snapshot").startswith("[MATERIAL]")

    def test_no_double_tagging(self):
        """Rendering an already-tagged gap must not produce [MINOR] [MATERIAL]."""
        out = render_data_gap("[MATERIAL] stale")
        assert out.count("[") == 1

    def test_non_string_gaps_do_not_raise(self):
        """data_gaps is model-authored; it must never crash the desk renderer."""
        for value in (None, 42, {"gap": "x"}, ["a"]):
            assert render_data_gap(value).startswith("[MINOR]")


class TestConfidenceAnchor:
    """The scale must be defined, and defined in BOTH directions."""

    def test_board_prompt_defines_what_confidence_means(self):
        from app.v3.agents.board_of_directors import _BOARD_COMMON

        assert "confidence" in _BOARD_COMMON.lower()
        # The band that matters: a sound thesis with ordinary gaps is a normal
        # 70-79 decision. Without this the model has no reason to sit above the
        # floor and the whole distribution drifts under it.
        assert "70-79" in _BOARD_COMMON

    def test_the_scale_is_not_a_one_way_ratchet(self):
        """Before this fix the prompt had three lines saying LOWER confidence and
        none saying what sustains it. A scale you can only push down will be
        pushed down."""
        from app.v3.agents.board_of_directors import _BOARD_COMMON

        lowered = _BOARD_COMMON.lower()
        assert "differentiate" in lowered, "must forbid a uniform confidence"
        assert "not a reason to drop a band" in lowered, (
            "must state that an irrelevant gap does NOT lower confidence"
        )

    def test_the_json_example_is_marked_as_not_a_target(self):
        """The only numeric anchor used to be a schema placeholder that differs
        per persona (75/80/65)."""
        from app.v3.agents.board_of_directors import _BOARD_COMMON

        assert "format illustrations" in _BOARD_COMMON.lower()

    def test_final_decision_schema_describes_confidence(self):
        """It carried NO description while a policy gate blocked every BUY/SELL
        below 70 on its value."""
        from app.v3.artifacts import FINAL_DECISION_SCHEMA

        desc = FINAL_DECISION_SCHEMA["properties"]["confidence"].get("description", "")
        assert desc, "the field the floor gates on must say what it means"
        assert "7 sessions" in desc, "the forecast horizon must be stated"

    def test_gaps_are_weighed_not_counted(self):
        """The measured defect: one routine gap was worth as much as a missing
        price history."""
        from app.v3.agents.board_of_directors import PERSONA_WARREN_BUFFETT

        assert "weighed, not counted" in PERSONA_WARREN_BUFFETT

    def test_missing_valuation_blocks_only_a_valuation_thesis(self):
        """Injected into the Board's prompt at _KEEP (never shed), and it used to
        end with an unconditional 'let that missing evidence lower your
        confidence'. Missing valuation data should sink a valuation-led thesis,
        not a technicals-and-catalyst one."""
        from app.quant.valuation_block import _NO_DATA

        assert "severity: BLOCKING" in _NO_DATA
        assert "If your thesis rests on" in _NO_DATA

    def test_missing_technicals_blocks_only_a_technical_thesis(self):
        """Same contract, but this text is built inline rather than being a
        module constant — so drive the real function with a ticker that has no
        stored baseline instead of skipping the case."""
        from unittest.mock import patch

        import app.quant.technical_baseline as tb

        with patch.object(tb, "compute_technical_baseline", return_value=None):
            text = tb.build_technical_baseline_block("NO_SUCH_TICKER_XYZ")

        assert "NONE ON FILE" in text
        assert "severity: BLOCKING" in text
        assert "If your thesis rests on" in text
