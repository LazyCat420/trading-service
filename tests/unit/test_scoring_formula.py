"""
Unit tests for the outcome-based decision quality scoring formula.

Tests the new `_audit_decisions()` function which scores based on actual
trade outcomes (WIN/LOSS/FLAT) rather than the broken action-ratio formula.

Unit tests use mocks — no NAS DB connection needed.
Integration tests use the NAS DB via conftest fixtures.
"""

import pytest
from unittest.mock import patch, MagicMock
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone


# ── Helper to mock get_db for autoresearch module ──

@contextmanager
def _mock_reads(confidence_rows=None, outcome_rows=None):
    """Patch both reads `_audit_decisions` makes, so neither reaches a store.

    Both now go through `mongo_query.find_rows`, so the stub dispatches on the
    COLLECTION name rather than on call order (dispatching on call_count was
    already fragile and could feed outcome_rows to the confidence query).

    `outcome_rows` fixtures still carry the decision AGE IN DAYS in the 5th
    slot, which is what the old `EXTRACT(EPOCH FROM (CURRENT_TIMESTAMP -
    created_at))/86400` column produced. The Mongo read selects `created_at`
    and the module derives the age itself, so the age is converted back into a
    created_at here — that keeps the module's own date arithmetic under test
    instead of handing it the answer.
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


class TestDecisionScoringFormula:
    """Test the decision quality scoring algorithm edge cases."""

    def _make_summary(self, buy=0, sell=0, hold=0):
        return {"buy_count": buy, "sell_count": sell, "hold_count": hold}

    def test_zero_decisions_returns_zero(self):
        """No decisions at all should score 0 with a critical issue."""
        with _mock_reads():
            from app.autoresearch.auditors.decision_audit import _audit_decisions
            result = _audit_decisions("test_cycle", self._make_summary(0, 0, 0))

        assert result["score"] == 0
        assert any(i["severity"] == "critical" for i in result["issues"])
        assert any("No decisions" in i["issue"] for i in result["issues"])

    def test_cold_start_defaults_to_neutral(self):
        """With < 3 resolved trades, score should default to ~0.5 (not 0.23)."""
        confs = [(60,), (70,), (45,)]
        outcomes = [("BUY", 70, 2.5, "WIN", 5.0)]  # Only 1 trade — cold start

        with _mock_reads(confs, outcomes):
            from app.autoresearch.auditors.decision_audit import _audit_decisions
            result = _audit_decisions("test_cycle", self._make_summary(2, 1, 8))

        assert result["score"] >= 0.4, f"Cold start score {result['score']} too low"
        assert result["score"] <= 0.6, f"Cold start score {result['score']} too high"
        assert result.get("outcome_stats", {}).get("scoring_method") in ("cold_start", "fallback_error")

    def test_all_hold_mild_penalty_in_cold_start(self):
        """100% HOLD cycle should get ~0.4 in cold start (mild, not 0%)."""
        confs = [(50,), (45,), (55,)]
        outcomes = []  # No resolved trades

        with _mock_reads(confs, outcomes):
            from app.autoresearch.auditors.decision_audit import _audit_decisions
            result = _audit_decisions("test_cycle", self._make_summary(0, 0, 10))

        assert result["score"] >= 0.3, f"All-HOLD score {result['score']} is too punishing"
        assert result["score"] <= 0.5

    def test_high_win_rate_scores_well(self):
        """60% win rate with good risk mgmt should score high."""
        confs = [(70,), (65,), (80,), (55,), (75,)]
        # 3 wins, 2 losses → 60% win rate
        outcomes = [
            ("BUY", 80, 5.0, "WIN", 3.0),
            ("BUY", 70, 3.0, "WIN", 4.0),
            ("BUY", 75, 4.0, "WIN", 2.0),
            ("BUY", 55, -2.0, "LOSS", 5.0),
            ("BUY", 60, -1.5, "LOSS", 3.0),
        ]

        with _mock_reads(confs, outcomes):
            from app.autoresearch.auditors.decision_audit import _audit_decisions
            result = _audit_decisions("test_cycle", self._make_summary(5, 0, 6))

        assert result["score"] >= 0.55, f"60% win rate scored {result['score']}, should be >= 0.55"
        stats = result.get("outcome_stats", {})
        assert stats.get("scoring_method") == "outcome_based"
        assert stats.get("win_rate") == pytest.approx(0.6, abs=0.01)

    def test_low_win_rate_scores_poorly(self):
        """20% win rate should score low and flag critical issue."""
        confs = [(70,), (65,), (80,), (55,), (75,)]
        # 1 win, 4 losses → 20% win rate
        outcomes = [
            ("BUY", 80, 2.0, "WIN", 3.0),
            ("BUY", 70, -3.0, "LOSS", 4.0),
            ("BUY", 75, -4.0, "LOSS", 2.0),
            ("BUY", 55, -2.0, "LOSS", 5.0),
            ("BUY", 60, -1.5, "LOSS", 3.0),
        ]

        with _mock_reads(confs, outcomes):
            from app.autoresearch.auditors.decision_audit import _audit_decisions
            result = _audit_decisions("test_cycle", self._make_summary(5, 0, 6))

        assert result["score"] < 0.4, f"20% win rate scored {result['score']}, should be < 0.4"
        assert any("win rate" in i["issue"].lower() for i in result["issues"])

    def test_single_low_conf_trade_cannot_zero_calibration(self):
        """One lucky low-confidence WIN must not zero the calibration term.

        Regression: cycle-v3-1784434883 scored 39.4 because a single conf-25
        WIN made low-conf win rate 100% vs high-conf 45%, clamping the entire
        30%-weight calibration term to 0.
        """
        confs = [(70,), (75,), (80,)]
        # 5 high-conf decided (3W/2L) + ONE low-conf win
        outcomes = [
            ("BUY", 80, 5.0, "WIN", 3.0),
            ("BUY", 70, 3.0, "WIN", 4.0),
            ("BUY", 75, 4.0, "WIN", 2.0),
            ("BUY", 72, -2.0, "LOSS", 5.0),
            ("BUY", 78, -1.5, "LOSS", 3.0),
            ("SELL", 25, 8.9, "WIN", 30.0),  # the n=1 lucky low-conf trade
        ]

        with _mock_reads(confs, outcomes):
            from app.autoresearch.auditors.decision_audit import _audit_decisions
            result = _audit_decisions("test_cycle", self._make_summary(5, 1, 0))

        cal = result["outcome_stats"]["calibration_score"]
        # No decile bucket reaches MIN_BUCKET and the low-conf side is n=1, so
        # both honesty and discrimination stay neutral (0.5) — the lucky trade
        # must not drag the term toward 0.
        assert cal == pytest.approx(0.5, abs=0.01), f"calibration {cal} — n=1 bucket leaked into the formula"

    def test_honest_confidence_scores_high_ece(self):
        """Stated confidence matching realized win rate → high honesty term.

        10 trades all stated conf 70, 7 wins → ECE = 0 → honesty 1.0. With no
        low-conf bucket, discrimination stays neutral: cal = 0.7*1 + 0.3*0.5.
        Uniform confidence must cap at 0.85 — full credit requires
        differentiating AND being right.
        """
        confs = [(70,)]
        outcomes = (
            [("BUY", 70, 5.0, "WIN", 3.0)] * 7 + [("BUY", 70, -2.0, "LOSS", 3.0)] * 3
        )

        with _mock_reads(confs, outcomes):
            from app.autoresearch.auditors.decision_audit import _audit_decisions
            result = _audit_decisions("test_cycle", self._make_summary(10, 0, 0))

        stats = result["outcome_stats"]
        assert stats["calibration_ece"] == pytest.approx(0.0, abs=0.01)
        assert stats["calibration_score"] == pytest.approx(0.85, abs=0.01)

    def test_overconfident_bucket_penalized_ece(self):
        """Stating 90 while winning 40% → large ECE → low honesty term."""
        confs = [(90,)]
        outcomes = (
            [("BUY", 90, 5.0, "WIN", 3.0)] * 4 + [("BUY", 90, -3.0, "LOSS", 3.0)] * 6
        )

        with _mock_reads(confs, outcomes):
            from app.autoresearch.auditors.decision_audit import _audit_decisions
            result = _audit_decisions("test_cycle", self._make_summary(10, 0, 0))

        stats = result["outcome_stats"]
        assert stats["calibration_ece"] == pytest.approx(0.5, abs=0.01)
        assert stats["calibration_honesty"] == pytest.approx(0.0, abs=0.01)
        assert any("miscalibrated" in i["issue"].lower() for i in result["issues"])

    def test_win_rate_benchmarked_at_60pct(self):
        """60% ex-flat win rate = full win-rate credit (raw scale needed 100%)."""
        confs = [(70,)]
        outcomes = (
            [("BUY", 70, 5.0, "WIN", 3.0)] * 6 + [("BUY", 70, -2.5, "LOSS", 3.0)] * 4
        )

        with _mock_reads(confs, outcomes):
            from app.autoresearch.auditors.decision_audit import _audit_decisions
            result = _audit_decisions("test_cycle", self._make_summary(10, 0, 0))

        # wr 0.6/0.6 → 0.4 full; cal: ece=|0.7-0.6|=0.1 → honesty 0.8, disc 0.5
        # → 0.71*0.3 = 0.213; risk: pf 5/2.5 = 2 → 0.3. Total ≈ 0.913.
        assert result["score"] >= 0.9, f"benchmark-excellent desk scored {result['score']}"

    def test_flats_excluded_from_win_rate(self):
        """FLAT outcomes are 'no verdict' — they must not count as losses."""
        confs = [(70,), (75,)]
        # 3W/2L decided + 5 FLATs: ex-flat wr = 0.6, naive wr would be 0.3
        outcomes = [
            ("BUY", 80, 5.0, "WIN", 3.0),
            ("BUY", 70, 3.0, "WIN", 4.0),
            ("BUY", 75, 4.0, "WIN", 2.0),
            ("BUY", 72, -2.0, "LOSS", 5.0),
            ("BUY", 78, -1.5, "LOSS", 3.0),
            ("SELL", 71, 0.0, "FLAT", 6.0),
            ("SELL", 74, 0.0, "FLAT", 6.0),
            ("SELL", 76, -0.1, "FLAT", 6.0),
            ("SELL", 73, 0.1, "FLAT", 6.0),
            ("SELL", 77, 0.0, "FLAT", 6.0),
        ]

        with _mock_reads(confs, outcomes):
            from app.autoresearch.auditors.decision_audit import _audit_decisions
            result = _audit_decisions("test_cycle", self._make_summary(5, 5, 0))

        stats = result["outcome_stats"]
        assert stats["win_rate"] == pytest.approx(0.6, abs=0.01)
        assert stats["flats"] == 5
        assert not any("Low win rate" in i["issue"] for i in result["issues"])

    def test_stale_cohort_flagged(self):
        """Median decision age > 14d should surface an info issue, not distort score."""
        confs = [(70,)]
        outcomes = [
            ("BUY", 80, 5.0, "WIN", 35.0),
            ("BUY", 70, 3.0, "WIN", 32.0),
            ("BUY", 75, -2.0, "LOSS", 30.0),
        ]

        with _mock_reads(confs, outcomes):
            from app.autoresearch.auditors.decision_audit import _audit_decisions
            result = _audit_decisions("test_cycle", self._make_summary(3, 0, 0))

        stale = [i for i in result["issues"] if "Stale cohort" in i["issue"]]
        assert stale and stale[0]["severity"] == "info"
        assert result["outcome_stats"]["median_decision_age_days"] == pytest.approx(32.0, abs=0.1)

    def test_outcome_stats_always_present(self):
        """The result dict should always include outcome_stats key."""
        with _mock_reads():
            from app.autoresearch.auditors.decision_audit import _audit_decisions
            result = _audit_decisions("test_cycle", self._make_summary(1, 0, 5))

        assert "outcome_stats" in result
        assert "scoring_method" in result["outcome_stats"]

    def test_db_error_falls_back_gracefully(self):
        """If DB query fails, score should fallback to 0.5 not crash."""
        with patch("app.autoresearch.auditors.decision_audit.mongo_query") as mq:
            mq.find_rows.side_effect = Exception("Connection refused")
            from app.autoresearch.auditors.decision_audit import _audit_decisions
            result = _audit_decisions("test_cycle", self._make_summary(2, 1, 8))

        assert result["score"] == 0.5
        assert result["outcome_stats"]["scoring_method"] == "fallback_error"


@pytest.mark.integration
class TestScoringIntegration:
    """Integration tests using the NAS database.

    These use the `patch_real_get_db` fixture from conftest.py which
    points to the NAS at 10.0.0.16:5433.
    """

    @pytest.fixture(autouse=True)
    def _skip_if_no_nas(self, real_test_db_engine):
        """Skip if NAS DB is not reachable."""
        if not real_test_db_engine:
            pytest.skip("NAS database not available")

    def test_outcome_count_matches_reality(self, patch_real_get_db):
        """Verify resolved trade count matches what's in the NAS DB (LIMIT 100 window)."""
        actual_count = patch_real_get_db.execute(
            """
            SELECT COUNT(*) FROM decision_outcomes
            WHERE resolved_at IS NOT NULL
              AND outcome != 'CANCELED'
              AND resolved_at > CURRENT_TIMESTAMP - INTERVAL '30 days'
            """
        ).fetchone()[0]

        from app.autoresearch.auditors.decision_audit import _audit_decisions
        result = _audit_decisions("integration_test", {"buy_count": 1, "sell_count": 0, "hold_count": 5})

        stats = result.get("outcome_stats", {})
        if actual_count >= 3:
            assert stats.get("scoring_method") == "outcome_based"
            assert stats.get("total_resolved") == min(actual_count, 100)
        else:
            assert stats.get("scoring_method") in ("cold_start", "fallback_error")

    def test_win_rate_matches_manual_sql(self, patch_real_get_db):
        """Verify computed ex-flat win rate matches direct SQL over the same window."""
        rows = patch_real_get_db.execute(
            """
            SELECT outcome FROM decision_outcomes
            WHERE resolved_at IS NOT NULL
              AND outcome != 'CANCELED'
              AND resolved_at > CURRENT_TIMESTAMP - INTERVAL '30 days'
            ORDER BY resolved_at DESC LIMIT 100
            """
        ).fetchall()

        decided = [r for r in rows if r[0] in ("WIN", "LOSS")]
        if len(rows) < 3 or not decided:
            pytest.skip("Not enough resolved trades for win rate test")

        manual_wins = sum(1 for r in decided if r[0] == "WIN")
        manual_rate = manual_wins / len(decided)

        from app.autoresearch.auditors.decision_audit import _audit_decisions
        result = _audit_decisions("integration_test", {"buy_count": 1, "sell_count": 0, "hold_count": 5})
        computed_rate = result.get("outcome_stats", {}).get("win_rate", -1)

        assert abs(computed_rate - manual_rate) < 0.01, (
            f"Win rate mismatch: computed={computed_rate}, manual={manual_rate}"
        )
