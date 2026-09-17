"""Unit tests for the Unified Outcome Access Layer (Step 10).

Covers:
1. get_verified_decision_outcomes: standardization, quarantine exclusion, synthetic cycle exclusion.
2. get_verified_closed_lot_outcomes: fee conservation, invested capital, attribution provenance.
3. get_learning_cohort_outcomes: strict learning eligibility for v4 and legacy v2/v3.
4. get_shadow_comparison_outcomes: decision_id matching with (cycle_id, ticker) fallback.
5. Cross-subsystem parity: scorecard, eval_engine calibration, and shadow report observe identical values.
"""

from __future__ import annotations

import datetime
from unittest.mock import MagicMock, patch
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
)


def _make_mock_cursor(items: list[dict]):
    cursor = MagicMock()
    cursor.sort.return_value = cursor
    cursor.limit.side_effect = lambda n: items[:n] if n > 0 else items
    cursor.__iter__.side_effect = lambda: iter(items)
    return cursor


def test_get_verified_decision_outcomes_standardization_and_filtering():
    """Verify standardization of return, alpha, and exclusion of quarantined/synthetic."""
    now = datetime.datetime.now(datetime.timezone.utc)
    raw_docs = [
        # 1. Valid Contract v4 mature verified record
        {
            "_id": "doc1",
            "decision_id": "dec-101",
            "cycle_id": "cycle-v3-real-001",
            "bot_id": "bot-alpha",
            "ticker": "AAPL",
            "action": "BUY",
            "confidence": 85.0,
            "decision_return_pct": 5.4321,
            "forecast_alpha": 2.1234,
            "benchmark_return_pct": 3.3087,
            "outcome": "WIN",
            "maturity_date": now + datetime.timedelta(days=7),
            "created_at": now,
            "resolved_at": now + datetime.timedelta(days=7),
            "contract_version": 4,
            "maturity_status": "MATURE_VERIFIED",
            "is_quarantined": False,
            "exclusion_reason": None,
        },
        # 2. Legacy Contract v3 record with pnl_pct fallback and string dates
        {
            "_id": "doc2",
            "decision_id": "dec-102",
            "cycle_id": "cycle-v3-real-002",
            "bot_id": "bot-alpha",
            "ticker": "MSFT",
            "requested_action": "SELL",
            "confidence": 60.0,
            "pnl_pct": -3.2100,
            "alpha": -1.5000,
            "benchmark_return": -1.7100,
            "created_at": now.isoformat(),
            "resolved_at": (now + datetime.timedelta(days=7)).isoformat(),
            "contract_version": 3,
            "maturity_status": "MATURE",
            "outcome_evidence_state": "verified",
            "entry_price_source": "polygon",
            "exit_price_source": "polygon",
            "is_quarantined": False,
            "exclusion_reason": None,
        },
    ]

    mock_db = MagicMock()
    mock_coll = MagicMock()
    mock_db.__getitem__.side_effect = lambda name: mock_coll if name == COLL_DECISION_OUTCOMES else MagicMock()
    mock_coll.find.return_value = _make_mock_cursor(raw_docs)

    outcomes = get_verified_decision_outcomes(db=mock_db)
    assert len(outcomes) == 2

    # Record 1 assertions
    r1 = outcomes[0]
    assert r1["decision_id"] == "dec-101"
    assert r1["ticker"] == "AAPL"
    assert r1["action"] == "BUY"
    assert r1["confidence"] == 85.0
    assert r1["return_pct"] == 5.4321
    assert r1["pnl_pct"] == 5.4321  # Compatibility alias
    assert r1["forecast_alpha"] == 2.1234
    assert r1["alpha"] == 2.1234    # Compatibility alias
    assert r1["benchmark_return_pct"] == 3.3087
    assert r1["outcome"] == "WIN"
    assert r1["is_eligible_for_learning"] is True

    # Record 2 assertions
    r2 = outcomes[1]
    assert r2["decision_id"] == "dec-102"
    assert r2["ticker"] == "MSFT"
    assert r2["action"] == "SELL"
    assert r2["return_pct"] == -3.21
    assert r2["pnl_pct"] == -3.21
    assert r2["forecast_alpha"] == -1.5
    assert r2["outcome"] == "LOSS"
    assert r2["is_eligible_for_learning"] is True

    # Query structure validation
    query_passed = mock_coll.find.call_args[0][0]
    assert query_passed["is_quarantined"] == {"$ne": True}
    assert query_passed["exclusion_reason"] is None


def test_get_verified_closed_lot_outcomes_provenance_and_fees():
    """Verify closed lot outcome accounting, invested capital, and provenance gating."""
    now = datetime.datetime.now(datetime.timezone.utc)
    raw_lots = [
        {
            "_id": "lot1",
            "closure_id": "cls-001",
            "lot_id": "lot-100",
            "bot_id": "bot-alpha",
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
            "lot_alpha": 5.2,
            "benchmark_return": 4.5902,
            "closed_at": now,
            "origin": "LIVE",
            "provenance_complete": True,
            "is_attributable": True,
        }
    ]

    mock_db = MagicMock()
    mock_coll = MagicMock()
    mock_db.__getitem__.side_effect = lambda name: mock_coll if name == COLL_LOT_CLOSURES else MagicMock()
    mock_coll.find.return_value = _make_mock_cursor(raw_lots)

    lots = get_verified_closed_lot_outcomes(db=mock_db)
    assert len(lots) == 1
    l1 = lots[0]

    assert l1["closure_id"] == "cls-001"
    assert l1["lot_id"] == "lot-100"
    assert l1["ticker"] == "NVDA"
    assert l1["closed_qty"] == 10.0
    assert l1["allocated_entry_fee"] == 1.0
    assert l1["exit_fee"] == 1.0
    assert l1["total_fees"] == 2.0
    assert l1["dollar_pnl"] == 98.0
    assert l1["invested_capital_denominator"] == 1001.0
    assert l1["net_realized_return"] == 9.7902
    assert l1["lot_alpha"] == 5.2
    assert l1["benchmark_return"] == 4.5902
    assert l1["is_attributable"] is True


def test_get_learning_cohort_outcomes_eligibility():
    """Verify learning eligibility filter drops quarantined, unverified, or mismatched vendors."""
    now = datetime.datetime.now(datetime.timezone.utc)
    raw_docs = [
        # Eligible v4
        {
            "decision_id": "d-v4-ok",
            "cycle_id": "c-real-1",
            "ticker": "AAPL",
            "maturity_status": "MATURE_VERIFIED",
            "is_quarantined": False,
            "exclusion_reason": None,
            "decision_return_pct": 4.0,
        },
        # Ineligible: quarantined
        {
            "decision_id": "d-v4-quar",
            "cycle_id": "c-real-2",
            "ticker": "TSLA",
            "maturity_status": "MATURE_VERIFIED",
            "is_quarantined": True,
            "exclusion_reason": None,
            "decision_return_pct": -10.0,
        },
        # Ineligible: synthetic cycle
        {
            "decision_id": "d-v4-synth",
            "cycle_id": "bench-contamination-01",
            "ticker": "GOOG",
            "maturity_status": "MATURE_VERIFIED",
            "is_quarantined": False,
            "exclusion_reason": None,
            "decision_return_pct": 2.0,
        },
        # Ineligible: legacy with mismatched price vendors
        {
            "decision_id": "d-legacy-mismatch",
            "cycle_id": "c-real-3",
            "ticker": "AMZN",
            "maturity_status": "MATURE",
            "outcome_evidence_state": "verified",
            "entry_price_source": "polygon",
            "exit_price_source": "yfinance",
            "is_quarantined": False,
            "exclusion_reason": None,
            "decision_return_pct": 1.5,
        },
        # Eligible: legacy with matching price vendors
        {
            "decision_id": "d-legacy-ok",
            "cycle_id": "c-real-4",
            "ticker": "META",
            "maturity_status": "MATURE",
            "outcome_evidence_state": "verified",
            "entry_price_source": "polygon",
            "exit_price_source": "polygon",
            "is_quarantined": False,
            "exclusion_reason": None,
            "decision_return_pct": 3.0,
        },
    ]

    mock_db = MagicMock()
    mock_coll = MagicMock()
    mock_db.__getitem__.side_effect = lambda name: mock_coll if name == COLL_DECISION_OUTCOMES else MagicMock()
    mock_coll.find.return_value = _make_mock_cursor(raw_docs)

    cohort = get_learning_cohort_outcomes(db=mock_db)
    ids = [o["decision_id"] for o in cohort]
    assert ids == ["d-v4-ok", "d-legacy-ok"]


def test_get_shadow_comparison_outcomes_join_logic():
    """Verify join prioritizes decision_id and falls back to (cycle_id, ticker)."""
    now = datetime.datetime.now(datetime.timezone.utc)
    scores = [
        # Score 1 matches by decision_id
        {
            "_id": "s1",
            "decision_id": "dec-match-id",
            "cycle_id": "cycle-01",
            "ticker": "AAPL",
            "band": "STRONG_BUY",
            "score": 88.5,
            "baseline_confidence": 85,
            "board_action": "BUY",
            "board_confidence": 90,
        },
        # Score 2 matches by (cycle_id, ticker) fallback
        {
            "_id": "s2",
            "decision_id": None,
            "cycle_id": "cycle-02",
            "ticker": "MSFT",
            "band": "HOLD",
            "score": 52.0,
            "baseline_confidence": 50,
            "board_action": "HOLD",
            "board_confidence": 55,
        },
        # Score 3 has no matching outcome
        {
            "_id": "s3",
            "decision_id": "dec-unmatched",
            "cycle_id": "cycle-03",
            "ticker": "NFLX",
            "band": "SELL",
            "score": 30.0,
            "baseline_confidence": 35,
            "board_action": "SELL",
            "board_confidence": 40,
        },
    ]

    outcomes = [
        {
            "decision_id": "dec-match-id",
            "cycle_id": "cycle-01",
            "ticker": "AAPL",
            "decision_return_pct": 6.5,
            "forecast_alpha": 3.2,
            "benchmark_return_pct": 3.3,
            "outcome": "WIN",
            "maturity_status": "MATURE_VERIFIED",
            "is_quarantined": False,
            "exclusion_reason": None,
        },
        {
            "decision_id": "dec-fallback-id",
            "cycle_id": "cycle-02",
            "ticker": "MSFT",
            "decision_return_pct": -2.0,
            "forecast_alpha": -0.5,
            "benchmark_return_pct": -1.5,
            "outcome": "LOSS",
            "maturity_status": "MATURE_VERIFIED",
            "is_quarantined": False,
            "exclusion_reason": None,
        },
    ]

    mock_db = MagicMock()
    def _coll_router(name):
        coll = MagicMock()
        if name == "decision_scores":
            coll.find.return_value = _make_mock_cursor(scores)
        elif name == COLL_DECISION_OUTCOMES:
            coll.find.return_value = _make_mock_cursor(outcomes)
        return coll

    mock_db.__getitem__.side_effect = _coll_router

    joined = get_shadow_comparison_outcomes(db=mock_db)
    assert len(joined) == 3

    # Score 1: joined on decision_id
    j1 = next(j for j in joined if j["ticker"] == "AAPL")
    assert j1["decision_id"] == "dec-match-id"
    assert j1["score"] == 88.5
    assert j1["verified_return_pct"] == 6.5
    assert j1["verified_alpha"] == 3.2
    assert j1["outcome_label"] == "WIN"

    # Score 2: joined on (cycle_id, ticker) fallback
    j2 = next(j for j in joined if j["ticker"] == "MSFT")
    assert j2["decision_id"] == "dec-fallback-id"
    assert j2["score"] == 52.0
    assert j2["verified_return_pct"] == -2.0
    assert j2["verified_alpha"] == -0.5
    assert j2["outcome_label"] == "LOSS"

    # Score 3: unmatched
    j3 = next(j for j in joined if j["ticker"] == "NFLX")
    assert j3["score"] == 30.0
    assert j3["verified_return_pct"] is None
    assert j3["verified_alpha"] is None
    assert j3["outcome_label"] is None


def test_cross_subsystem_reader_parity():
    """Verify that scorecard, eval_engine, and decision_score_report observe 100% identical values."""
    now = datetime.datetime.now(datetime.timezone.utc)
    verified_doc = {
        "decision_id": "parity-dec-001",
        "cycle_id": "parity-cycle-001",
        "bot_id": "default",
        "ticker": "NVDA",
        "action": "BUY",
        "confidence": 75.0,
        "decision_return_pct": 8.125,
        "forecast_alpha": 4.5,
        "benchmark_return_pct": 3.625,
        "outcome": "WIN",
        "maturity_status": "MATURE_VERIFIED",
        "created_at": now,
        "resolved_at": now + datetime.timedelta(days=7),
        "is_quarantined": False,
        "exclusion_reason": None,
    }

    mock_db = MagicMock()
    mock_coll = MagicMock()
    mock_db.__getitem__.side_effect = lambda name: mock_coll if name == COLL_DECISION_OUTCOMES else MagicMock()
    mock_coll.find.return_value = _make_mock_cursor([verified_doc])

    # 1. Direct reader
    outcomes = get_verified_decision_outcomes(db=mock_db)
    assert len(outcomes) == 1
    assert outcomes[0]["pnl_pct"] == 8.125
    assert outcomes[0]["return_pct"] == 8.125
    assert outcomes[0]["forecast_alpha"] == 4.5

    # 2. agent_scorecard reader
    from scripts.agent_scorecard import _resolved_outcomes
    with patch("app.trading.attribution.outcome_reader.mongo_store.get_doc_db", return_value=mock_db):
        scorecard_outcomes = _resolved_outcomes(since="2026-01-01")
        assert len(scorecard_outcomes) == 1
        assert scorecard_outcomes[0]["pnl_pct"] == outcomes[0]["pnl_pct"]
        assert scorecard_outcomes[0]["return_pct"] == outcomes[0]["return_pct"]

    # 3. eval_engine confidence calibration reader
    from app.autoresearch.eval_engine import evaluate_confidence_calibration
    with patch("app.trading.attribution.outcome_reader.mongo_store.get_doc_db", return_value=mock_db):
        # We need at least 3 samples to calculate calibration score, so provide 3 identical
        mock_coll.find.return_value = _make_mock_cursor([verified_doc, verified_doc, verified_doc])
        cal = evaluate_confidence_calibration(ticker="NVDA")
        assert cal["status"] == "ok"
        assert cal["win_count"] == 3
        assert cal["avg_confidence_on_wins"] == 75.0
        assert cal["calibration_score"] == 75.0

    # 4. decision_score_report shadow reader
    from scripts.decision_score_report import _shadow_rows
    mock_scores_doc = [
        ("STRONG_BUY", 85.0, 75, 2.5, "BUY", 80, "parity-cycle-001", "NVDA", "parity-dec-001")
    ]
    with patch("app.db.mongo_query.find_rows", return_value=mock_scores_doc), \
         patch("app.trading.attribution.outcome_reader.mongo_store.get_doc_db", return_value=mock_db):
        mock_coll.find.return_value = _make_mock_cursor([verified_doc])
        rows = _shadow_rows()
        assert len(rows) == 1
        # The last element is pnl_pct
        assert rows[0][-1] == outcomes[0]["pnl_pct"]
