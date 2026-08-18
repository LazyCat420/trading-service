"""`check_ticker_complete` had no tests at all.

It emits three of the pipeline's per-ticker invariants — `PIPELINE_NO_DESK`,
`PIPELINE_NO_TRADE_ROW` and `PIPELINE_COMPLETE_BUT_NO_DECISION` — and every one
of them was unverified, on a check that reads the database and is wired into
the end of the per-ticker pipeline (`orchestrator.py`).

TWO OBSERVERS, DELIBERATELY SEPARATE
------------------------------------
`PIPELINE_COMPLETE_BUT_NO_DECISION` is NOT the stall detector. The stall
detector is `DESK_STALLED_MID_PIPELINE` (`_check_desks_reached_terminal`,
covered in `test_desk_stall_invariant.py`), and the two are kept apart on
purpose: a stalled desk never reached the end, while this check fires on a desk
that ran to completion and decided nothing. Wiring the two together would give
the pipeline one observer where it has two, and two observers are only worth
having when they can disagree.

The fake DB below reproduces the check's own probe semantics from the filter it
actually passes to `mongo_store.count_docs`, rather than asserting a hardcoded
call shape.
"""

from __future__ import annotations

import pytest

from app.v3 import invariants


class _FakeDB:
    """Answers the two existence probes using the filter the check passed.

    Keyed off the COLLECTION name, so the test cannot pass by matching the
    order the probes happen to run in.
    """

    def __init__(self, *, desks, trade_rows, cycle_id="cycle-test-1"):
        self.desks = set(desks)
        self.trade_rows = set(trade_rows)
        self.cycle_id = cycle_id
        self.queries: list[tuple[str, dict]] = []

    def count_docs(self, collection, filt, **kwargs):
        self.queries.append((collection, filt))
        assert collection in ("shared_desk", "trade_results"), collection
        table = self.desks if collection == "shared_desk" else self.trade_rows
        hit = filt.get("cycle_id") == self.cycle_id and filt.get("ticker") in table
        return 1 if hit else 0


class _Desk:
    def __init__(self, phase="PM_DONE", trade_decision=None, final_decision=None):
        self.phase = phase
        self.trade_decision = trade_decision or {}
        self.final_decision = final_decision or {}


@pytest.fixture
def recorded(monkeypatch):
    calls = []

    def _rec(kind, **detail):
        calls.append({"kind": kind, **detail})
        return kind

    monkeypatch.setattr(invariants, "record_violation", _rec)
    return calls


def _run(monkeypatch, *, desks=("NVDA",), trade_rows=("NVDA",),
         ticker="NVDA", cycle_id="cycle-test-1", desk=None, result=None):
    db = _FakeDB(desks=desks, trade_rows=trade_rows, cycle_id=cycle_id)

    # The check imports `app.db.mongo_store` INSIDE the function, so setting
    # the attribute on `invariants` would be a silent no-op.
    from app.db import mongo_store

    monkeypatch.setattr(mongo_store, "count_docs", db.count_docs)
    out = invariants.check_ticker_complete(
        ticker=ticker, cycle_id=cycle_id, desk=desk, result=result
    )
    return out, db


def _kinds(recorded):
    return [c["kind"] for c in recorded]


# ── the healthy case must stay silent ────────────────────────────────────

class TestAHealthyTickerFiresNothing:
    def test_desk_trade_row_and_a_decision(self, monkeypatch, recorded):
        out, _ = _run(monkeypatch, desk=_Desk(), result={"action": "BUY"})
        assert out == []
        assert _kinds(recorded) == []

    @pytest.mark.parametrize("action", ["BUY", "SELL", "HOLD"])
    def test_hold_is_a_decision_like_any_other(self, monkeypatch, recorded, action):
        """A HOLD is a decision. Treating it as "nothing happened" is what
        would make this check fire on healthy cycles until someone muted it."""
        out, _ = _run(monkeypatch, desk=_Desk(), result={"action": action})
        assert out == []


# ── the three shapes it exists to catch ──────────────────────────────────

class TestTheDeskVanished:
    def test_no_desk_row_fires_no_desk(self, monkeypatch, recorded):
        """The ORCL case: 215s of work, save_desk threw, nothing persisted."""
        out, _ = _run(monkeypatch, desks=(), desk=_Desk(), result={"action": "BUY"})
        assert invariants.KIND_NO_DESK in _kinds(recorded)
        assert invariants.KIND_NO_DESK in out

    def test_a_missing_desk_does_not_also_claim_a_missing_trade_row(
        self, monkeypatch, recorded
    ):
        """Reporting both would double-count one failure. The trade-row check
        is deliberately the `elif` branch."""
        _, _ = _run(monkeypatch, desks=(), trade_rows=(), desk=_Desk(),
                    result={"action": "BUY"})
        assert invariants.KIND_NO_TRADE_ROW not in _kinds(recorded)


class TestTheTradeRowNeverLanded:
    def test_decision_without_a_trade_row_fires(self, monkeypatch, recorded):
        """policy_action computed, UPDATE hit zero rows, tier absent from
        every funnel query."""
        out, _ = _run(monkeypatch, trade_rows=(), desk=_Desk(),
                      result={"action": "BUY"})
        assert invariants.KIND_NO_TRADE_ROW in out

    def test_no_decision_means_no_trade_row_is_expected(self, monkeypatch, recorded):
        """A Triage-Gate skip is a legitimate non-decision. Demanding a trade
        row for it is how an observer gets muted."""
        _, _ = _run(monkeypatch, trade_rows=(), desk=_Desk(), result={})
        assert invariants.KIND_NO_TRADE_ROW not in _kinds(recorded)


class TestCompletedButUndecided:
    def test_empty_result_fires_no_decision(self, monkeypatch, recorded):
        out, _ = _run(monkeypatch, desk=_Desk(), result={})
        assert invariants.KIND_NO_DECISION in out

    def test_action_present_but_none_is_the_degraded_sentinel(
        self, monkeypatch, recorded
    ):
        """`{"action": None}` means the pipeline TRIED to decide and failed.
        Counting it as a decision is the exact blindness this check exists to
        remove — it is the "HOLD @ 0%, persona: unknown" shape."""
        out, _ = _run(monkeypatch, desk=_Desk(), result={"action": None})
        assert invariants.KIND_NO_DECISION in out

    def test_an_unrecognised_action_is_not_a_decision(self, monkeypatch, recorded):
        out, _ = _run(monkeypatch, desk=_Desk(), result={"action": "MAYBE"})
        assert invariants.KIND_NO_DECISION in out

    def test_a_decision_on_the_desk_counts_when_result_is_empty(
        self, monkeypatch, recorded
    ):
        """The decision may arrive on the desk rather than in `result`; the
        check reads three sources for exactly this reason."""
        out, _ = _run(monkeypatch, desk=_Desk(trade_decision={"action": "SELL"}),
                      result=None)
        assert invariants.KIND_NO_DECISION not in out

    def test_final_decision_also_counts(self, monkeypatch, recorded):
        out, _ = _run(monkeypatch, desk=_Desk(final_decision={"action": "HOLD"}),
                      result=None)
        assert invariants.KIND_NO_DECISION not in out

    def test_the_provenance_is_carried_into_the_violation(
        self, monkeypatch, recorded
    ):
        """Without it the row says a desk decided nothing and cannot say why."""
        _, _ = _run(monkeypatch, desk=_Desk(),
                    result={"decision_provenance": "coerced_unshortable"})
        row = next(c for c in recorded if c["kind"] == invariants.KIND_NO_DECISION)
        assert row["provenance"] == "coerced_unshortable"


# ── it must never take the pipeline down ─────────────────────────────────

class TestItIsAProbeNotAGate:
    def test_a_db_failure_yields_no_violations(self, monkeypatch, recorded):
        """A probe that cannot read must not invent a violation — that would
        turn a transient outage into a fleet of false alarms."""
        from app.db import mongo_store

        def _boom(*a, **k):
            raise RuntimeError("connection refused")

        # The probe is `mongo_store.count_docs` now; patching `connection.get_db`
        # here intercepted nothing and left this test vacuous.
        monkeypatch.setattr(mongo_store, "count_docs", _boom)
        out = invariants.check_ticker_complete(ticker="NVDA", cycle_id="c1")
        assert out == []
        assert recorded == []

    @pytest.mark.parametrize("ticker,cycle_id", [("", "c1"), ("NVDA", ""), ("", "")])
    def test_missing_identifiers_short_circuit(self, monkeypatch, recorded,
                                               ticker, cycle_id):
        out = invariants.check_ticker_complete(ticker=ticker, cycle_id=cycle_id)
        assert out == []


# ── the separation from the stall detector ───────────────────────────────

def test_this_check_is_not_the_stall_detector():
    """They are different observers with different kinds and different inputs.

    Collapsing them is explicitly rejected in the 2026-07-30 handoff: the
    no-decision row would assert something false about a desk that never
    finished, and it is strictly weaker than the `phase_at_crash` the crash
    recorder already writes.
    """
    assert invariants.KIND_NO_DECISION != invariants.KIND_DESK_STALLED
    assert invariants.KIND_NO_DECISION == "PIPELINE_COMPLETE_BUT_NO_DECISION"
    assert invariants.KIND_DESK_STALLED == "DESK_STALLED_MID_PIPELINE"
