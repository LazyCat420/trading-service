"""
Open-items wave 2, 2026-08-11 — items 26, 28 and 41.

Item 41 was decided rather than engineered: the 68 inert triggers are RETIRED,
not armed. Every one is a BUY, so teaching the checker to read them would have
started real trades. These tests pin "retired and not armed" in both directions.

These used to pass a `_FakeDb` into `retire_inert_dynamic_triggers(db)` and to
patch `scripts.migration.pg_connection.get_db` for the reconciliation cases, then assert on
SQL text (`s.startswith("UPDATE")`). Neither hook exists now:
`retire_inert_dynamic_triggers()` takes no argument and drives
`mongo_query.find_rows` / `mongo_store.update_docs` itself, and
`reconcile_cycle` reads both sides through `mongo_query.find_rows` — imported
INSIDE the function, so only `app.db.mongo_query` can be patched for it.

The retirement assertions are stronger for the move: instead of counting
statements that begin with "UPDATE", they read the actual filter and the
actual `$set`, so a sweep that retired the right count of the WRONG rows, or
that set some other field, now fails here.
"""

from unittest.mock import patch

import pytest

from app.trading import order_triggers
from app.trading.order_triggers import (
    dynamic_trigger_is_evaluable,
    retire_inert_dynamic_triggers,
)
from app.v3.reconciliation import ReconciliationResult, reconcile_cycle
from app.v3.shared_desk import DecisionProvenance


class _FakeTriggerStore:
    """Captures the sweep's `update_docs` calls against `price_triggers`."""

    def __init__(self):
        self.updates = []

    def update_docs(self, collection, filt, update, **kwargs):
        assert collection == "price_triggers"
        self.updates.append((filt, update))
        return len(filt.get("id", {}).get("$in", []))


def _sweep_with(rows, monkeypatch, read_error=None, write_error=None):
    """Run the sweep over `rows`, returning (result, captured store).

    `rows` are the (id, ticker, dynamic_trigger_type) tuples find_rows returns
    in that requested column order.
    """
    def _find_rows(collection, filt, columns, **kwargs):
        if read_error:
            raise read_error
        assert collection == "price_triggers"
        # The sweep must only ever consider ACTIVE DYNAMIC triggers; widening
        # this filter would put static stop-losses at risk of retirement.
        assert filt == {"trigger_type": "dynamic", "active": True}
        assert columns == ["id", "ticker", "dynamic_trigger_type"]
        return rows

    store = _FakeTriggerStore()
    if write_error:
        def _update_docs(*a, **k):
            raise write_error
        store.update_docs = _update_docs

    monkeypatch.setattr(order_triggers.mongo_query, "find_rows", _find_rows)
    monkeypatch.setattr(order_triggers, "mongo_store", store)
    return retire_inert_dynamic_triggers(), store


# ── Item 41: retire the inert, arm nothing ─────────────────────────────

def test_inert_triggers_are_deactivated(monkeypatch):
    retired, store = _sweep_with([
        ("trg-1", "CRM", "support_retest"),      # inert
        ("trg-2", "PLTR", "sma_50_reclaim"),     # inert
        ("trg-3", "ACHR", "sma_100_drop"),       # inert, no such column
        ("trg-4", "XOM", "sma_50_drop"),         # WORKS — must survive
        ("trg-5", "GS", "rsi_14_oversold"),      # WORKS — must survive
    ], monkeypatch)
    assert retired == 3

    assert len(store.updates) == 1, "one bulk update, not one per row"
    filt, update = store.updates[0]
    retired_ids = filt["id"]["$in"]
    assert sorted(retired_ids) == ["trg-1", "trg-2", "trg-3"]
    assert "trg-4" not in retired_ids and "trg-5" not in retired_ids
    # Retirement means deactivation, and nothing else about the row changes.
    assert update == {"$set": {"active": False}}


def test_a_working_trigger_is_never_retired(monkeypatch):
    """The dangerous direction. Retiring a live watch silently removes a
    protection the desk believes it has."""
    retired, store = _sweep_with(
        [("trg-a", "XOM", "sma_50_drop"), ("trg-b", "F", "sma_200_drop")], monkeypatch)
    assert retired == 0
    assert store.updates == []


def test_the_sweep_is_idempotent(monkeypatch):
    """It runs every 60s. A second pass finds nothing because the first
    deactivated them, so the UPDATE must not keep firing."""
    retired, store = _sweep_with([], monkeypatch)
    assert retired == 0
    assert store.updates == []


def test_retirement_never_raises_on_a_broken_database(monkeypatch):
    # Both halves must fail soft: the read that finds candidates...
    retired, _ = _sweep_with([], monkeypatch, read_error=RuntimeError("db down"))
    assert retired == 0
    # ...and the write that retires them.
    retired, _ = _sweep_with(
        [("trg-1", "CRM", "support_retest")], monkeypatch,
        write_error=RuntimeError("db down"))
    assert retired == 0


def test_nothing_was_armed():
    """Item 41's decision, pinned. If someone later teaches the checker to read
    these, this test fails and they must revisit the decision deliberately."""
    for setup in ("sma_50_reclaim", "sma_50_breakout", "sma_200_break",
                  "support_retest", "resistance_breakout", "sma_100_drop"):
        assert dynamic_trigger_is_evaluable(setup) is False, \
            f"{setup} became evaluable — that ARMS a dormant BUY trigger"


# ── Item 28: the writer that must NOT be added ─────────────────────────

def test_no_trade_gate_skip_stays_unwritten(monkeypatch):
    """Open item 28 called this "an enum member with a test and no producer".
    Adding a producer would stamp a genuinely-reasoned board decision as a
    skip and drop it out of --reasoned-only accuracy scoring."""
    import subprocess
    from pathlib import Path

    repo = Path(__file__).resolve().parents[2]
    hits = subprocess.run(
        ["grep", "-rn", "NO_TRADE_GATE_SKIP", "app/"],
        cwd=repo, capture_output=True, text=True,
    ).stdout
    # Only the definition and prose about it — never an assignment.
    assert "DecisionProvenance.NO_TRADE_GATE_SKIP.value" not in hits, \
        "a writer appeared; see the comment on the enum member before adding one"
    assert DecisionProvenance.NO_TRADE_GATE_SKIP.value == "no_trade_gate_skip"


# ── Item 26: the reconciliation that nobody ran ────────────────────────

def _reconcile_with(desks, tr_rows):
    """Reconcile with `trade_results` returning `tr_rows`.

    `reconcile_cycle` imports mongo_query inside the function, so the patch has
    to land on `app.db.mongo_query` itself. Reads are dispatched on the
    COLLECTION name: `desks` is passed in here, so only `trade_results` should
    ever be queried, and asking for anything else is a bug worth failing on.
    """
    def _find_rows(collection, filt, columns, **kwargs):
        assert collection == "trade_results", f"unexpected read of {collection}"
        assert filt == {"cycle_id": "cycle-test"}
        assert columns == ["ticker", "action", "decision_provenance"]
        return tr_rows

    with patch("app.db.mongo_query.find_rows", _find_rows):
        return reconcile_cycle("cycle-test", desks=desks)


def test_agreeing_records_reconcile():
    r = _reconcile_with(
        {"XOM": {"trade_decision": {"action": "HOLD", "decision_provenance": "board_reasoned"}}},
        [("XOM", "HOLD", "board_reasoned")],
    )
    assert r.reconciled
    assert r.action_mismatches == [] and r.provenance_mismatches == []


def test_a_disagreeing_action_is_caught():
    r = _reconcile_with(
        {"XOM": {"trade_decision": {"action": "BUY"}}},
        [("XOM", "HOLD", "board_reasoned")],
    )
    assert not r.reconciled
    assert "desk=BUY != trade_results=HOLD" in r.action_mismatches[0]


def test_a_desk_with_no_saved_row_is_caught():
    """The 19-day divergence this check exists for."""
    r = _reconcile_with({"XOM": {"trade_decision": {"action": "HOLD"}}}, [])
    assert not r.reconciled
    assert "NO trade_results row" in r.action_mismatches[0]


def test_a_provenance_disagreement_is_caught():
    """Comparing only the action let the two stores disagree about whether an
    agent decided at all — the exact laundering the field exists to stop."""
    r = _reconcile_with(
        {"XOM": {"trade_decision": {"action": "HOLD",
                                    "decision_provenance": "board_degraded_fallback"}}},
        [("XOM", "HOLD", "board_reasoned")],
    )
    assert not r.reconciled
    assert r.action_mismatches == []
    assert "board_degraded_fallback != " in r.provenance_mismatches[0].replace("desk=", "")


def test_an_empty_comparison_is_not_a_pass():
    """An empty result is the absence of evidence, not evidence of health."""
    r = _reconcile_with({}, [])
    assert r.checked is False
    assert r.reconciled is False, "nothing-to-compare must never read as reconciled"
    assert "nothing to compare" in r.summary()


def test_reconciliation_never_raises():
    with patch("app.db.mongo_query.find_rows", side_effect=RuntimeError("db down")):
        r = reconcile_cycle("cycle-test")
    assert r.error and not r.reconciled and not r.checked


def test_the_runtime_entry_point_warns_on_a_mismatch(caplog):
    import logging
    from app.v3 import reconciliation

    bad = ReconciliationResult(cycle_id="c", desks_seen=1, saved_rows=1,
                               action_mismatches=["XOM: desk=BUY != trade_results=HOLD"])
    with patch.object(reconciliation, "reconcile_cycle", return_value=bad):
        with caplog.at_level(logging.WARNING, logger="app.v3.reconciliation"):
            reconciliation.reconcile_and_report("c")

    mine = [r for r in caplog.records if r.name == "app.v3.reconciliation"]
    assert any("DISAGREE" in r.getMessage() for r in mine)
    # Recorded, not paged — item 26 warns the first mismatch may be a person.
    #
    # SCOPED TO THIS LOGGER, and that scoping is the fix for a real flake.
    # `caplog` captures every propagated record, not only the logger named in
    # `at_level`. Whenever another test in the same xdist worker had already
    # armed the DB log handler, `mongo_store.ensure_indexes` tripped the
    # `block_production_mongo` guard and swallowed it into a NON-FATAL
    # `logger.error(...)`. That unrelated ERROR landed in `caplog.records` and
    # failed this assertion — a test failing on worker scheduling, for a
    # record emitted by a module it does not touch.
    #
    # Reproduced deterministically 2026-09-05:
    #   pytest tests/unit/test_db_logger_boot.py tests/unit/test_open_items_wave2.py
    # fails; either file alone passes.
    assert not [r for r in mine if r.levelno >= logging.ERROR]
