"""
Open-items wave 2, 2026-08-11 — items 26, 28 and 41.

Item 41 was decided rather than engineered: the 68 inert triggers are RETIRED,
not armed. Every one is a BUY, so teaching the checker to read them would have
started real trades. These tests pin "retired and not armed" in both directions.
"""

from unittest.mock import patch

import pytest

from app.trading.order_triggers import (
    dynamic_trigger_is_evaluable,
    retire_inert_dynamic_triggers,
)
from app.v3.reconciliation import ReconciliationResult, reconcile_cycle
from app.v3.shared_desk import DecisionProvenance


class _FakeDb:
    """Minimal db double: canned SELECT result, records every statement."""

    def __init__(self, rows):
        self._rows = rows
        self.statements = []

    def execute(self, sql, args=None):
        self.statements.append((" ".join(sql.split()), args))
        return self

    def fetchall(self):
        return self._rows


# ── Item 41: retire the inert, arm nothing ─────────────────────────────

def test_inert_triggers_are_deactivated():
    db = _FakeDb([
        ("trg-1", "CRM", "support_retest"),      # inert
        ("trg-2", "PLTR", "sma_50_reclaim"),     # inert
        ("trg-3", "ACHR", "sma_100_drop"),       # inert, no such column
        ("trg-4", "XOM", "sma_50_drop"),         # WORKS — must survive
        ("trg-5", "GS", "rsi_14_oversold"),      # WORKS — must survive
    ])
    assert retire_inert_dynamic_triggers(db) == 3

    updates = [s for s, _ in db.statements if s.startswith("UPDATE")]
    assert len(updates) == 1, "one bulk update, not one per row"
    retired_ids = db.statements[-1][1][0]
    assert sorted(retired_ids) == ["trg-1", "trg-2", "trg-3"]
    assert "trg-4" not in retired_ids and "trg-5" not in retired_ids


def test_a_working_trigger_is_never_retired():
    """The dangerous direction. Retiring a live watch silently removes a
    protection the desk believes it has."""
    db = _FakeDb([("trg-a", "XOM", "sma_50_drop"), ("trg-b", "F", "sma_200_drop")])
    assert retire_inert_dynamic_triggers(db) == 0
    assert not [s for s, _ in db.statements if s.startswith("UPDATE")]


def test_the_sweep_is_idempotent():
    """It runs every 60s. A second pass finds nothing because the first
    deactivated them, so the UPDATE must not keep firing."""
    db = _FakeDb([])
    assert retire_inert_dynamic_triggers(db) == 0


def test_retirement_never_raises_on_a_broken_database():
    class _Boom:
        def execute(self, *a, **k):
            raise RuntimeError("db down")
    assert retire_inert_dynamic_triggers(_Boom()) == 0


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
    with patch("app.db.connection.get_db") as gd:
        gd.return_value.__enter__.return_value = _FakeDb(tr_rows)
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
    with patch("app.db.connection.get_db", side_effect=RuntimeError("db down")):
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

    assert any("DISAGREE" in r.getMessage() for r in caplog.records)
    # Recorded, not paged — item 26 warns the first mismatch may be a person.
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]
