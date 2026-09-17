"""Integration Test Suite: Horizon Price Provenance on Real MongoDB.

Exercises real queries against MongoDB replica set (rs0) price_history and decision_outcomes:
1. Real Mongo source-pinned observation lookup across weekend/holiday boundaries.
2. Missing horizon bar on real Mongo never falls back to live quote.
3. Multi-vendor source pinning: Pinned source matches; competing vendor ignored.
4. Idempotency and deterministic replay on real Mongo.
"""

from __future__ import annotations

import datetime
import secrets
import pytest

from app.trading.attribution.models import DecisionArtifact, OutcomeMaturityStatus
from app.trading.attribution.outcome_contract import (
    AdjustmentConvention,
    ExclusionReason,
    MarketCalendar,
)
from app.trading.attribution.provenance import get_source_pinned_observation
from app.trading.attribution.worker import (
    COLL_DECISION_ARTIFACTS,
    COLL_DECISION_OUTCOMES,
    evaluate_decision_at_horizon,
)

pytestmark = pytest.mark.real_mongo


def test_real_mongo_weekend_and_holiday_provenance(real_mongo):
    """Verifies that get_source_pinned_observation finds Friday's bar over a weekend in real MongoDB."""
    col = real_mongo["price_history"]
    ticker = f"TEST_SYM_{secrets.token_hex(4).upper()}"

    # Insert Friday close bar at 00:00 UTC
    friday_bar = datetime.datetime(2026, 9, 4, 0, 0, tzinfo=datetime.timezone.utc)
    col.insert_one({
        "ticker": ticker,
        "date": friday_bar,
        "close": 210.50,
        "source": "alpaca_pro",
        "adjustment_convention": "SPLIT_ADJUSTED",
    })

    # Target date is Sunday 2026-09-06 at 12:00 UTC
    target_sunday = datetime.datetime(2026, 9, 6, 12, 0, tzinfo=datetime.timezone.utc)

    obs = get_source_pinned_observation(
        ticker=ticker,
        target_dt=target_sunday,
        pinned_source="alpaca_pro",
        calendar=MarketCalendar.US_EQUITY,
        max_age_days=5,
    )

    assert obs is not None
    assert obs.price == 210.50
    assert obs.source == "alpaca_pro"
    assert obs.date == friday_bar
    assert obs.adjustment_convention == AdjustmentConvention.SPLIT_ADJUSTED
    assert obs.vendor_hash != ""


def test_real_mongo_no_current_quote_substitution(real_mongo, monkeypatch):
    """Verifies that missing horizon bar in real MongoDB price_history fails closed without live quote fallback."""
    ticker = f"NO_BAR_{secrets.token_hex(4).upper()}"
    entry_time = datetime.datetime(2026, 8, 1, 21, 0, tzinfo=datetime.timezone.utc)
    horizon_days = 7
    maturity_date = entry_time + datetime.timedelta(days=horizon_days)
    eval_time = datetime.datetime(2026, 8, 15, 12, 0, tzinfo=datetime.timezone.utc)

    artifact = DecisionArtifact(
        decision_id=f"dec-live-sub-{secrets.token_hex(6)}",
        cycle_id="cycle-1",
        ticker=ticker,
        requested_action="BUY",
        reference_quote={"price": 100.0, "source": "alpaca"},
        producer="test_agent",
        model="test_model",
        confidence=80,
        created_at=entry_time,
        declared_horizon_days=horizon_days,
        benchmark_symbol="SPY",
    )

    # Mock live quote to verify it is NEVER substituted
    monkeypatch.setattr("app.trading.attribution.worker._get_current_price", lambda sym: (999.99, "live_feed"))

    outcome = evaluate_decision_at_horizon(artifact, now=eval_time)
    assert outcome is not None
    assert outcome.maturity_status == OutcomeMaturityStatus.UNRESOLVED
    assert outcome.decision_return is None

    stored = real_mongo[COLL_DECISION_OUTCOMES].find_one({"outcome_id": outcome.outcome_id})
    assert stored is not None
    assert stored["maturity_status"] == "UNRESOLVED"
    assert stored["exclusion_reason"] == ExclusionReason.STALE_HORIZON_BAR.value


def test_real_mongo_source_pinning_multi_vendor(real_mongo):
    """Verifies that if price_history has bars from multiple vendors, only the pinned source is returned."""
    col = real_mongo["price_history"]
    ticker = f"MULTI_SRC_{secrets.token_hex(4).upper()}"

    bar_date = datetime.datetime(2026, 9, 1, 0, 0, tzinfo=datetime.timezone.utc)
    target_dt = datetime.datetime(2026, 9, 1, 21, 0, tzinfo=datetime.timezone.utc)

    # Insert two bars for the same date with different sources
    col.insert_many([
        {"ticker": ticker, "date": bar_date, "close": 100.0, "source": "alpaca"},
        {"ticker": ticker, "date": bar_date, "close": 105.0, "source": "polygon"},
    ])

    # Pinned to alpaca
    obs_alpaca = get_source_pinned_observation(ticker=ticker, target_dt=target_dt, pinned_source="alpaca")
    assert obs_alpaca is not None
    assert obs_alpaca.price == 100.0
    assert obs_alpaca.source == "alpaca"

    # Pinned to polygon
    obs_polygon = get_source_pinned_observation(ticker=ticker, target_dt=target_dt, pinned_source="polygon")
    assert obs_polygon is not None
    assert obs_polygon.price == 105.0
    assert obs_polygon.source == "polygon"

    # Pinned to nonexistent vendor tiingo -> None
    obs_tiingo = get_source_pinned_observation(ticker=ticker, target_dt=target_dt, pinned_source="tiingo")
    assert obs_tiingo is None


def test_real_mongo_replay_and_idempotency(real_mongo):
    """Verifies full round-trip evaluation and repeated replay on real MongoDB."""
    ticker = f"REPLAY_{secrets.token_hex(4).upper()}"
    entry_time = datetime.datetime(2026, 8, 1, 21, 0, tzinfo=datetime.timezone.utc)
    maturity_date = datetime.datetime(2026, 8, 8, 21, 0, tzinfo=datetime.timezone.utc)
    eval_time = datetime.datetime(2026, 8, 15, 12, 0, tzinfo=datetime.timezone.utc)

    # Seed price history for asset and SPY benchmark
    real_mongo["price_history"].insert_many([
        {"ticker": ticker, "date": datetime.datetime(2026, 8, 8, 0, 0, tzinfo=datetime.timezone.utc), "close": 110.0, "source": "alpaca"},
        {"ticker": "SPY", "date": datetime.datetime(2026, 8, 1, 0, 0, tzinfo=datetime.timezone.utc), "close": 400.0, "source": "alpaca"},
        {"ticker": "SPY", "date": datetime.datetime(2026, 8, 8, 0, 0, tzinfo=datetime.timezone.utc), "close": 420.0, "source": "alpaca"},
    ])

    artifact = DecisionArtifact(
        decision_id=f"dec-replay-{secrets.token_hex(6)}",
        cycle_id="cycle-1",
        ticker=ticker,
        requested_action="BUY",
        reference_quote={"price": 100.0, "source": "alpaca"},
        producer="test_agent",
        model="test_model",
        confidence=80,
        created_at=entry_time,
        declared_horizon_days=7,
        benchmark_symbol="SPY",
    )

    # Run 1: First evaluation
    res1 = evaluate_decision_at_horizon(artifact, now=eval_time)
    assert res1 is not None
    assert res1.maturity_status == OutcomeMaturityStatus.MATURE
    # Return: (110 - 100) / 100 = 10%, Benchmark: (420 - 400) / 400 = 5% -> Alpha = 5%
    assert res1.decision_alpha == pytest.approx(5.0, 0.01)

    # Run 2: Delayed replay 30 days later
    replay_time = eval_time + datetime.timedelta(days=30)
    res2 = evaluate_decision_at_horizon(artifact, now=replay_time)
    assert res2 is not None
    assert res2.outcome_id == res1.outcome_id
    assert res2.decision_alpha == res1.decision_alpha
    assert res2.decision_return == res1.decision_return
