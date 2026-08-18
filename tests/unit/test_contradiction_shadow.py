"""
Tests for the V3 Contradiction Shadow (observation-only mesh step).

Verifies that:
- the previously-dead cognition detector is actually reachable/wired,
- a genuine bull/bear split across independent artifacts is flagged,
- adversarial-by-construction bull_argument/bear_rebuttal are NOT counted,
- agreement produces zero contradictions,
- the shadow never mutates the desk's decision,
- price-target divergence is flagged in the same units.
"""

from app.v3.shared_desk import SharedDesk
from app.v3.contradiction_shadow import compute_contradiction_shadow


def _desk(**artifacts) -> SharedDesk:
    desk = SharedDesk(cycle_id="test-cycle-1234567890", ticker="AAPL")
    for atype, art in artifacts.items():
        setattr(desk, atype, art)
    return desk


class TestDirectionalContradiction:
    def test_fundamental_bull_vs_quant_bear_is_flagged(self):
        desk = _desk(
            fundamental_report={"thesis_direction": "BULLISH", "confidence": 70},
            quant_report={"thesis_direction": "BEARISH", "confidence": 65},
            final_decision={"action": "BUY", "confidence": 60},
        )
        report = compute_contradiction_shadow(desk)
        assert report["contradiction_count"] >= 1
        assert report["sentiment_by_source"]["fundamental_report"] == "BULLISH"
        assert report["sentiment_by_source"]["quant_report"] == "BEARISH"
        # BUY final action on top of an unresolved directional split is exactly
        # what a downgrade-to-HOLD gate would catch.
        assert report["would_downgrade_to_hold"] is True

    def test_board_action_contradicts_analysts(self):
        # Both analysts bearish, but the board bought anyway.
        desk = _desk(
            fundamental_report={"thesis_direction": "BEARISH", "confidence": 70},
            quant_report={"thesis_direction": "BEARISH", "confidence": 60},
            final_decision={"action": "BUY", "confidence": 55},
        )
        report = compute_contradiction_shadow(desk)
        assert report["contradiction_count"] >= 1
        assert report["would_downgrade_to_hold"] is True

    def test_agreement_produces_no_contradiction(self):
        desk = _desk(
            fundamental_report={"thesis_direction": "BULLISH", "confidence": 70},
            quant_report={"thesis_direction": "BULLISH", "confidence": 65},
            final_decision={"action": "BUY", "confidence": 72},
        )
        report = compute_contradiction_shadow(desk)
        assert report["contradiction_count"] == 0
        assert report["would_downgrade_to_hold"] is False

    def test_bull_bear_debate_artifacts_are_not_counted(self):
        # Bull argues bullish, bear argues bearish — adversarial by design.
        # On their own (no independent analyst split) this must NOT flag.
        desk = _desk(
            bull_argument={"thesis_direction": "BULLISH", "confidence": 80},
            bear_rebuttal={"thesis_direction": "BEARISH", "confidence": 80},
            fundamental_report={"thesis_direction": "BULLISH", "confidence": 70},
            quant_report={"thesis_direction": "BULLISH", "confidence": 68},
            final_decision={"action": "BUY", "confidence": 70},
        )
        report = compute_contradiction_shadow(desk)
        assert report["contradiction_count"] == 0
        # Debate artifacts never entered the sentiment map.
        assert "bull_argument" not in report["sentiment_by_source"]
        assert "bear_rebuttal" not in report["sentiment_by_source"]


class TestPriceTargetContradiction:
    def test_divergent_price_targets_flagged(self):
        desk = _desk(
            fundamental_report={
                "thesis_direction": "BULLISH", "confidence": 70, "take_profit": 300.0,
            },
            quant_report={
                "thesis_direction": "BULLISH", "confidence": 60, "take_profit": 120.0,
            },
        )
        report = compute_contradiction_shadow(desk)
        # >2x divergence (300 vs 120) is a contradiction; directions agree so
        # only the price-target contradiction should appear.
        descriptions = " ".join(c["description"] for c in report["contradictions"])
        assert "Price targets" in descriptions


class TestSafety:
    def test_shadow_does_not_mutate_decision(self):
        final = {"action": "BUY", "confidence": 60}
        desk = _desk(
            fundamental_report={"thesis_direction": "BULLISH", "confidence": 70},
            quant_report={"thesis_direction": "BEARISH", "confidence": 65},
            final_decision=final,
        )
        compute_contradiction_shadow(desk)
        # Decision object is untouched — shadow is observation-only.
        assert desk.final_decision["action"] == "BUY"
        assert desk.final_decision["confidence"] == 60

    def test_empty_desk_is_safe(self):
        desk = _desk()
        report = compute_contradiction_shadow(desk)
        assert report["contradiction_count"] == 0
        assert report["claims_extracted"] == 0
        assert "error" not in report
