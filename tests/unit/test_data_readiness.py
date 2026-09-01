"""The readiness evaluator, checked against what the producers actually emit.

THE BUG THIS FILE USED TO HAVE. The old version asserted a DATA_GAP using the
fixture

    technical_context="NONE ON FILE: No technical indicators"

and the rule it exercised required both "NONE ON FILE" and the literal
"No technical indicators". No producer in the repo emits the second string —
`technical_baseline.build_technical_baseline_block` writes
"TECHNICAL BASELINE: **NONE ON FILE** ... no verified RSI, SMA, ATR or
Bollinger level". So the rule could never fire in production, and the test
passed only because the fixture manufactured the exact text the code was
looking for. A test that supplies the input its subject is looking for, when
nothing else in the system supplies it, measures the fixture.

The evaluator is SHADOW ONLY — nothing gates on the stamp yet; the hard gate is
Phase 3. These tests therefore pin the reasons, not any trading behaviour.
"""

import pathlib

import pytest

from app.v3.data_readiness import (
    NO_TECHNICAL_BASELINE_MARKER,
    STALE_TRADING_DAYS,
    evaluate_ticker_readiness,
)

REPO = pathlib.Path(__file__).resolve().parents[2]

#: Verbatim from technical_baseline.build_technical_baseline_block.
REAL_NO_BASELINE_TEXT = (
    "TECHNICAL BASELINE: **NONE ON FILE** [severity: BLOCKING for any "
    "technically-driven thesis] — this ticker has no stored indicator row, so "
    "there is no verified RSI, SMA, ATR or Bollinger level to anchor on."
)


class TestTheMarkerMatchesItsProducer:
    """The cross-check the original pair was missing."""

    def test_the_marker_appears_in_the_producer(self):
        src = (REPO / "app" / "quant" / "technical_baseline.py").read_text(encoding="utf-8")

        assert NO_TECHNICAL_BASELINE_MARKER in src, (
            "the consumer is looking for a string the producer no longer emits — "
            "the rule is disarmed"
        )

    def test_the_string_the_old_test_invented_is_emitted_by_nobody(self):
        """Kept as a regression: this is what a fabricated fixture looks like."""
        hits = [
            f.name for f in (REPO / "app").rglob("*.py")
            # data_readiness.py documents the incident, so it names the string
            # without emitting it.
            if f.name != "data_readiness.py"
            and "No technical indicators" in f.read_text(encoding="utf-8")
        ]

        assert not hits, f"unexpectedly emitted by {hits}"


class TestAReadyTicker:
    def test_clean_inputs_proceed(self):
        res = evaluate_ticker_readiness(
            ticker="NVDA",
            data_report="### PRICE & PROFILE\nSpot: $120.50\n### TECHNICAL INDICATORS\nRSI: 55.4",
            technical_context="RSI: 55.4, SMA20: $118.0",
            valuation_context="EV/EBITDA: 35.2",
            price_age_trading_days=0,
        )

        assert res.is_ready is True
        assert res.disposition == "PROCEED"
        assert res.quality_score == 1.0
        assert res.missing_reasons == []


class TestTheReasonsFireOnRealInput:
    def test_a_failed_precollect(self):
        res = evaluate_ticker_readiness(
            ticker="FAILTICKER",
            data_report="Failed to pre-collect stock data: 404 not found",
        )

        assert res.disposition == "DATA_GAP"
        assert "data_report_collection_failed" in res.missing_reasons

    def test_a_missing_baseline_using_the_producer_s_own_words(self):
        res = evaluate_ticker_readiness(
            ticker="THINLY",
            data_report="### PRICE & PROFILE\nSpot: $3.10",
            technical_context=REAL_NO_BASELINE_TEXT,
        )

        assert "missing_technical_baseline" in res.missing_reasons

    def test_stale_prices_use_the_same_threshold_as_the_gate(self):
        """Was > 5 while HOLD_POLICY_BLOCKED_STALE_PRICE_DATA blocks at > 3."""
        assert STALE_TRADING_DAYS == 3

        at_threshold = evaluate_ticker_readiness(
            ticker="X", data_report="ok", price_age_trading_days=3)
        over = evaluate_ticker_readiness(
            ticker="X", data_report="ok", price_age_trading_days=4)

        assert at_threshold.is_ready is True
        assert "price_history_stale_4_days" in over.missing_reasons

    def test_the_gate_and_the_shadow_agree_on_the_number(self):
        """Read the gate's own threshold rather than trusting a comment."""
        src = (REPO / "app" / "v3" / "orchestrator.py").read_text(encoding="utf-8")

        assert f"stale_age > {STALE_TRADING_DAYS}" in src

    def test_an_unknown_age_is_not_treated_as_fresh(self):
        """The staleness probe can die on its own; 'unknown' is its own fact."""
        res = evaluate_ticker_readiness(
            ticker="X", data_report="ok",
            price_age_trading_days=None, stale_detection_failed=True)

        assert "price_age_unknown" in res.missing_reasons

    def test_a_known_fresh_age_beats_a_stale_detection_flag(self):
        res = evaluate_ticker_readiness(
            ticker="X", data_report="ok",
            price_age_trading_days=1, stale_detection_failed=True)

        assert res.is_ready is True


class TestScoring:
    def test_more_reasons_score_lower(self):
        one = evaluate_ticker_readiness(ticker="X", data_report="")
        many = evaluate_ticker_readiness(
            ticker="X",
            data_report="Failed to pre-collect stock data",
            technical_context=REAL_NO_BASELINE_TEXT,
            price_age_trading_days=10,
        )

        assert 0.0 <= many.quality_score < one.quality_score < 1.0
        assert len(many.missing_reasons) == 3
