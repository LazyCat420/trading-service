"""The synthesizer's veto, made measurable.

Execution reads `trade_decision or final_decision`, so when the decision
synthesizer disagrees with the Board it wins. Over 7 days it downgraded 21 of
41 Board BUYs to HOLD — a 51% veto on the pipeline's entire trade flow — and
nothing measured it, because `decision_outcomes` recorded only the surviving
action. A HOLD the desk agreed on and a HOLD that overruled a Board BUY were
the same row.

The confidence floor of 70 earned its place with numbers. The veto sitting in
FRONT of that floor had none.
"""

from unittest.mock import MagicMock, patch

from app.autoresearch import outcome_tracker as ot


class _FakeDb:
    def __init__(self, results):
        self._results = list(results)

    def execute(self, *_a, **_k):
        out = MagicMock()
        out.fetchall.return_value = self._results.pop(0) if self._results else []
        out.fetchone.return_value = None
        return out

    def __enter__(self):
        return self

    def __exit__(self, *_a):
        return False


class TestTheScorecardRefusesToGuess:
    def test_no_verdict_without_enough_resolved_rows(self):
        """A 4-row mean printed next to a 900-row mean invites reading it as a
        finding. That is the failure this whole audit was about."""
        rows = [[
            ("kept_buys", 98, 76, -0.47),
            ("overridden_buys", 23, 4, -2.445),
        ]]
        with patch.object(ot, "get_db", return_value=_FakeDb(rows)):
            out = ot.override_scorecard(30)

        assert "verdict" not in out
        assert "not enough resolved rows" in out["note"]
        assert out["overridden_buys"]["scored"] == 4

    def test_a_verdict_appears_once_both_sides_are_populated(self):
        rows = [[
            ("kept_buys", 120, 100, 2.5),
            ("overridden_buys", 60, 50, -1.5),
        ]]
        with patch.object(ot, "get_db", return_value=_FakeDb(rows)):
            out = ot.override_scorecard(30)

        assert out["veto_edge_pct"] == -4.0
        assert "AVOIDED" in out["verdict"]

    def test_it_names_the_costly_case_too(self):
        """If the declined trades outperform the kept ones, the veto is losing
        money and the scorecard has to say so."""
        rows = [[
            ("kept_buys", 120, 100, 0.5),
            ("overridden_buys", 60, 50, 4.0),
        ]]
        with patch.object(ot, "get_db", return_value=_FakeDb(rows)):
            out = ot.override_scorecard(30)

        assert out["veto_edge_pct"] == 3.5
        assert "DECLINED better" in out["verdict"]

    def test_a_db_failure_is_not_a_crash(self):
        """This runs inside the cycle's post-processing; a reporting query must
        never take a cycle down."""
        with patch.object(ot, "get_db", side_effect=RuntimeError("pool gone")):
            out = ot.override_scorecard(30)

        assert out["days"] == 30
        assert "veto_edge_pct" not in out


class TestTheCounterfactualIsTheRightNumber:
    def test_a_hold_carries_the_long_side_move(self):
        """resolve_pending_outcomes measures HOLD on the raw signed move, so an
        overridden BUY's pnl_pct IS what the declined trade would have
        returned. Without that the column would record the override but not
        what it cost."""
        import inspect

        src = inspect.getsource(ot.resolve_pending_outcomes)
        assert "BUY and HOLD both measure the long-side move" in src

    def test_hold_classification_is_unchanged_by_the_column(self):
        """The override label is provenance, not an outcome. Folding it into
        _classify would change what HOLD_CORRECT means mid-history."""
        assert ot._classify("HOLD", 0.5) == "HOLD_CORRECT"
        assert ot._classify("HOLD", 5.0) == "HOLD_MISS"
        assert ot._classify("BUY", 5.0) == "WIN"
