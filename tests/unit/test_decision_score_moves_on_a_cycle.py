"""The decision-quality score must be able to respond to the cycle it audits.

Through v5 every term was a 30-day rolling cohort, so the headline number was
structurally incapable of moving on a single cycle. Measured 2026-09-04 from
autoresearch_reports: it printed 82.4 for twelve consecutive cycles, and 86.5
for seven before that. The per-cycle judge subscore -- the one signal that does
move -- was computed, rendered as a headline on the panel, and multiplied by
nothing.

v6 gives it PER_CYCLE_JUDGE_WEIGHT. These tests pin the property (two cycles
that differ only in their judge grade must not score identically), the bound
(it must not dominate), and the refusal (no judged decisions must not invent a
number).
"""

import pytest

import app.autoresearch.auditors.decision_audit as da
from app.autoresearch.auditors.decision_audit import SCORE_VERSION, _audit_decisions

# Resolved at call time, not import time: a `from ... import
# PER_CYCLE_JUDGE_WEIGHT` makes this whole file ERROR at collection on the
# pre-fix module, and a collection error is a weaker control than a failing
# assertion — it proves a constant is new, not that the behaviour changed.
PER_CYCLE_JUDGE_WEIGHT = getattr(da, "PER_CYCLE_JUDGE_WEIGHT", 0.0)


class _Q:
    """Fixed 30d cohort; only the per-cycle judge grade varies."""

    def __init__(self, judge_avg, judged_n=2):
        self.judge_avg = judge_avg
        self.judged_n = judged_n

    def find_rows(self, collection, query, columns, sort=None, limit=0, **kw):
        if collection != "decision_outcomes":
            return []
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc)
        rows = []
        for i in range(30):
            outcome = "WIN" if i % 3 == 0 else ("LOSS" if i % 3 == 1 else "HOLD_CORRECT")
            pnl = 3.0 if outcome == "WIN" else (-2.0 if outcome == "LOSS" else 0.2)
            action = "BUY" if outcome in ("WIN", "LOSS") else "HOLD"
            rows.append((action, 70, pnl, outcome, now))
        return rows

    def agg_row(self, collection, query, aggs, **kw):
        if collection == "decision_evaluations":
            if self.judge_avg is None:
                return (None, 0)
            return (self.judge_avg, self.judged_n)
        return tuple(0 if op.startswith("count") else None for op, _ in aggs)

    def count(self, *a, **k):
        return 0


def _score(monkeypatch, judge_avg, judged_n=2):
    monkeypatch.setattr(da, "mongo_query", _Q(judge_avg, judged_n))
    return _audit_decisions("cycle-test", {"buy_count": 1, "sell_count": 0, "hold_count": 1})


def test_the_version_was_bumped_with_the_formula():
    """A formula change without a version stamp is indistinguishable from real
    movement — the file's own v3->v4->v5 history says so."""
    assert SCORE_VERSION.startswith("v") and int(SCORE_VERSION[1:]) >= 6


def test_two_cycles_differing_only_in_judge_grade_do_not_tie(monkeypatch):
    good = _score(monkeypatch, 5.0)["score"]
    bad = _score(monkeypatch, 1.0)["score"]
    assert good != bad, "the score still cannot move on a cycle — this is the defect"
    assert good > bad


def test_the_judge_term_cannot_dominate(monkeypatch):
    """A perfect grade must not paper over a bad outcome cohort, and vice versa."""
    # Assert the weight EXISTS before comparing against it: the getattr
    # fallback is 0.0, and `spread == approx(0.0)` is true on the pre-fix
    # module too — a check that passes in both states is not a check.
    assert 0.0 < PER_CYCLE_JUDGE_WEIGHT < 0.5, (
        "the judge term carries no weight, or carries a majority of it"
    )
    good = _score(monkeypatch, 5.0)
    bad = _score(monkeypatch, 0.0)
    spread = good["score"] - bad["score"]
    assert spread == pytest.approx(PER_CYCLE_JUDGE_WEIGHT, abs=0.01)


def test_no_judged_decisions_leaves_the_outcome_component_alone(monkeypatch):
    """No evidence must not be redistributed into a guess."""
    out = _score(monkeypatch, None)
    assert out["per_cycle_judge_score"] is None
    assert out["per_cycle_judge_weight"] == 0.0
    assert out["score"] == pytest.approx(out["outcome_component"])


def test_the_outcome_component_is_still_reported(monkeypatch):
    """The rolling half stays visible so a move can be attributed."""
    out = _score(monkeypatch, 4.0)
    assert 0.0 <= out["outcome_component"] <= 1.0
    assert out["per_cycle_judge_n"] == 2


def test_a_garbage_judge_read_does_not_become_a_grade(monkeypatch):
    """Surviving float() is not the same as being a measurement.

    float(MagicMock()) is 1.0, so a fully mocked store scored a perfect 20/100
    judge result and silently moved the headline number — caught when this
    change first ran the existing test_scoring_formula suite. Now that the term
    carries weight, the read is type-checked against the judge's own 0-5 scale.
    """
    from unittest.mock import MagicMock

    class _Garbage(_Q):
        def agg_row(self, collection, query, aggs, **kw):
            if collection == "decision_evaluations":
                return MagicMock()
            return super().agg_row(collection, query, aggs, **kw)

    monkeypatch.setattr(da, "mongo_query", _Garbage(None))
    out = _audit_decisions("cycle-test", {"buy_count": 1, "sell_count": 0, "hold_count": 1})
    assert out["per_cycle_judge_score"] is None
    assert out["score"] == pytest.approx(out["outcome_component"])


def test_an_out_of_scale_judge_value_is_refused(monkeypatch):
    """The judge grades 0-5. A 94 is a rescaled number that already went through
    the *20 conversion somewhere else, and must not go through it twice."""
    monkeypatch.setattr(da, "mongo_query", _Q(94.0))
    out = _audit_decisions("cycle-test", {"buy_count": 1, "sell_count": 0, "hold_count": 1})
    assert out["per_cycle_judge_score"] is None
