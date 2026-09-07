"""The dashboard payload: a missing outcome must never render as "no change".

The five columns the plan promises are only useful if an unmeasured value is
distinguishable from a measured zero. That distinction is the whole file.
"""

from datetime import datetime, timedelta, timezone

import pytest

from app.routers import watch_allocator_router as war

NOW = datetime(2026, 9, 6, 15, 0, tzinfo=timezone.utc)


def _row(**over):
    base = {
        "id": "wtl-1", "created_at": NOW, "mode": 1, "ticker": "C",
        "watch_id": "w1", "schema_version": 1, "has_resolution_condition": True,
        "open_question": "Does Q3 NIM hold?", "trigger_type": "news",
        "event_key": "k1", "evidence": {"kind": "news", "observed_at": NOW},
        "catalyst": {}, "eligible": True, "reject_reason": None, "score": 2.4,
        "score_floor": 1.2, "components": {"materiality": 0.5},
        "would_action": "ANALYZE_NOW", "would_reason": "eligible_and_affordable",
        "clamps": [], "fired": True, "cycle_id": "wd-1", "outcome": None,
        "budget_left": 5, "analyses_this_week": 1,
    }
    base.update(over)
    return base


class TestOutcomeState:
    def test_a_fired_unscored_row_is_PENDING_not_unchanged(self):
        s = war._summarise([_row(fired=True, outcome=None)])
        assert s["outcomes_pending"] == 1
        assert s["decision_changed"] == 0
        # The rate is None, not 0.0 — nothing has been measured yet.
        assert s["decision_change_rate"] is None

    def test_a_deferred_row_is_not_applicable_not_pending(self):
        s = war._summarise([_row(fired=False, outcome=None)])
        assert s["outcomes_pending"] == 0 and s["deferred"] == 1

    def test_a_scored_row_counts(self):
        s = war._summarise([_row(outcome={"decision_changed": True,
                                          "woke_action": "BUY"})])
        assert s["outcomes_scored"] == 1 and s["decision_changed"] == 1
        assert s["decision_change_rate"] == 1.0

    def test_a_rate_over_an_empty_denominator_is_none_not_zero(self):
        """A confident 0.0 closes a question a blank would have kept open."""
        s = war._summarise([_row(fired=False)])
        assert s["decision_change_rate"] is None
        assert s["no_decision_rate"] is None

    def test_an_empty_window_reports_n_zero_not_a_fabricated_summary(self):
        assert war._summarise([]) == {"n": 0}


class TestWhyItDeferred:
    def test_deferral_reasons_are_counted_and_ranked(self):
        rows = [_row(fired=False, reject_reason="evidence_off_thesis"),
                _row(fired=False, reject_reason="evidence_off_thesis"),
                _row(fired=False, reject_reason="below_score_floor")]
        s = war._summarise(rows)
        assert list(s["deferral_reasons"]) == ["evidence_off_thesis", "below_score_floor"]
        assert s["deferral_reasons"]["evidence_off_thesis"] == 2

    def test_a_deferral_with_no_reason_is_labelled_not_dropped(self):
        """Dropping it would make the reason histogram sum to less than the
        deferral count, and nothing would say why."""
        s = war._summarise([_row(fired=False, reject_reason=None, would_reason=None)])
        assert s["deferral_reasons"] == {"unknown": 1}
        assert sum(s["deferral_reasons"].values()) == s["deferred"]

    def test_fired_and_deferred_scores_are_reported_separately(self):
        """If the allocator works, fired trips outscore deferred ones. A single
        mean over both could not show that."""
        s = war._summarise([_row(fired=True, score=3.5), _row(fired=False, score=0.8)])
        assert s["mean_score_fired"] == 3.5 and s["mean_score_deferred"] == 0.8


class TestSaturationSignature:
    def test_a_flat_line_at_the_ceiling_is_reported_as_saturated(self, monkeypatch):
        rows = [(NOW - timedelta(days=d, hours=h), "C")
                for d in range(10) for h in range(6)]
        monkeypatch.setattr(war.mongo_query, "find_rows", lambda *a, **k: rows)
        out = war.saturation(days=21)
        assert out["saturated"] is True
        assert out["distinct_daily_counts"] == [6]

    def test_a_varying_budget_is_not_saturated(self, monkeypatch):
        rows = [(NOW - timedelta(days=d, hours=h), "C")
                for d in range(10) for h in range(d % 5 + 1)]
        monkeypatch.setattr(war.mongo_query, "find_rows", lambda *a, **k: rows)
        assert war.saturation(days=21)["saturated"] is False

    def test_too_few_days_is_not_called_saturated(self, monkeypatch):
        """Three identical days is a coincidence, not a signature."""
        rows = [(NOW - timedelta(days=d, hours=h), "C")
                for d in range(3) for h in range(6)]
        monkeypatch.setattr(war.mongo_query, "find_rows", lambda *a, **k: rows)
        assert war.saturation(days=21)["saturated"] is False

    def test_a_read_failure_reports_an_error_not_an_empty_calm(self, monkeypatch):
        def boom(*a, **k):
            raise RuntimeError("mongo down")

        monkeypatch.setattr(war.mongo_query, "find_rows", boom)
        out = war.saturation(days=21)
        assert out["status"] == "error" and out["per_day"] == {}


def test_allocations_survives_a_read_failure(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("mongo down")

    monkeypatch.setattr(war.mongo_query, "find_rows", boom)
    out = war.allocations(hours=72, ticker=None, limit=200)
    assert out["status"] == "error" and out["allocations"] == []


def test_allocations_labels_every_row_with_an_outcome_state(monkeypatch):
    rows = [tuple(_row(fired=True, outcome=None)[c] for c in war._COLUMNS),
            tuple(_row(fired=False, outcome=None)[c] for c in war._COLUMNS),
            tuple(_row(fired=True, outcome={"decision_changed": False,
                                            "woke_action": "HOLD"})[c]
                  for c in war._COLUMNS)]
    monkeypatch.setattr(war.mongo_query, "find_rows", lambda *a, **k: rows)
    out = war.allocations(hours=72, ticker=None, limit=200)
    assert [a["outcome_state"] for a in out["allocations"]] == \
        ["pending", "not_applicable", "scored"]
