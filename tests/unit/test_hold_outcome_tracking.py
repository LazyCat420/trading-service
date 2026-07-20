"""HOLD outcome tracking (score_version v4).

HOLDs were never outcome-tracked, which discarded ~75% of fleet verdicts.
They now resolve as HOLD_CORRECT/HOLD_MISS claims that feed calibration and a
hold-accuracy metric — but must stay OUT of the directional win rate, where
"price stayed flat" would let low volatility masquerade as skill.

Unit tests use mocks — no NAS DB connection needed.
"""

from contextlib import contextmanager
from unittest.mock import MagicMock, patch

from app.autoresearch.outcome_tracker import _classify


def _patch_db(confidence_rows=None, outcome_rows=None):
    # The call counter must span get_db() contexts: _audit_decisions opens one
    # context for the confidence query and another for the outcomes query.
    calls = {"n": 0}

    @contextmanager
    def factory():
        conn = MagicMock()

        def execute_side_effect(*args, **kwargs):
            calls["n"] += 1
            cursor = MagicMock()
            cursor.fetchall.return_value = (
                (confidence_rows or []) if calls["n"] == 1 else (outcome_rows or [])
            )
            return cursor

        conn.execute.side_effect = execute_side_effect
        yield conn

    return patch("app.autoresearch.auditors.decision_audit.get_db", factory)


class TestClassify:
    def test_hold_inside_band_is_correct(self):
        assert _classify("HOLD", 0.4) == "HOLD_CORRECT"
        assert _classify("HOLD", -0.99) == "HOLD_CORRECT"

    def test_hold_outside_band_is_miss_either_direction(self):
        assert _classify("HOLD", 1.0) == "HOLD_MISS"
        assert _classify("HOLD", -2.5) == "HOLD_MISS"

    def test_directional_taxonomy_unchanged(self):
        assert _classify("BUY", 1.2) == "WIN"
        assert _classify("BUY", -1.2) == "LOSS"
        assert _classify("SELL", 0.2) == "FLAT"


class TestHoldScoring:
    """Rows are (action, confidence, pnl_pct, outcome, age_days)."""

    def _run(self, outcome_rows):
        with _patch_db(outcome_rows=outcome_rows):
            from app.autoresearch.auditors.decision_audit import _audit_decisions

            return _audit_decisions(
                "cycle-test", {"buy_count": 2, "sell_count": 1, "hold_count": 5}
            )

    def test_holds_do_not_move_the_directional_win_rate(self):
        directional = (
            [("BUY", 70, 2.0, "WIN", 5.0)] * 6 + [("BUY", 70, -2.0, "LOSS", 5.0)] * 6
        )
        holds = [("HOLD", 65, 0.2, "HOLD_CORRECT", 5.0)] * 20

        base = self._run(list(directional))
        with_holds = self._run(directional + holds)

        # 20 perfectly-correct holds must not inflate a 50% directional record.
        assert with_holds["outcome_stats"]["win_rate"] == base["outcome_stats"]["win_rate"] == 0.5

    def test_hold_accuracy_surfaced(self):
        rows = (
            [("BUY", 70, 2.0, "WIN", 5.0)] * 4
            + [("HOLD", 60, 0.1, "HOLD_CORRECT", 5.0)] * 3
            + [("HOLD", 60, 3.0, "HOLD_MISS", 5.0)] * 1
        )
        stats = self._run(rows)["outcome_stats"]
        assert stats["holds_correct"] == 3
        assert stats["holds_miss"] == 1
        assert stats["hold_accuracy"] == 0.75

    def test_holds_join_the_calibration_cohort(self):
        # 10 decided rows at conf 70 with 50% wins → ECE contribution |0.7-0.5|.
        # Adding 10 holds at conf 70, all correct, lifts that bucket's realized
        # rate to 75% → ECE must SHRINK if holds are in the cohort.
        decided = (
            [("BUY", 70, 2.0, "WIN", 5.0)] * 5 + [("BUY", 70, -2.0, "LOSS", 5.0)] * 5
        )
        holds = [("HOLD", 70, 0.1, "HOLD_CORRECT", 5.0)] * 10

        ece_without = self._run(list(decided))["outcome_stats"]["calibration_ece"]
        ece_with = self._run(decided + holds)["outcome_stats"]["calibration_ece"]
        assert ece_with < ece_without

    def test_score_version_stamped(self):
        rows = [("BUY", 70, 2.0, "WIN", 5.0)] * 4
        result = self._run(rows)
        assert result["score_version"] == "v4"
        assert result["outcome_stats"]["score_version"] == "v4"
        assert result["outcome_stats"]["cohort_n"] == 4
        assert result["outcome_stats"]["cohort_window_days"] == 30

    def test_bad_hold_accuracy_flags_issue(self):
        rows = (
            [("BUY", 70, 2.0, "WIN", 5.0)] * 3
            + [("HOLD", 60, 3.0, "HOLD_MISS", 5.0)] * 8
            + [("HOLD", 60, 0.1, "HOLD_CORRECT", 5.0)] * 2
        )
        issues = self._run(rows)["issues"]
        assert any("HOLD calls miss" in i["issue"] for i in issues)
