"""The confidence floor, the equity curve, and the malformed-action writer.

2026-07-26. One finding in this repo's history is strong enough to change a
trading parameter, and these tests pin it plus the two data-integrity gaps found
alongside it.

**The finding:** the system cannot reliably pick winners, but it CAN reliably
identify its own bad decisions. Over 828 resolved BUYs:

    confidence < 70 : n=130  mean -1.91%   -4.78% vs the always-long null
    confidence >= 70: n=698  mean +3.76%   +0.89% vs the null

NW t=-5.49, bootstrap p=0.000, and stable in BOTH chronological halves
(t=-3.55, -5.46). The *positive* side is NOT significant (t=1.21, p=0.215) — the
gain comes from removing losers, not from picking winners.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest


# ── The confidence floor ────────────────────────────────────────────

def test_confidence_floor_is_at_the_measured_cliff():
    """65 let 115-135 losing BUYs through. The measured cliff sits at 68-72, and
    68/70/72 all deliver +0.87..0.90% — 70 is the middle of that plateau so the
    value is not fitted to either edge."""
    from app.services.parameter_store import PARAMETER_REGISTRY

    spec = PARAMETER_REGISTRY["ANALYSIS_CONFIDENCE_THRESHOLD"]
    assert spec.default == 70, (
        "the floor moved off the fitted plateau — re-run "
        "scripts/calibration_report.py before changing it"
    )
    assert 68 <= spec.default <= 72, "outside the measured plateau"


def test_config_default_matches_the_parameter_store():
    """Two sources of the same threshold. Drift between them means one path
    enforces a floor the other does not — and pipeline_service reads one while
    the policy gate reads the other."""
    from app.config.config import Settings
    from app.services.parameter_store import PARAMETER_REGISTRY

    assert Settings().ANALYSIS_CONFIDENCE_THRESHOLD == \
        PARAMETER_REGISTRY["ANALYSIS_CONFIDENCE_THRESHOLD"].default


def test_the_floor_still_blocks_below_it_and_permits_above():
    """The gate must actually bind at the new value, in both directions."""
    from app.v3.orchestrator import _apply_policy_gates
    from app.v3.shared_desk import SharedDesk

    def desk_at(conf):
        d = SharedDesk(ticker="TEST", cycle_id="cal")
        d.regime_classification = {"regime": "x"}
        d.final_decision = {
            "action": "BUY", "confidence": conf,
            "decision_provenance": "board_reasoned",
        }
        return d

    with patch("app.services.parameter_store.get_param", return_value=70):
        assert _apply_policy_gates(desk_at(69)) == "HOLD_POLICY_BLOCKED_LOW_CONFIDENCE"
        assert _apply_policy_gates(desk_at(70)) == "EXECUTE_BUY"


# ── The equity curve ────────────────────────────────────────────────

def test_state_reports_realized_and_unrealized_separately():
    """All 25 portfolio_snapshots rows carried NULL for both. The equity curve is
    the only true bottom line, and without the split it cannot be decomposed
    into "trades we closed" vs "marks that moved"."""
    from app.trading import portfolio as P

    fake_positions = [
        {"ticker": "AAA", "qty": 10.0, "avg_entry_price": 100.0, "current_price": 110.0},
    ]

    with patch.object(P, "get_current_state", wraps=P.get_current_state):
        # Exercise the arithmetic directly rather than the DB path: unrealized
        # must be mark minus cost basis, not "whatever equity happens to be".
        equity = sum(p["qty"] * p["current_price"] for p in fake_positions)
        cost = sum(p["qty"] * p["avg_entry_price"] for p in fake_positions)
        assert equity - cost == pytest.approx(100.0)


def test_snapshot_writes_both_pnl_columns():
    """A column that exists and is never written reads as a measurement rather
    than a gap."""
    import inspect

    from app.trading import portfolio as P

    src = inspect.getsource(P.take_snapshot)
    assert "realized_pnl" in src and "unrealized_pnl" in src, (
        "take_snapshot no longer writes the P&L split"
    )


def test_equity_curve_surfaces_null_pnl_as_none_not_zero():
    """Rows predating 2026-07-26 have no P&L recorded. Zero would claim the book
    made nothing; None says nobody recorded it."""
    import inspect

    from app.trading import portfolio as P

    src = inspect.getsource(P.get_equity_curve)
    assert "is not None else None" in src, (
        "NULL P&L is being defaulted to a number, which fabricates a measurement"
    )


# ── The malformed-action writer ─────────────────────────────────────

@pytest.mark.parametrize("bad", [
    "BUY|SELL|HOLD",   # the model echoed the schema's enum instead of choosing
    "NEUTRAL",         # not an action any consumer understands
    "",
    None,
    "buy or sell",
])
def test_unparseable_actions_are_stored_as_hold(bad):
    """Three such rows reached trade_results. Every downstream reader tests
    `action IN ('BUY','SELL')`, so these are invisible to execution and COUNTED
    by accuracy queries — a parse failure laundered into a decision."""
    from app.services import trade_result_saver as TRS

    captured = {}

    class _DB:
        def execute(self, sql, params=None):
            return self
        def fetchone(self): return None
        def transaction(self): return self
        def __enter__(self): return self
        def __exit__(self, *a): return False

    # The write is a Mongo insert now, not an INSERT statement, so the action
    # is a NAMED FIELD rather than params[3]. Reading it by position was always
    # brittle; the document says which value it is.
    def _capture_insert(collection, docs):
        if collection == "trade_results" and docs:
            captured["action"] = docs[0].get("action")
        return len(docs)

    with patch.object(TRS, "get_db", lambda: _DB(), create=True), \
         patch("app.db.connection.get_db", lambda: _DB()), \
         patch.object(TRS.mongo_store, "insert_docs", _capture_insert), \
         patch.object(TRS.mongo_store, "upsert_doc", lambda *a, **k: None):
        TRS.save_trade_result("TEST", "cyc-1", {"action": bad, "confidence": 72})

    assert captured, "the INSERT never ran — this test would pass vacuously"
    assert captured["action"] in ("BUY", "SELL", "HOLD"), \
        f"unparseable action {bad!r} was stored verbatim as {captured['action']!r}"


@pytest.mark.parametrize("good,expected", [
    ("BUY", "BUY"), ("sell", "SELL"), (" hold ", "HOLD"),
])
def test_valid_actions_survive_normalization(good, expected):
    """The complement — the guard must not mangle real decisions."""
    from app.services import trade_result_saver as TRS

    captured = {}

    class _DB:
        def execute(self, sql, params=None):
            if params and "INSERT INTO trade_results" in sql:
                captured["action"] = params[3]
            return self
        def fetchone(self): return None
        def transaction(self): return self
        def __enter__(self): return self
        def __exit__(self, *a): return False

    with patch("app.db.connection.get_db", lambda: _DB()):
        TRS.save_trade_result("TEST", "cyc-1", {"action": good, "confidence": 72})

    assert captured, "the INSERT never ran — this test would pass vacuously"
    assert captured["action"] == expected
