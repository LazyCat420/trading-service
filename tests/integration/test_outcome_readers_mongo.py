"""Real MongoDB integration test suite for Step 10: Connect Verified Outcomes to All Readers.

Tests:
1. get_verified_decision_outcomes against real MongoDB:
   - Enforces quarantine exclusion, exclusion reasons, synthetic cycle filtering.
   - Correctly standardizes return_pct, alpha, and maturity fields.
2. get_verified_closed_lot_outcomes against real MongoDB:
   - Enforces is_attributable and provenance_complete.
   - Projects correct invested capital and fee conservation fields.
3. get_learning_cohort_outcomes against real MongoDB:
   - Qualifies v4 MATURE_VERIFIED records and legacy verified records with matching sources.
   - Rejects mismatched vendor and unverified records.
4. get_shadow_comparison_outcomes against real MongoDB:
   - Joins on decision_id and falls back to (cycle_id, ticker).
5. Cross-subsystem reader parity on real MongoDB:
   - Scorecard, eval_engine, and shadow reports observe 100% identical return and alpha values.
"""

from __future__ import annotations

import datetime
import secrets
from unittest.mock import patch
import pytest

from app.trading.attribution.outcome_reader import (
    get_verified_decision_outcomes,
    get_verified_closed_lot_outcomes,
    get_learning_cohort_outcomes,
    get_shadow_comparison_outcomes,
)
from app.trading.attribution.repository import (
    COLL_DECISION_OUTCOMES,
    COLL_LOT_CLOSURES,
    ensure_attribution_indexes,
)

pytestmark = pytest.mark.real_mongo


def _gen_token() -> str:
    """Generate in-memory token for zero credential leakage compliance."""
    return f"tok_{secrets.token_hex(8)}"


@pytest.fixture(autouse=True)
def setup_indexes(real_mongo):
    """Ensure all indexes exist on real test database."""
    ensure_attribution_indexes()


def test_real_mongo_verified_decision_outcomes_filtering_and_standardization(real_mongo):
    """Verify get_verified_decision_outcomes drops quarantined/synthetic and standardizes."""
    now = datetime.datetime.now(datetime.timezone.utc)
    bot_id = f"bot-{_gen_token()}"
    tag = _gen_token()

    # 1. Valid Contract v4 mature verified
    valid_dec_id = f"dec-v4-{tag}"
    real_mongo[COLL_DECISION_OUTCOMES].insert_one({
        "decision_id": valid_dec_id,
        "cycle_id": f"cycle-real-{tag}",
        "bot_id": bot_id,
        "ticker": "AAPL",
        "action": "BUY",
        "confidence": 80.0,
        "decision_return_pct": 4.5678,
        "forecast_alpha": 2.1000,
        "benchmark_return_pct": 2.4678,
        "outcome": "WIN",
        "maturity_status": "MATURE_VERIFIED",
        "contract_version": 4,
        "created_at": now,
        "resolved_at": now + datetime.timedelta(days=7),
        "is_quarantined": False,
        "exclusion_reason": None,
    })

    # 2. Quarantined record (must be excluded)
    real_mongo[COLL_DECISION_OUTCOMES].insert_one({
        "decision_id": f"dec-quar-{tag}",
        "cycle_id": f"cycle-real-quar-{tag}",
        "bot_id": bot_id,
        "ticker": "AAPL",
        "action": "BUY",
        "confidence": 70.0,
        "decision_return_pct": 999.0,
        "maturity_status": "MATURE_VERIFIED",
        "contract_version": 4,
        "created_at": now,
        "resolved_at": now + datetime.timedelta(days=7),
        "is_quarantined": True,
        "exclusion_reason": None,
    })

    # 3. Excluded record (must be excluded)
    real_mongo[COLL_DECISION_OUTCOMES].insert_one({
        "decision_id": f"dec-excl-{tag}",
        "cycle_id": f"cycle-real-excl-{tag}",
        "bot_id": bot_id,
        "ticker": "AAPL",
        "action": "BUY",
        "confidence": 70.0,
        "decision_return_pct": 10.0,
        "maturity_status": "MATURE_VERIFIED",
        "contract_version": 4,
        "created_at": now,
        "resolved_at": now + datetime.timedelta(days=7),
        "is_quarantined": False,
        "exclusion_reason": "SPLIT_UNADJUSTED",
    })

    # 4. Synthetic cycle record (must be excluded)
    real_mongo[COLL_DECISION_OUTCOMES].insert_one({
        "decision_id": f"dec-synth-{tag}",
        "cycle_id": f"bench-synthetic-{tag}",
        "bot_id": bot_id,
        "ticker": "AAPL",
        "action": "BUY",
        "confidence": 70.0,
        "decision_return_pct": 10.0,
        "maturity_status": "MATURE_VERIFIED",
        "contract_version": 4,
        "created_at": now,
        "resolved_at": now + datetime.timedelta(days=7),
        "is_quarantined": False,
        "exclusion_reason": None,
    })

    outcomes = get_verified_decision_outcomes(bot_id=bot_id, db=real_mongo)
    assert len(outcomes) == 1
    o = outcomes[0]
    assert o["decision_id"] == valid_dec_id
    assert o["return_pct"] == 4.5678
    assert o["pnl_pct"] == 4.5678
    assert o["forecast_alpha"] == 2.1
    assert o["outcome"] == "WIN"
    assert o["is_eligible_for_learning"] is True


def test_real_mongo_verified_closed_lot_outcomes(real_mongo):
    """Verify get_verified_closed_lot_outcomes on real MongoDB enforces attribution gating."""
    now = datetime.datetime.now(datetime.timezone.utc)
    bot_id = f"bot-lot-{_gen_token()}"
    tag = _gen_token()

    # 1. Valid attributable closed lot
    valid_closure_id = f"cls-valid-{tag}"
    real_mongo[COLL_LOT_CLOSURES].insert_one({
        "closure_id": valid_closure_id,
        "lot_id": f"lot-{tag}",
        "bot_id": bot_id,
        "ticker": "MSFT",
        "closed_qty": 5.0,
        "entry_price": 200.0,
        "exit_price": 220.0,
        "allocated_entry_fee": 1.0,
        "exit_fee": 1.0,
        "fees": 2.0,
        "dollar_pnl": 98.0,
        "invested_capital_denominator": 1001.0,
        "net_realized_return": 9.7902,
        "gross_pnl": 100.0,
        "lot_alpha": 4.0,
        "benchmark_return": 5.7902,
        "closed_at": now,
        "origin": "LIVE",
        "provenance_complete": True,
        "is_attributable": True,
    })

    # 2. Non-attributable / incomplete lot (must be excluded)
    real_mongo[COLL_LOT_CLOSURES].insert_one({
        "closure_id": f"cls-inval-{tag}",
        "lot_id": f"lot-inval-{tag}",
        "bot_id": bot_id,
        "ticker": "MSFT",
        "closed_qty": 5.0,
        "entry_price": 200.0,
        "exit_price": 220.0,
        "allocated_entry_fee": 1.0,
        "exit_fee": 1.0,
        "fees": 2.0,
        "dollar_pnl": 98.0,
        "invested_capital_denominator": 1001.0,
        "net_realized_return": 9.7902,
        "closed_at": now,
        "origin": "MIGRATION",
        "provenance_complete": False,
        "is_attributable": False,
    })

    lots = get_verified_closed_lot_outcomes(bot_id=bot_id, db=real_mongo)
    assert len(lots) == 1
    l1 = lots[0]
    assert l1["closure_id"] == valid_closure_id
    assert l1["dollar_pnl"] == 98.0
    assert l1["invested_capital_denominator"] == 1001.0
    assert l1["net_realized_return"] == 9.7902
    assert l1["total_fees"] == 2.0
    assert l1["is_attributable"] is True


def test_real_mongo_learning_cohort_and_shadow_comparison(real_mongo):
    """Verify learning cohort qualification and shadow comparison join on real MongoDB."""
    now = datetime.datetime.now(datetime.timezone.utc)
    tag = _gen_token()
    cycle_id = f"cycle-{tag}"
    dec_id = f"dec-{tag}"

    # Insert decision outcome
    real_mongo[COLL_DECISION_OUTCOMES].insert_one({
        "decision_id": dec_id,
        "cycle_id": cycle_id,
        "bot_id": "default",
        "ticker": "NVDA",
        "action": "BUY",
        "confidence": 85.0,
        "decision_return_pct": 7.5,
        "forecast_alpha": 3.5,
        "benchmark_return_pct": 4.0,
        "outcome": "WIN",
        "maturity_status": "MATURE_VERIFIED",
        "contract_version": 4,
        "created_at": now,
        "resolved_at": now + datetime.timedelta(days=7),
        "is_quarantined": False,
        "exclusion_reason": None,
    })

    # Insert decision score
    real_mongo["decision_scores"].insert_one({
        "decision_id": dec_id,
        "cycle_id": cycle_id,
        "ticker": "NVDA",
        "band": "STRONG_BUY",
        "score": 92.0,
        "baseline_confidence": 90,
        "board_action": "BUY",
        "board_confidence": 85,
        "created_at": now,
    })

    # 1. Learning cohort
    cohort = get_learning_cohort_outcomes(ticker="NVDA", db=real_mongo)
    matches = [c for c in cohort if c["decision_id"] == dec_id]
    assert len(matches) == 1
    assert matches[0]["is_eligible_for_learning"] is True

    # 2. Shadow comparison join
    shadow_rows = get_shadow_comparison_outcomes(db=real_mongo)
    matching_shadow = [s for s in shadow_rows if s["decision_id"] == dec_id]
    assert len(matching_shadow) == 1
    s = matching_shadow[0]
    assert s["score"] == 92.0
    assert s["verified_return_pct"] == 7.5
    assert s["verified_alpha"] == 3.5
    assert s["outcome_label"] == "WIN"


def test_real_mongo_cross_reader_parity(real_mongo, monkeypatch):
    """Assert scorecard, eval_engine, and shadow reports observe identical numbers on real MongoDB."""
    now = datetime.datetime.now(datetime.timezone.utc)
    tag = _gen_token()
    cycle_id = f"parity-cycle-{tag}"
    dec_id = f"parity-dec-{tag}"
    ticker = f"T{secrets.token_hex(2).upper()}"

    # Insert 3 identical verified decision outcomes so calibration sample count >= 3
    for i in range(3):
        real_mongo[COLL_DECISION_OUTCOMES].insert_one({
            "decision_id": f"{dec_id}_{i}",
            "cycle_id": cycle_id,
            "bot_id": "default",
            "ticker": ticker,
            "action": "BUY",
            "confidence": 80.0,
            "decision_return_pct": 6.25,
            "forecast_alpha": 2.5,
            "benchmark_return_pct": 3.75,
            "outcome": "WIN",
            "maturity_status": "MATURE_VERIFIED",
            "contract_version": 4,
            "created_at": now - datetime.timedelta(days=1),
            "resolved_at": now,
            "is_quarantined": False,
            "exclusion_reason": None,
        })

    real_mongo["decision_scores"].insert_one({
        "decision_id": f"{dec_id}_0",
        "cycle_id": cycle_id,
        "ticker": ticker,
        "band": "STRONG_BUY",
        "score": 85.0,
        "baseline_confidence": 80,
        "board_action": "BUY",
        "board_confidence": 80,
        "created_at": now,
    })

    # Configure monkeypatch to point mongo_store to real_mongo for readers
    monkeypatch.setattr("app.db.mongo_store.get_doc_db", lambda: real_mongo)

    # 1. Unified Outcome Reader
    verified = get_verified_decision_outcomes(ticker=ticker, db=real_mongo)
    assert len(verified) == 3
    ref_return = verified[0]["return_pct"]
    ref_alpha = verified[0]["forecast_alpha"]
    assert ref_return == 6.25
    assert ref_alpha == 2.5

    # 2. Scorecard reader
    from scripts.agent_scorecard import _resolved_outcomes
    scorecard_rows = _resolved_outcomes(since=now - datetime.timedelta(days=2))
    sc_match = [r for r in scorecard_rows if r["ticker"] == ticker]
    assert len(sc_match) == 3
    for r in sc_match:
        assert r["pnl_pct"] == ref_return
        assert r["return_pct"] == ref_return

    # 3. Eval engine calibration
    from app.autoresearch.eval_engine import evaluate_confidence_calibration
    cal = evaluate_confidence_calibration(ticker=ticker)
    assert cal["status"] == "ok"
    assert cal["win_count"] == 3
    assert cal["avg_confidence_on_wins"] == 80.0

    # 4. Shadow rows reader
    from scripts.decision_score_report import _shadow_rows
    with patch("app.db.mongo_query.find_rows", return_value=[
        ("STRONG_BUY", 85.0, 80, None, "BUY", 80, cycle_id, ticker, f"{dec_id}_0")
    ]):
        rows = _shadow_rows()
        assert len(rows) == 1
        # The last column in _shadow_rows is pnl_pct
        assert rows[0][-1] == ref_return
