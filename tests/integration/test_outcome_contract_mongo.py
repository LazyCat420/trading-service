"""Integration Test Suite: Outcome Contract Persistence and Physical Mapping.

Verifies:
1. Physical persistence and round-trip retrieval of DecisionOutcomeRecordV4 in decision_outcomes.
2. Physical persistence and round-trip retrieval of LotClosureRecordV4 in lot_closures.
3. Additive compatibility: Legacy v3 and canonical v4 records queryable together in decision_outcomes.
4. Physical mapping verification for all entries in LOGICAL_TO_PHYSICAL_COLLECTIONS.
5. Report slice aggregation across real MongoDB collections.
"""

from __future__ import annotations

import datetime
import secrets
import pytest

from app.trading.attribution.models import DecisionOutcomeRecord
from app.trading.attribution.outcome_contract import (
    AdjustmentConvention,
    BenchmarkSpec,
    DecisionOutcomeRecordV4,
    ExclusionReason,
    HorizonSpec,
    LOGICAL_TO_PHYSICAL_COLLECTIONS,
    LotClosureRecordV4,
    MarketCalendar,
    MaturityStatus,
    OutcomeClaimType,
    OutcomeReportSlice,
    PriceObservation,
    distinguish_action,
    is_eligible_for_learning,
)

pytestmark = pytest.mark.real_mongo


def test_outcome_v4_mongo_persistence_roundtrip(real_mongo):
    """Verifies storing and retrieving a DecisionOutcomeRecordV4 in MongoDB decision_outcomes."""
    col_name = LOGICAL_TO_PHYSICAL_COLLECTIONS["decision_outcomes"]
    col = real_mongo[col_name]

    now = datetime.datetime.now(datetime.timezone.utc)
    entry_dt = now - datetime.timedelta(days=7)
    decision_id = f"dec-{secrets.token_hex(8)}"
    outcome_id = f"out-{decision_id}-v4"

    bm_spec = BenchmarkSpec.resolve("AAPL")
    horizon_spec = HorizonSpec(horizon_value=7, calendar=MarketCalendar.US_EQUITY)

    record = DecisionOutcomeRecordV4(
        outcome_id=outcome_id,
        decision_id=decision_id,
        bot_id=f"bot-{secrets.token_hex(6)}",
        execution_intent_id=f"int-{secrets.token_hex(6)}",
        fill_ids=[f"fill-{secrets.token_hex(6)}"],
        evaluation_contract_version=4,
        claim_type=OutcomeClaimType.PROPOSAL_DIRECTION,
        action_classification="IMMEDIATE_BUY",
        ticker="AAPL",
        horizon_spec=horizon_spec,
        benchmark_spec=bm_spec,
        entry_observation=PriceObservation(
            price=150.0,
            date=entry_dt,
            source="alpaca_daily",
            adjustment_convention=AdjustmentConvention.SPLIT_ADJUSTED,
        ),
        horizon_observation=PriceObservation(
            price=165.0,
            date=now,
            source="alpaca_daily",
            adjustment_convention=AdjustmentConvention.SPLIT_ADJUSTED,
        ),
        benchmark_entry=PriceObservation(price=400.0, date=entry_dt, source="alpaca_daily"),
        benchmark_horizon=PriceObservation(price=420.0, date=now, source="alpaca_daily"),
        maturity_status=MaturityStatus.MATURE_VERIFIED,
        decision_return=10.0,
        benchmark_return=5.0,
        forecast_alpha=5.0,
        executed_return=9.8,
        realized_net_alpha=4.8,
        is_eligible_for_learning=True,
        created_at=entry_dt,
        maturity_date=now,
        resolved_at=now,
    )

    doc = record.model_dump(mode="python")
    col.insert_one(doc)

    fetched = col.find_one({"outcome_id": outcome_id})
    assert fetched is not None
    assert fetched["evaluation_contract_version"] == 4
    assert fetched["claim_type"] == OutcomeClaimType.PROPOSAL_DIRECTION.value
    assert fetched["forecast_alpha"] == 5.0
    assert fetched["realized_net_alpha"] == 4.8

    # Validate model reconstruction
    reconstructed = DecisionOutcomeRecordV4.model_validate(fetched)
    assert reconstructed.outcome_id == outcome_id
    assert reconstructed.forecast_alpha == 5.0
    assert reconstructed.decision_alpha == 5.0  # backward compat property
    assert is_eligible_for_learning(reconstructed) is True


def test_lot_closure_v4_mongo_persistence_roundtrip(real_mongo):
    """Verifies storing and retrieving a LotClosureRecordV4 in MongoDB lot_closures."""
    col_name = LOGICAL_TO_PHYSICAL_COLLECTIONS["lot_closures"]
    col = real_mongo[col_name]

    now = datetime.datetime.now(datetime.timezone.utc)
    opened = now - datetime.timedelta(days=4)
    closure_id = f"close-{secrets.token_hex(8)}"
    lot_id = f"lot-{secrets.token_hex(8)}"

    lot_record = LotClosureRecordV4(
        closure_id=closure_id,
        lot_id=lot_id,
        bot_id=f"bot-{secrets.token_hex(6)}",
        ticker="MSFT",
        closed_qty=25.0,
        entry_price=400.0,
        exit_price=420.0,
        allocated_entry_fee=2.50,
        exit_fee=2.50,
        fees_embedded_in_fills=False,
        invested_capital_denominator=10002.50,
        dollar_pnl=495.0,
        net_realized_return=4.9488,
        benchmark_spec=BenchmarkSpec.resolve("MSFT"),
        benchmark_entry=PriceObservation(price=500.0, date=opened, source="alpaca"),
        benchmark_exit=PriceObservation(price=510.0, date=now, source="alpaca"),
        benchmark_return=2.0,
        realized_net_alpha=2.9488,
        provenance="LIVE",
        provenance_complete=True,
        is_attributable=True,
        opened_at=opened,
        closed_at=now,
        evaluated_at=now,
    )

    doc = lot_record.model_dump(mode="python")
    col.insert_one(doc)

    fetched = col.find_one({"closure_id": closure_id})
    assert fetched is not None
    assert fetched["dollar_pnl"] == 495.0
    assert fetched["net_realized_return"] == 4.9488
    assert fetched["realized_net_alpha"] == 2.9488

    reconstructed = LotClosureRecordV4.model_validate(fetched)
    assert reconstructed.closure_id == closure_id
    assert reconstructed.is_attributable is True


def test_additive_compatibility_v3_and_v4_coexistence(real_mongo):
    """Verifies that v3 and v4 documents coexist in the same decision_outcomes collection."""
    col_name = LOGICAL_TO_PHYSICAL_COLLECTIONS["decision_outcomes"]
    col = real_mongo[col_name]

    now = datetime.datetime.now(datetime.timezone.utc)
    v3_id = f"out-v3-{secrets.token_hex(6)}"
    v4_id = f"out-v4-{secrets.token_hex(6)}"

    # Legacy v3 doc
    v3_doc = {
        "outcome_id": v3_id,
        "decision_id": f"dec-{secrets.token_hex(6)}",
        "evaluation_contract_version": 3,
        "horizon_days": 7,
        "benchmark_symbol": "SPY",
        "entry_observation": {"price": 100.0, "source": "alpaca"},
        "horizon_observation": {"price": 110.0, "source": "alpaca"},
        "benchmark_entry": {"price": 400.0, "source": "alpaca"},
        "benchmark_horizon": {"price": 420.0, "source": "alpaca"},
        "maturity_status": "MATURE",
        "decision_return": 10.0,
        "benchmark_return": 5.0,
        "decision_alpha": 5.0,
        "resolved_at": now,
    }
    col.insert_one(v3_doc)

    # Canonical v4 doc
    v4_record = DecisionOutcomeRecordV4(
        outcome_id=v4_id,
        decision_id=f"dec-{secrets.token_hex(6)}",
        evaluation_contract_version=4,
        claim_type=OutcomeClaimType.FLAT_WAIT,
        action_classification="FLAT_WAIT",
        ticker="SPY",
        horizon_spec=HorizonSpec(horizon_value=7),
        benchmark_spec=BenchmarkSpec.resolve("SPY"),
        entry_observation=PriceObservation(price=400.0, date=now, source="alpaca"),
        horizon_observation=PriceObservation(price=395.0, date=now, source="alpaca"),
        benchmark_entry=PriceObservation(price=400.0, date=now, source="alpaca"),
        benchmark_horizon=PriceObservation(price=395.0, date=now, source="alpaca"),
        maturity_status=MaturityStatus.MATURE_VERIFIED,
        decision_return=1.25,
        benchmark_return=-1.25,
        forecast_alpha=2.5,
        is_eligible_for_learning=True,
        created_at=now,
        maturity_date=now,
    )
    col.insert_one(v4_record.model_dump(mode="python"))

    # Query all outcomes for both versions
    results = list(col.find({"outcome_id": {"$in": [v3_id, v4_id]}}))
    assert len(results) == 2

    # Verify v3 reader handles v3
    v3_found = next(r for r in results if r["outcome_id"] == v3_id)
    v3_parsed = DecisionOutcomeRecord.model_validate(v3_found)
    assert v3_parsed.decision_alpha == 5.0
    # Adapt to v4
    adapted = v3_parsed.to_v4(ticker="TEST")
    assert adapted.forecast_alpha == 5.0

    # Verify v4 reader handles v4
    v4_found = next(r for r in results if r["outcome_id"] == v4_id)
    v4_parsed = DecisionOutcomeRecordV4.model_validate(v4_found)
    assert v4_parsed.forecast_alpha == 2.5
    assert v4_parsed.claim_type == OutcomeClaimType.FLAT_WAIT


def test_report_slice_aggregation_in_mongo(real_mongo):
    """Verifies aggregation pipeline computing report slice counts across states."""
    col = real_mongo[LOGICAL_TO_PHYSICAL_COLLECTIONS["decision_outcomes"]]
    now = datetime.datetime.now(datetime.timezone.utc)
    tag = secrets.token_hex(4)

    # Insert a cohort of records
    docs = [
        # Mature
        {"outcome_id": f"s-{tag}-1", "decision_id": f"d-{tag}-1", "ticker": "AAPL",
         "maturity_status": MaturityStatus.MATURE_VERIFIED.value, "evaluation_contract_version": 4,
         "claim_type": "proposal_direction", "forecast_alpha": 4.0},
        # Due Unresolved
        {"outcome_id": f"s-{tag}-2", "decision_id": f"d-{tag}-2", "ticker": "AAPL",
         "maturity_status": MaturityStatus.DUE_UNRESOLVED.value, "evaluation_contract_version": 4,
         "claim_type": "proposal_direction", "exclusion_reason": ExclusionReason.MISSING_BENCHMARK_BAR.value},
        # Excluded
        {"outcome_id": f"s-{tag}-3", "decision_id": f"d-{tag}-3", "ticker": "AAPL",
         "maturity_status": MaturityStatus.EXCLUDED.value, "evaluation_contract_version": 4,
         "claim_type": "proposal_direction", "exclusion_reason": ExclusionReason.DELISTED_OR_SUSPENDED.value},
        # Not yet due
        {"outcome_id": f"s-{tag}-4", "decision_id": f"d-{tag}-4", "ticker": "AAPL",
         "maturity_status": MaturityStatus.NOT_YET_DUE.value, "evaluation_contract_version": 4,
         "claim_type": "proposal_direction"},
    ]
    col.insert_many(docs)

    pipeline = [
        {"$match": {"decision_id": {"$regex": f"^d-{tag}"}}},
        {"$group": {
            "_id": "$maturity_status",
            "count": {"$sum": 1},
        }},
    ]
    agg_res = {r["_id"]: r["count"] for r in col.aggregate(pipeline)}

    assert agg_res.get(MaturityStatus.MATURE_VERIFIED.value) == 1
    assert agg_res.get(MaturityStatus.DUE_UNRESOLVED.value) == 1
    assert agg_res.get(MaturityStatus.EXCLUDED.value) == 1
    assert agg_res.get(MaturityStatus.NOT_YET_DUE.value) == 1
