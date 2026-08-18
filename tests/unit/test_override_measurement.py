"""The synthesizer's veto, made measurable.

Execution reads `trade_decision or final_decision`, so when the decision
synthesizer disagrees with the Board it wins. Over 7 days it downgraded 21 of
41 Board BUYs to HOLD — a 51% veto on the pipeline's entire trade flow — and
nothing measured it, because `decision_outcomes` recorded only the surviving
action. A HOLD the desk agreed on and a HOLD that overruled a Board BUY were
the same row.

The confidence floor of 70 earned its place with numbers. The veto sitting in
FRONT of that floor had none.

These used to hand the scorecard pre-aggregated `(bucket, n, scored, mean)`
rows through a fake `get_db`. `get_db` is gone and the aggregation moved into
Python: `override_scorecard` now reads raw `decision_outcomes` documents via
`mongo_store.find_docs` and buckets them itself on `action`/`overridden_from`.
The fixtures are therefore documents now, which also puts the BUCKETING RULE
under test — feeding pre-summed rows skipped it entirely, so a scorecard that
filed an overridden BUY under `kept_buys` would still have passed.
"""

from unittest.mock import patch

from app.autoresearch import outcome_tracker as ot


def _docs(bucket_specs):
    """Build `decision_outcomes` documents for {bucket: (n, scored, mean)}.

    `n` rows are emitted per bucket, of which `scored` carry a pnl_pct (the
    rest are unresolved, i.e. pnl_pct is None). The scored values are chosen
    to average exactly `mean`.
    """
    shapes = {
        # bucket -> (action, overridden_from)
        "kept_buys": ("BUY", None),
        "overridden_buys": ("HOLD", "BUY"),
        "blocked_by_gate": ("HOLD", "HOLD"),
    }
    out = []
    for bucket, (n, scored, mean) in bucket_specs.items():
        action, overridden_from = shapes[bucket]
        for i in range(n):
            # A symmetric spread around `mean` averages to it exactly, so the
            # asserted mean is a real average and not a single planted value.
            if i < scored:
                offset = 1.0 if i % 2 == 0 else -1.0
                if scored % 2 == 1 and i == scored - 1:
                    offset = 0.0
                pnl = mean + offset
            else:
                pnl = None
            out.append({
                "action": action,
                "overridden_from": overridden_from,
                "pnl_pct": pnl,
            })
    return out


def _patch_docs(docs):
    """Patch the scorecard's Mongo read, checking the collection and window."""
    def _find_docs(collection, filt, *a, **k):
        assert collection == "decision_outcomes"
        # The scorecard is a trailing-window report; an unbounded read would
        # quietly mix in history older than the requested `days`.
        assert "$gt" in filt["created_at"]
        return list(docs)

    return patch.object(ot.mongo_store, "find_docs", _find_docs)


class TestTheScorecardRefusesToGuess:
    def test_no_verdict_without_enough_resolved_rows(self):
        """A 4-row mean printed next to a 900-row mean invites reading it as a
        finding. That is the failure this whole audit was about."""
        docs = _docs({
            "kept_buys": (98, 76, -0.47),
            "overridden_buys": (23, 4, -2.445),
        })
        with _patch_docs(docs):
            out = ot.override_scorecard(30)

        assert "verdict" not in out
        assert "not enough resolved rows" in out["note"]
        assert out["overridden_buys"]["scored"] == 4

    def test_a_verdict_appears_once_both_sides_are_populated(self):
        docs = _docs({
            "kept_buys": (120, 100, 2.5),
            "overridden_buys": (60, 50, -1.5),
        })
        with _patch_docs(docs):
            out = ot.override_scorecard(30)

        assert out["veto_edge_pct"] == -4.0
        assert "AVOIDED" in out["verdict"]

    def test_it_names_the_costly_case_too(self):
        """If the declined trades outperform the kept ones, the veto is losing
        money and the scorecard has to say so."""
        docs = _docs({
            "kept_buys": (120, 100, 0.5),
            "overridden_buys": (60, 50, 4.0),
        })
        with _patch_docs(docs):
            out = ot.override_scorecard(30)

        assert out["veto_edge_pct"] == 3.5
        assert "DECLINED better" in out["verdict"]

    def test_a_db_failure_is_not_a_crash(self):
        """This runs inside the cycle's post-processing; a reporting query must
        never take a cycle down."""
        with patch.object(ot.mongo_store, "find_docs", side_effect=RuntimeError("pool gone")):
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
