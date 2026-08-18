"""HOLD outcome tracking (score_version v4).

HOLDs were never outcome-tracked, which discarded ~75% of fleet verdicts.
They now resolve as HOLD_CORRECT / HOLD_AVOIDED_DECLINE / HOLD_MISS claims that
feed calibration and a hold-accuracy metric — but must stay OUT of the
directional win rate, where "price stayed flat" would let low volatility
masquerade as skill.

HOLD grading is DIRECTION-AWARE (2026-08-08): the book is long-only, so only a
rally is forgone. A hold through a decline is HOLD_AVOIDED_DECLINE and counts
as correct.

Unit tests use mocks — no NAS DB connection needed.
"""

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

from app.autoresearch.outcome_tracker import _classify


@contextmanager
def _patch_db(confidence_rows=None, outcome_rows=None):
    """Stub both of `_audit_decisions`' reads at the mongo_query boundary.

    Both are `find_rows` now, so the stub dispatches on the COLLECTION name
    rather than a call counter (the counter assumed one get_db context per
    query and broke as soon as that stopped being true).

    `outcome_rows` fixtures carry the decision AGE IN DAYS in the 5th slot —
    what the old EXTRACT(EPOCH ...)/86400 column returned. The Mongo read
    selects `created_at` and the module derives the age, so the age is turned
    back into a created_at here.
    """
    now = datetime.now(timezone.utc)

    def _find_rows(collection, query, columns, sort=None, limit=0):
        if collection == 'decision_outcomes':
            return [
                (action, conf, pnl, outcome,
                 None if age is None else now - timedelta(days=age))
                for action, conf, pnl, outcome, age in (outcome_rows or [])
            ]
        return confidence_rows or []

    with patch("app.autoresearch.auditors.decision_audit.mongo_query") as mq:
        mq.find_rows.side_effect = _find_rows
        yield


class TestClassify:
    def test_hold_inside_band_is_correct(self):
        assert _classify("HOLD", 0.4) == "HOLD_CORRECT"
        assert _classify("HOLD", -0.99) == "HOLD_CORRECT"

    def test_hold_through_a_rally_is_a_miss(self):
        """The only thing a HOLD forgoes on a long-only book: upside."""
        assert _classify("HOLD", 1.0) == "HOLD_MISS"
        assert _classify("HOLD", 12.0) == "HOLD_MISS"

    def test_hold_through_a_decline_is_correct_not_a_miss(self):
        """This assertion used to read `== "HOLD_MISS"`.

        Grading a hold on |pnl| is direction-BLIND, and this desk can only buy
        — there was no short to place, so sitting out a fall was RIGHT. On the
        154 graded HOLD_MISS rows on record, 69 had fallen; calling them misses
        put hold accuracy at 28% when it is 60%.
        """
        assert _classify("HOLD", -1.0) == "HOLD_AVOIDED_DECLINE"
        assert _classify("HOLD", -2.5) == "HOLD_AVOIDED_DECLINE"
        assert _classify("HOLD", -33.0) == "HOLD_AVOIDED_DECLINE"

    def test_the_band_edges_land_on_exactly_one_label(self):
        """No pnl may fall through the classifier unlabelled or double-labelled."""
        assert _classify("HOLD", 0.999) == "HOLD_CORRECT"
        assert _classify("HOLD", -0.999) == "HOLD_CORRECT"
        assert _classify("HOLD", 1.0) == "HOLD_MISS"
        assert _classify("HOLD", -1.0) == "HOLD_AVOIDED_DECLINE"
        assert _classify("HOLD", 0.0) == "HOLD_CORRECT"

    def test_every_hold_label_has_a_weight_in_the_scorecard(self):
        """`scorecard._weighted` SKIPS outcomes it has no weight for, so a label
        the scorecard has not learned about leaves the hold component's n short
        instead of failing. Enumerate from the classifier, not by hand."""
        from app.autoresearch.scorecard import _HOLD_WEIGHTS

        produced = {_classify("HOLD", p) for p in
                    (0.0, 0.5, -0.5, 1.0, 5.0, -1.0, -5.0)}
        assert produced == {"HOLD_CORRECT", "HOLD_MISS", "HOLD_AVOIDED_DECLINE"}
        assert produced <= set(_HOLD_WEIGHTS), (
            f"scorecard._HOLD_WEIGHTS is missing {produced - set(_HOLD_WEIGHTS)}"
        )
        assert _HOLD_WEIGHTS["HOLD_AVOIDED_DECLINE"] == 1.0

    def test_directional_taxonomy_unchanged(self):
        """Directional grading was ALREADY direction-aware — only HOLD changed."""
        assert _classify("BUY", 1.2) == "WIN"
        assert _classify("BUY", -1.2) == "LOSS"
        assert _classify("SELL", 0.2) == "FLAT"
        assert _classify("SELL", 1.2) == "WIN"
        assert _classify("SELL", -1.2) == "LOSS"


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
        # Assert against the constant, not a literal. Pinning "v4" here made
        # every legitimate bump look like a regression while testing nothing
        # the constant does not already say; the point of this test is that
        # the stamp REACHES both payloads. The current value is pinned once,
        # deliberately, in test_confidence_calibration.py.
        from app.autoresearch.auditors.decision_audit import SCORE_VERSION

        rows = [("BUY", 70, 2.0, "WIN", 5.0)] * 4
        result = self._run(rows)
        assert result["score_version"] == SCORE_VERSION
        assert result["outcome_stats"]["score_version"] == SCORE_VERSION
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
