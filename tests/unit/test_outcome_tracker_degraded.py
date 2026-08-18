"""A pipeline failure is not a trade and must never be scored as one.

2026-07-27: 363 of 2,215 `decision_outcomes` rows were confidence=0 artifacts —
145 reading "PIPELINE FAILURE (EMPTY_SIGNAL): Thesis returned confidence=0 with
0 claims", 198 reading "Failed to parse thesis. Invalid JSON format" — recorded,
resolved against price, and labelled WIN/LOSS like real calls.

They win 55.1% at -5.61% mean vs 61.1% / +1.94% for real decisions and all sit
at confidence 0, so they poisoned SkillOpt's baseline, the scorecard, and the
confidence calibration (where they invented a fake "low confidence loses money"
band that was really the crash rate).
"""

import pytest

from app.autoresearch.outcome_tracker import _is_unscoreable


class TestUnscoreable:
    def test_confidence_zero_is_the_sentinel(self):
        """Real scores bottom out at 15 in the recorded history; 0 is a
        sentinel every degraded path writes, not a dismal-but-real score."""
        assert _is_unscoreable(0, {"action": "BUY"}) is True
        assert _is_unscoreable(0.0, {"action": "SELL"}) is True

    def test_pipeline_failure_text_is_caught(self):
        result = {
            "action": "SELL",
            "thesis_summary": (
                "PIPELINE FAILURE (EMPTY_SIGNAL): Thesis returned confidence=0 "
                "with 0 claims. Original action was SELL."
            ),
        }
        assert _is_unscoreable(45, result) is True

    def test_unparseable_thesis_is_caught(self):
        result = {
            "action": "SELL",
            "thesis_summary": "Failed to parse thesis. Invalid JSON format. Raw: # CBOE",
        }
        assert _is_unscoreable(45, result) is True

    def test_degraded_provenance_is_caught(self):
        """Reuses the orchestrator's own definition so the policy gate and the
        scorer cannot disagree about what counts as degraded."""
        from app.v3.orchestrator import _DEGRADED_PROVENANCE

        prov = next(iter(_DEGRADED_PROVENANCE))
        assert _is_unscoreable(60, {"action": "BUY", "decision_provenance": prov}) is True

    def test_null_action_sentinel_is_caught(self):
        assert _is_unscoreable(60, {"action": None}) is True

    def test_missing_confidence_is_unscoreable(self):
        assert _is_unscoreable(None, {"action": "BUY"}) is True

    def test_non_dict_artifact_is_unscoreable(self):
        assert _is_unscoreable(70, None) is True
        assert _is_unscoreable(70, "not a dict") is True

    # ── The boundary that matters ───────────────────────────────────────────
    # The filter must remove FAILURES, not LOSERS. A confident call that lost
    # money is the single most valuable row in this table: the whole
    # confidence calibration is built on low-confidence decisions being
    # genuinely worse than high-confidence ones. Dropping them would destroy
    # the finding the trade floor rests on.

    def test_real_low_confidence_decision_is_still_scored(self):
        assert _is_unscoreable(15, {"action": "BUY", "thesis_summary": "Weak setup"}) is False

    def test_confident_decision_is_scored(self):
        assert _is_unscoreable(74, {"action": "BUY", "thesis_summary": "Strong beat"}) is False

    def test_hold_is_still_scored(self):
        assert _is_unscoreable(58, {"action": "HOLD", "thesis_summary": "Waiting"}) is False

    def test_thesis_merely_mentioning_failure_words_is_not_enough(self):
        """Substring matching is deliberately anchored on the exact machine-
        written markers. A human-readable thesis discussing a company's
        failures is a real decision."""
        result = {
            "action": "SELL",
            "thesis_summary": "Management failure to parse market signals hurt Q2.",
        }
        assert _is_unscoreable(72, result) is False


@pytest.mark.parametrize("confidence", [0, 0.0])
def test_zero_beats_every_other_signal(confidence):
    """confidence=0 short-circuits before the text check, so an artifact whose
    thesis was lost entirely is still caught."""
    assert _is_unscoreable(confidence, {}) is True
