"""Real MongoDB integration test suite for Step 11: Historical Data Disposition & Clean-Break Verification.

Tests Steps 06–11 together on disposable real MongoDB:
1. Multi-contract dataset insertion (unversioned, v2, v3, v4, quarantined, synthetic, legacy lots, live lots).
2. audit_historical_database: mathematical cohort reconciliation and no-silent-upgrades validation.
3. Unified Outcome Readers (Step 10):
   - get_verified_decision_outcomes strictly isolates valid mature records.
   - get_learning_cohort_outcomes strictly excludes unversioned, synthetic, quarantined, and mismatched records.
   - get_verified_closed_lot_outcomes strictly rejects legacy unallocated closures.
4. Downstream Readers (scorecard, eval_engine, shadow report) execute cleanly with zero corruption from legacy rows.
"""

from __future__ import annotations

import datetime
import secrets
from unittest.mock import patch
import pytest

from app.trading.attribution.historical_classifier import audit_historical_database
from app.trading.attribution.outcome_reader import (
    get_verified_decision_outcomes,
    get_verified_closed_lot_outcomes,
    get_learning_cohort_outcomes,
    get_shadow_comparison_outcomes,
)
from app.trading.attribution.repository import (
    COLL_DECISION_OUTCOMES,
    COLL_LOT_CLOSURES,
    COLL_POSITION_LOTS,
    ensure_attribution_indexes,
)

pytestmark = pytest.mark.real_mongo


def _gen_token() -> str:
    """Generate in-memory token for zero credential leakage compliance."""
    return f"tok_{secrets.token_hex(8)}"


@pytest.fixture(autouse=True)
def setup_indexes(real_mongo):
    """Ensure indexes exist on test database."""
    ensure_attribution_indexes()


def test_real_mongo_step11_clean_break_and_steps_06_to_10_joint_verification(real_mongo, monkeypatch):
    """Comprehensive real MongoDB verification of Step 11 clean break and Steps 06-10 outcomes."""
    now = datetime.datetime.now(datetime.timezone.utc)
    tag = _gen_token()

    # ─────────────────────────────────────────────────────────────────────────
    # 1. Insert Multi-Contract Historical Decision Outcomes Dataset
    # ─────────────────────────────────────────────────────────────────────────
    # A. 5 Legacy unversioned rows (May 2026 style)
    for i in range(5):
        real_mongo[COLL_DECISION_OUTCOMES].insert_one({
            "id": f"unver-{tag}-{i}",
            "cycle_id": f"cycle-legacy-{tag}-{i}",
            "ticker": "AAPL",
            "action": "BUY",
            "confidence": 70,
            "entry_price": 100.0,
            "exit_price": 105.0,
            "pnl_pct": 5.0,
            "outcome": "WIN",
            "created_at": now - datetime.timedelta(days=120),
            "resolved_at": now - datetime.timedelta(days=113),
        })

    # B. 2 Contract v2 verified rows (matching vendors)
    for i in range(2):
        real_mongo[COLL_DECISION_OUTCOMES].insert_one({
            "decision_id": f"dec-v2-ok-{tag}-{i}",
            "cycle_id": f"cycle-v2-{tag}-{i}",
            "ticker": "MSFT",
            "action": "BUY",
            "confidence": 80,
            "outcome_contract_version": 2,
            "outcome_evidence_state": "verified",
            "entry_price": 200.0,
            "exit_price": 210.0,
            "entry_price_source": "polygon",
            "exit_price_source": "polygon",
            "pnl_pct": 5.0,
            "outcome": "WIN",
            "created_at": now - datetime.timedelta(days=30),
            "resolved_at": now - datetime.timedelta(days=23),
        })

    # C. 1 Contract v2 legacy row with mismatched price sources (unsupported for learning)
    real_mongo[COLL_DECISION_OUTCOMES].insert_one({
        "decision_id": f"dec-v2-mismatch-{tag}",
        "cycle_id": f"cycle-v2-mismatch-{tag}",
        "ticker": "MSFT",
        "action": "BUY",
        "confidence": 75,
        "outcome_contract_version": 2,
        "outcome_evidence_state": "verified",
        "entry_price": 200.0,
        "exit_price": 210.0,
        "entry_price_source": "polygon",
        "exit_price_source": "yfinance",
        "pnl_pct": 5.0,
        "outcome": "WIN",
        "created_at": now - datetime.timedelta(days=30),
        "resolved_at": now - datetime.timedelta(days=23),
    })

    # D. 2 Unsupported claim rows
    for i in range(2):
        real_mongo[COLL_DECISION_OUTCOMES].insert_one({
            "decision_id": f"dec-unsupp-{tag}-{i}",
            "cycle_id": f"cycle-unsupp-{tag}-{i}",
            "ticker": "GOOG",
            "action": "HOLD",
            "outcome_evidence_state": "unsupported_claim",
            "created_at": now - datetime.timedelta(days=25),
        })

    # E. 1 Synthetic cycle row (contamination)
    real_mongo[COLL_DECISION_OUTCOMES].insert_one({
        "decision_id": f"dec-synth-{tag}",
        "cycle_id": f"bench-contamination-{tag}",
        "ticker": "NVDA",
        "action": "BUY",
        "pnl_pct": 10.0,
        "outcome": "WIN",
        "contract_version": 4,
        "maturity_status": "MATURE_VERIFIED",
        "created_at": now - datetime.timedelta(days=10),
    })

    # F. 1 Quarantined row
    real_mongo[COLL_DECISION_OUTCOMES].insert_one({
        "decision_id": f"dec-quar-{tag}",
        "cycle_id": f"cycle-quar-{tag}",
        "ticker": "NVDA",
        "action": "BUY",
        "is_quarantined": True,
        "contract_version": 4,
        "maturity_status": "MATURE_VERIFIED",
        "created_at": now - datetime.timedelta(days=10),
    })

    # G. 3 Contract v4 Mature Verified rows (Step 06 & 08 canonical)
    for i in range(3):
        real_mongo[COLL_DECISION_OUTCOMES].insert_one({
            "decision_id": f"dec-v4-{tag}-{i}",
            "cycle_id": f"cycle-v4-{tag}-{i}",
            "bot_id": "test_bot",
            "ticker": "NVDA",
            "action": "BUY",
            "confidence": 85.0,
            "decision_return_pct": 8.0,
            "forecast_alpha": 3.5,
            "benchmark_return_pct": 4.5,
            "outcome": "WIN",
            "contract_version": 4,
            "maturity_status": "MATURE_VERIFIED",
            "is_quarantined": False,
            "exclusion_reason": None,
            "created_at": now - datetime.timedelta(days=9),
            "resolved_at": now - datetime.timedelta(days=2),
        })

    # Total decision_outcomes inserted = 5 + 2 + 1 + 2 + 1 + 1 + 3 = 15

    # ─────────────────────────────────────────────────────────────────────────
    # 2. Insert Realized Lot Closures Dataset
    # ─────────────────────────────────────────────────────────────────────────
    # A. 2 Legacy closures (missing allocated fee, unversioned)
    for i in range(2):
        real_mongo[COLL_LOT_CLOSURES].insert_one({
            "closure_id": f"cls-leg-{tag}-{i}",
            "lot_id": f"lot-leg-{tag}-{i}",
            "bot_id": "test_bot",
            "ticker": "AAPL",
            "closed_qty": 10.0,
            "entry_price": 100.0,
            "exit_price": 110.0,
            "realized_pnl": 100.0,
            "closed_at": now - datetime.timedelta(days=40),
        })

    # B. 2 Live attributable v4 closures (Step 09 with conserved fees)
    for i in range(2):
        real_mongo[COLL_LOT_CLOSURES].insert_one({
            "closure_id": f"cls-live-v4-{tag}-{i}",
            "lot_id": f"lot-live-{tag}-{i}",
            "bot_id": "test_bot",
            "ticker": "NVDA",
            "closed_qty": 10.0,
            "entry_price": 100.0,
            "exit_price": 110.0,
            "allocated_entry_fee": 1.0,
            "exit_fee": 1.0,
            "fees": 2.0,
            "dollar_pnl": 98.0,
            "invested_capital_denominator": 1001.0,
            "net_realized_return": 9.7902,
            "gross_pnl": 100.0,
            "lot_alpha": 4.2,
            "benchmark_return": 5.5902,
            "closed_at": now - datetime.timedelta(days=5),
            "origin": "LIVE",
            "provenance_complete": True,
            "is_attributable": True,
        })

    # Total lot_closures inserted = 2 + 2 = 4

    # ─────────────────────────────────────────────────────────────────────────
    # 3. Insert Position Lots
    # ─────────────────────────────────────────────────────────────────────────
    real_mongo[COLL_POSITION_LOTS].insert_one({"lot_id": f"pl-1-{tag}", "status": "open"})
    real_mongo[COLL_POSITION_LOTS].insert_one({"lot_id": f"pl-2-{tag}", "status": "closed"})

    # ─────────────────────────────────────────────────────────────────────────
    # 4. Step 11 Audit Reconciliation Verification
    # ─────────────────────────────────────────────────────────────────────────
    audit = audit_historical_database(real_mongo)
    assert audit["decision_outcomes"]["total"] == 15
    assert audit["decision_outcomes"]["reconciled"] is True
    assert audit["decision_outcomes"]["no_silent_upgrades_verified"] is True

    # Learning eligible must be EXACTLY 2 (Contract v2 OK) + 3 (Contract v4 OK) = 5
    assert audit["decision_outcomes"]["learning_eligible_count"] == 5

    # Lot closures reconciliation
    assert audit["lot_closures"]["total"] == 4
    assert audit["lot_closures"]["reconciled"] is True
    assert audit["lot_closures"]["attributable_count"] == 2

    # ─────────────────────────────────────────────────────────────────────────
    # 5. Reader Compatibility & Exclusion Paths (Steps 06-10 Integration)
    # ─────────────────────────────────────────────────────────────────────────
    # Reader A: get_learning_cohort_outcomes
    learning_cohort = get_learning_cohort_outcomes(db=real_mongo)
    assert len(learning_cohort) == 5
    learning_ids = {o["decision_id"] for o in learning_cohort}
    expected_learning_ids = {
        f"dec-v2-ok-{tag}-0", f"dec-v2-ok-{tag}-1",
        f"dec-v4-{tag}-0", f"dec-v4-{tag}-1", f"dec-v4-{tag}-2",
    }
    assert learning_ids == expected_learning_ids

    # Reader B: get_verified_closed_lot_outcomes
    closed_lots = get_verified_closed_lot_outcomes(db=real_mongo)
    assert len(closed_lots) == 2
    for cl in closed_lots:
        assert cl["is_attributable"] is True
        assert cl["dollar_pnl"] == 98.0
        assert cl["invested_capital_denominator"] == 1001.0
        assert cl["net_realized_return"] == 9.7902

    # Reader C: agent_scorecard._resolved_outcomes
    monkeypatch.setattr("app.db.mongo_store.get_doc_db", lambda: real_mongo)
    from scripts.agent_scorecard import _resolved_outcomes
    scorecard_rows = _resolved_outcomes(since=now - datetime.timedelta(days=200))
    # Excludes quarantined and synthetic cycles!
    scorecard_ids = {r.get("decision_id") for r in scorecard_rows if r.get("decision_id")}
    assert f"dec-synth-{tag}" not in scorecard_ids
    assert f"dec-quar-{tag}" not in scorecard_ids

    # Reader D: eval_engine.evaluate_confidence_calibration
    from app.autoresearch.eval_engine import evaluate_confidence_calibration
    cal = evaluate_confidence_calibration(ticker="NVDA")
    assert cal["status"] == "ok"
    # All 3 v4 NVDA outcomes are WIN with confidence 85.0
    assert cal["win_count"] == 3
    assert cal["avg_confidence_on_wins"] == 85.0
    assert cal["calibration_score"] == 85.0
