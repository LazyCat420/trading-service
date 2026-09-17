"""Integration Test Suite: Maturity Scheduling and Recovery on Real MongoDB.

Exercises real queries, indexes, pagination, and crash recovery against MongoDB replica set (rs0):
1. Real Mongo mixed 1/7/30-day horizons due-work scheduling.
2. Real Mongo pagination over >50 records with restart recovery and checkpoints.
3. Real Mongo malformed document quarantine without starvation.
4. Real Mongo older ineligible records (>50) do not starve due records.
"""

from __future__ import annotations

import datetime
import secrets
import pytest

from app.trading.attribution.models import DecisionArtifact, OutcomeMaturityStatus
from app.trading.attribution.outcome_contract import HorizonSpec, MarketCalendar
from app.trading.attribution.repository import (
    COLL_ATTRIBUTION_REPORTS,
    COLL_DECISION_ARTIFACTS,
    COLL_DECISION_OUTCOMES,
    COLL_DECISION_QUARANTINE,
    COLL_EVALUATION_CHECKPOINTS,
    ensure_attribution_indexes,
    get_evaluation_checkpoint,
)
from app.trading.attribution.worker import (
    run_mature_outcome_evaluation_iteration,
)

pytestmark = pytest.mark.real_mongo


def test_real_mongo_mixed_horizons_scheduling(real_mongo, monkeypatch):
    """Verifies that on real Mongo, 1-day mature decisions are processed while 7d and 30d wait."""
    ensure_attribution_indexes()
    col_da = real_mongo[COLL_DECISION_ARTIFACTS]
    col_do = real_mongo[COLL_DECISION_OUTCOMES]
    col_ph = real_mongo["price_history"]

    now = datetime.datetime(2026, 9, 17, 14, 0, 0, tzinfo=datetime.timezone.utc)
    ticker_1d = f"T1D_{secrets.token_hex(4).upper()}"
    ticker_7d = f"T7D_{secrets.token_hex(4).upper()}"
    ticker_30d = f"T30D_{secrets.token_hex(4).upper()}"
    bm_symbol = "SPY"

    # Insert price history bars for entry and horizon of 1-day decision
    entry_1d = now - datetime.timedelta(days=2)
    entry_bar_dt = datetime.datetime.combine(entry_1d.date(), datetime.time.min, tzinfo=datetime.timezone.utc)
    horizon_bar_dt = datetime.datetime.combine((entry_1d + datetime.timedelta(days=1)).date(), datetime.time.min, tzinfo=datetime.timezone.utc)

    col_ph.insert_many([
        {"ticker": ticker_1d, "date": entry_bar_dt, "close": 100.0, "source": "alpaca", "adjustment_convention": "SPLIT_ADJUSTED"},
        {"ticker": ticker_1d, "date": horizon_bar_dt, "close": 110.0, "source": "alpaca", "adjustment_convention": "SPLIT_ADJUSTED"},
        {"ticker": bm_symbol, "date": entry_bar_dt, "close": 400.0, "source": "alpaca", "adjustment_convention": "SPLIT_ADJUSTED"},
        {"ticker": bm_symbol, "date": horizon_bar_dt, "close": 420.0, "source": "alpaca", "adjustment_convention": "SPLIT_ADJUSTED"},
    ])

    # 1-day horizon created 2 days ago (due)
    dec_1d = DecisionArtifact(
        decision_id=f"dec-1d-{secrets.token_hex(6)}",
        cycle_id="c1",
        ticker=ticker_1d,
        producer="v3_decision_synthesizer",
        model="local",
        requested_action="BUY",
        confidence=85,
        declared_horizon_days=1,
        created_at=entry_1d,
        reference_quote={"price": 100.0, "source": "alpaca"},
        benchmark_symbol=bm_symbol,
    )

    # 7-day horizon created 4 days ago (immature)
    dec_7d = DecisionArtifact(
        decision_id=f"dec-7d-{secrets.token_hex(6)}",
        cycle_id="c1",
        ticker=ticker_7d,
        producer="v3_decision_synthesizer",
        model="local",
        requested_action="BUY",
        confidence=85,
        declared_horizon_days=7,
        created_at=now - datetime.timedelta(days=4),
        reference_quote={"price": 200.0, "source": "alpaca"},
        benchmark_symbol=bm_symbol,
    )

    # 30-day horizon created 10 days ago (immature)
    dec_30d = DecisionArtifact(
        decision_id=f"dec-30d-{secrets.token_hex(6)}",
        cycle_id="c1",
        ticker=ticker_30d,
        producer="v3_decision_synthesizer",
        model="local",
        requested_action="BUY",
        confidence=85,
        declared_horizon_days=30,
        created_at=now - datetime.timedelta(days=10),
        reference_quote={"price": 300.0, "source": "alpaca"},
        benchmark_symbol=bm_symbol,
    )

    col_da.insert_many([
        dec_1d.model_dump(mode="python"),
        dec_7d.model_dump(mode="python"),
        dec_30d.model_dump(mode="python"),
    ])

    # Run iteration
    evaluated_count = run_mature_outcome_evaluation_iteration(limit=50, now=now)
    assert evaluated_count == 1

    # Check 1d decision is maturely evaluated on real Mongo
    doc_1d = col_da.find_one({"decision_id": dec_1d.decision_id})
    assert doc_1d["outcome_status"] == OutcomeMaturityStatus.MATURE.value

    # Check 7d and 30d decisions are untouched
    doc_7d = col_da.find_one({"decision_id": dec_7d.decision_id})
    assert doc_7d["outcome_status"] is None

    doc_30d = col_da.find_one({"decision_id": dec_30d.decision_id})
    assert doc_30d["outcome_status"] is None


def test_real_mongo_pagination_and_restart_recovery(real_mongo, monkeypatch):
    """Verifies real Mongo pagination across batches >50 records with restart recovery and checkpoints."""
    ensure_attribution_indexes()
    col_da = real_mongo[COLL_DECISION_ARTIFACTS]
    col_ph = real_mongo["price_history"]

    now = datetime.datetime(2026, 9, 17, 14, 0, 0, tzinfo=datetime.timezone.utc)
    bm_symbol = "SPY"
    ticker = f"PAG_{secrets.token_hex(4).upper()}"

    entry_dt = now - datetime.timedelta(days=3)
    entry_bar_dt = datetime.datetime.combine(entry_dt.date(), datetime.time.min, tzinfo=datetime.timezone.utc)
    horizon_bar_dt = datetime.datetime.combine((entry_dt + datetime.timedelta(days=1)).date(), datetime.time.min, tzinfo=datetime.timezone.utc)

    col_ph.insert_many([
        {"ticker": ticker, "date": entry_bar_dt, "close": 50.0, "source": "alpaca", "adjustment_convention": "SPLIT_ADJUSTED"},
        {"ticker": ticker, "date": horizon_bar_dt, "close": 55.0, "source": "alpaca", "adjustment_convention": "SPLIT_ADJUSTED"},
        {"ticker": bm_symbol, "date": entry_bar_dt, "close": 400.0, "source": "alpaca", "adjustment_convention": "SPLIT_ADJUSTED"},
        {"ticker": bm_symbol, "date": horizon_bar_dt, "close": 410.0, "source": "alpaca", "adjustment_convention": "SPLIT_ADJUSTED"},
    ])

    # Insert 65 due records (declared_horizon_days=1, created 3 days ago)
    test_run_id = secrets.token_hex(4)
    records = []
    for i in range(65):
        art = DecisionArtifact(
            decision_id=f"dec-pag-{test_run_id}-{i:03d}",
            cycle_id=f"c_pag_{test_run_id}",
            ticker=ticker,
            producer="v3_decision_synthesizer",
            model="local",
            requested_action="BUY",
            confidence=80,
            declared_horizon_days=1,
            created_at=entry_dt + datetime.timedelta(seconds=i),
            reference_quote={"price": 50.0, "source": "alpaca"},
            benchmark_symbol=bm_symbol,
        )
        records.append(art.model_dump(mode="python"))

    col_da.insert_many(records)

    # Batch 1: limit 50 -> evaluates exactly 50
    b1_count = run_mature_outcome_evaluation_iteration(limit=50, now=now)
    assert b1_count == 50

    ckpt1 = get_evaluation_checkpoint("outcome_worker")
    assert ckpt1 is not None
    assert ckpt1["records_processed"] == 50

    # Simulate process restart / Batch 2: limit 50 -> evaluates remaining 15 without repeating the first 50
    b2_count = run_mature_outcome_evaluation_iteration(limit=50, now=now)
    assert b2_count == 15

    ckpt2 = get_evaluation_checkpoint("outcome_worker")
    assert ckpt2["records_processed"] == 15

    # Batch 3: queue exhausted -> evaluates 0
    b3_count = run_mature_outcome_evaluation_iteration(limit=50, now=now)
    assert b3_count == 0


def test_real_mongo_malformed_artifact_quarantine(real_mongo, monkeypatch):
    """Verifies that malformed documents in real Mongo are quarantined and do not block valid records."""
    ensure_attribution_indexes()
    col_da = real_mongo[COLL_DECISION_ARTIFACTS]
    col_dq = real_mongo[COLL_DECISION_QUARANTINE]
    col_ph = real_mongo["price_history"]

    now = datetime.datetime(2026, 9, 17, 14, 0, 0, tzinfo=datetime.timezone.utc)
    ticker = f"VAL_{secrets.token_hex(4).upper()}"
    bm_symbol = "SPY"

    entry_dt = now - datetime.timedelta(days=2)
    entry_bar_dt = datetime.datetime.combine(entry_dt.date(), datetime.time.min, tzinfo=datetime.timezone.utc)
    horizon_bar_dt = datetime.datetime.combine((entry_dt + datetime.timedelta(days=1)).date(), datetime.time.min, tzinfo=datetime.timezone.utc)

    col_ph.insert_many([
        {"ticker": ticker, "date": entry_bar_dt, "close": 100.0, "source": "alpaca", "adjustment_convention": "SPLIT_ADJUSTED"},
        {"ticker": ticker, "date": horizon_bar_dt, "close": 105.0, "source": "alpaca", "adjustment_convention": "SPLIT_ADJUSTED"},
        {"ticker": bm_symbol, "date": entry_bar_dt, "close": 400.0, "source": "alpaca", "adjustment_convention": "SPLIT_ADJUSTED"},
        {"ticker": bm_symbol, "date": horizon_bar_dt, "close": 410.0, "source": "alpaca", "adjustment_convention": "SPLIT_ADJUSTED"},
    ])

    run_id = secrets.token_hex(4)
    malformed_id = f"dec-corrupt-{run_id}"

    # Insert raw corrupt document missing non-null required fields
    col_da.insert_one({
        "decision_id": malformed_id,
        "ticker": "CORRUPT",
        # missing cycle_id, producer, model, requested_action, confidence
        "created_at": entry_dt - datetime.timedelta(hours=5),
        "declared_horizon_days": 1,
        "maturity_date": entry_dt - datetime.timedelta(hours=4),
        "is_quarantined": False,
    })

    # Insert valid due document
    valid_id = f"dec-valid-{run_id}"
    valid_art = DecisionArtifact(
        decision_id=valid_id,
        cycle_id=f"c_val_{run_id}",
        ticker=ticker,
        producer="v3_decision_synthesizer",
        model="local",
        requested_action="BUY",
        confidence=85,
        declared_horizon_days=1,
        created_at=entry_dt,
        reference_quote={"price": 100.0, "source": "alpaca"},
        benchmark_symbol=bm_symbol,
    )
    col_da.insert_one(valid_art.model_dump(mode="python"))

    # Run iteration
    evaluated_count = run_mature_outcome_evaluation_iteration(limit=50, now=now)
    assert evaluated_count == 1

    # Corrupt document must be quarantined in real Mongo
    corrupt_doc = col_da.find_one({"decision_id": malformed_id})
    assert corrupt_doc["is_quarantined"] is True
    assert corrupt_doc["outcome_status"] == "QUARANTINED"
    assert "VALIDATION_ERROR" in corrupt_doc["quarantine_reason"]

    # Entry in decision_quarantine collection
    quar_entry = col_dq.find_one({"decision_id": malformed_id})
    assert quar_entry is not None
    assert "VALIDATION_ERROR" in quar_entry["reason"]

    # Valid document must be evaluated to MATURE
    valid_doc = col_da.find_one({"decision_id": valid_id})
    assert valid_doc["outcome_status"] == OutcomeMaturityStatus.MATURE.value


def test_real_mongo_older_ineligible_does_not_starve_due_work(real_mongo, monkeypatch):
    """Verifies that >50 older immature records in real Mongo do not starve a due 1-day record."""
    ensure_attribution_indexes()
    col_da = real_mongo[COLL_DECISION_ARTIFACTS]
    col_ph = real_mongo["price_history"]

    now = datetime.datetime(2026, 9, 17, 14, 0, 0, tzinfo=datetime.timezone.utc)
    ticker = f"STARVE_{secrets.token_hex(4).upper()}"
    bm_symbol = "SPY"

    entry_dt = now - datetime.timedelta(days=2)
    entry_bar_dt = datetime.datetime.combine(entry_dt.date(), datetime.time.min, tzinfo=datetime.timezone.utc)
    horizon_bar_dt = datetime.datetime.combine((entry_dt + datetime.timedelta(days=1)).date(), datetime.time.min, tzinfo=datetime.timezone.utc)

    col_ph.insert_many([
        {"ticker": ticker, "date": entry_bar_dt, "close": 200.0, "source": "alpaca", "adjustment_convention": "SPLIT_ADJUSTED"},
        {"ticker": ticker, "date": horizon_bar_dt, "close": 210.0, "source": "alpaca", "adjustment_convention": "SPLIT_ADJUSTED"},
        {"ticker": bm_symbol, "date": entry_bar_dt, "close": 400.0, "source": "alpaca", "adjustment_convention": "SPLIT_ADJUSTED"},
        {"ticker": bm_symbol, "date": horizon_bar_dt, "close": 410.0, "source": "alpaca", "adjustment_convention": "SPLIT_ADJUSTED"},
    ])

    run_id = secrets.token_hex(4)

    # Insert 55 older immature records (30-day horizon created 15 days ago -> matures at day 30)
    older_records = []
    for i in range(55):
        art = DecisionArtifact(
            decision_id=f"dec-immature-{run_id}-{i:03d}",
            cycle_id=f"c_imm_{run_id}",
            ticker=ticker,
            producer="v3_decision_synthesizer",
            model="local",
            requested_action="BUY",
            confidence=70,
            declared_horizon_days=30,
            created_at=now - datetime.timedelta(days=15),
            reference_quote={"price": 200.0, "source": "alpaca"},
            benchmark_symbol=bm_symbol,
        )
        older_records.append(art.model_dump(mode="python"))

    col_da.insert_many(older_records)

    # Insert 1 newer due record (1-day horizon created 2 days ago -> mature)
    due_id = f"dec-due-{run_id}"
    due_art = DecisionArtifact(
        decision_id=due_id,
        cycle_id=f"c_due_{run_id}",
        ticker=ticker,
        producer="v3_decision_synthesizer",
        model="local",
        requested_action="BUY",
        confidence=90,
        declared_horizon_days=1,
        created_at=entry_dt,
        reference_quote={"price": 200.0, "source": "alpaca"},
        benchmark_symbol=bm_symbol,
    )
    col_da.insert_one(due_art.model_dump(mode="python"))

    # Run iteration with limit=50
    evaluated_count = run_mature_outcome_evaluation_iteration(limit=50, now=now)
    assert evaluated_count == 1

    # The due target MUST be mature on real Mongo!
    due_doc = col_da.find_one({"decision_id": due_id})
    assert due_doc["outcome_status"] == OutcomeMaturityStatus.MATURE.value
