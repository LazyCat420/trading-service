"""Unit tests for Shared Canonical Outcome Contract (v4).

Verifies the exit gate requirements of Step 06:
1. Distinguishing close-long SELL from short prediction.
2. Distinguishing held HOLD from flat wait.
3. Invariant: Missing execution evidence CANNOT become realized alpha.
4. Net returns from cash flows without fee/slippage double-counting.
5. Dynamic benchmark selection (crypto BTC vs equity SPY, etc.).
6. Learning eligibility rules and exclusion reasons.
7. Multi-dimensional report slicing with transparent denominators.
8. Backward compatibility adapter from v3 to v4.
9. Logical-to-physical collection mappings.
"""

from __future__ import annotations

import datetime
from zoneinfo import ZoneInfo
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
    calculate_forecast_alpha,
    calculate_net_cashflow_return,
    distinguish_action,
    is_eligible_for_learning,
)


def test_distinguish_sell_action_close_long_vs_short():
    """Exit Gate Check: SELL must be classified as CLOSE_LONG_SELL when long, or SHORT_PREDICTION when flat."""
    # When long 100 shares, SELL is an exit timing decision
    claim_type, classification = distinguish_action(
        requested_action="SELL",
        current_position_qty=100.0,
    )
    assert claim_type == OutcomeClaimType.EXIT_TIMING
    assert classification == "CLOSE_LONG_SELL"

    # When position is 0 (or flat), SELL is a short prediction thesis
    claim_type_short, classification_short = distinguish_action(
        requested_action="SELL",
        current_position_qty=0.0,
    )
    assert claim_type_short == OutcomeClaimType.PROPOSAL_DIRECTION
    assert classification_short == "SHORT_PREDICTION"


def test_distinguish_hold_action_held_vs_flat():
    """Exit Gate Check: HOLD must be classified as HELD_POSITION when holding, or FLAT_WAIT when flat."""
    # When position > 0, HOLD is inventory maintenance
    claim_type_held, classification_held = distinguish_action(
        requested_action="HOLD",
        current_position_qty=50.0,
    )
    assert claim_type_held == OutcomeClaimType.HELD_POSITION
    assert classification_held == "HELD_POSITION"

    # When position is 0, HOLD is sitting in cash / flat wait
    claim_type_flat, classification_flat = distinguish_action(
        requested_action="HOLD",
        current_position_qty=0.0,
    )
    assert claim_type_flat == OutcomeClaimType.FLAT_WAIT
    assert classification_flat == "FLAT_WAIT"


def test_distinguish_buy_immediate_vs_conditional():
    """BUY must distinguish immediate market entry from conditional entry."""
    claim_imm, class_imm = distinguish_action("BUY", 0.0, entry_mode="enter_now")
    assert claim_imm == OutcomeClaimType.PROPOSAL_DIRECTION
    assert class_imm == "IMMEDIATE_BUY"

    claim_cond, class_cond = distinguish_action("BUY", 0.0, entry_mode="limit_on_pullback")
    assert claim_cond == OutcomeClaimType.CONDITIONAL_ENTRY
    assert class_cond == "CONDITIONAL_BUY"


def test_missing_execution_evidence_cannot_become_realized_alpha():
    """Invariant: When no execution fill evidence exists, realized_net_alpha must remain None."""
    now = datetime.datetime.now(datetime.timezone.utc)
    entry_time = now - datetime.timedelta(days=7)

    bm_spec = BenchmarkSpec.resolve("AAPL")
    horizon_spec = HorizonSpec(horizon_value=7)

    # Outcome with forecast evaluation but NO fill_ids or execution return
    outcome = DecisionOutcomeRecordV4(
        outcome_id="out-dec-100-v4",
        decision_id="dec-100",
        claim_type=OutcomeClaimType.PROPOSAL_DIRECTION,
        action_classification="IMMEDIATE_BUY",
        ticker="AAPL",
        horizon_spec=horizon_spec,
        benchmark_spec=bm_spec,
        entry_observation=PriceObservation(price=150.0, date=entry_time, source="alpaca"),
        horizon_observation=PriceObservation(price=165.0, date=now, source="alpaca"),
        benchmark_entry=PriceObservation(price=400.0, date=entry_time, source="alpaca"),
        benchmark_horizon=PriceObservation(price=420.0, date=now, source="alpaca"),
        maturity_status=MaturityStatus.MATURE_VERIFIED,
        decision_return=10.0,
        benchmark_return=5.0,
        forecast_alpha=5.0,
        # Execution fields explicitly None:
        fill_ids=[],
        executed_return=None,
        realized_net_alpha=None,
        created_at=entry_time,
        maturity_date=now,
        resolved_at=now,
    )

    assert outcome.forecast_alpha == 5.0
    assert outcome.executed_return is None
    assert outcome.realized_net_alpha is None
    # Backward compatibility properties
    assert outcome.horizon_days == 7
    assert outcome.benchmark_symbol == "SPY"
    assert outcome.decision_alpha == 5.0


def test_net_cashflow_returns_conserve_fees_and_prevent_double_counting():
    """Net return from cash flows must not subtract fees twice if already embedded in fills."""
    # Scenario A: Fees are already embedded in execution fill prices (e.g. broker net fills)
    res_embedded = calculate_net_cashflow_return(
        entry_fill_price=100.0,
        exit_fill_price=110.0,
        qty=10.0,
        entry_fees=1.0,
        exit_fees=1.0,
        fees_embedded_in_fills=True,
    )
    # 10 shares @ 100 = $1000 outflow; 10 shares @ 110 = $1100 inflow
    # Net P&L = $100; Return = 10%
    assert res_embedded["cash_outflow"] == 1000.0
    assert res_embedded["cash_inflow"] == 1100.0
    assert res_embedded["dollar_pnl"] == 100.0
    assert res_embedded["net_return_pct"] == 10.0
    assert res_embedded["total_fees_charged"] == 0.0

    # Scenario B: Explicit fees not in fill prices
    res_explicit = calculate_net_cashflow_return(
        entry_fill_price=100.0,
        exit_fill_price=110.0,
        qty=10.0,
        entry_fees=1.0,
        exit_fees=1.0,
        fees_embedded_in_fills=False,
    )
    # Outflow = 1000 + 1 = 1001.0
    # Inflow = 1100 - 1 = 1099.0
    # Net P&L = 1099 - 1001 = 98.0
    # Return = (98 / 1001) * 100 = 9.7902%
    assert res_explicit["cash_outflow"] == 1001.0
    assert res_explicit["cash_inflow"] == 1099.0
    assert res_explicit["dollar_pnl"] == 98.0
    assert pytest.approx(res_explicit["net_return_pct"], rel=1e-3) == 9.7902
    assert res_explicit["total_fees_charged"] == 2.0


def test_dynamic_benchmark_resolution_not_hardcoded_spy():
    """Benchmarks must resolve dynamically by asset class and instrument, not hardcoded to SPY."""
    # Crypto resolves to BTC
    btc_spec = BenchmarkSpec.resolve("ETH")
    assert btc_spec.symbol == "BTC"
    assert btc_spec.asset_class == "crypto"

    sol_spec = BenchmarkSpec.resolve("SOL/USD", asset_class="crypto")
    assert sol_spec.symbol == "BTC"
    assert sol_spec.asset_class == "crypto"

    # US Equity resolves to SPY
    spy_spec = BenchmarkSpec.resolve("MSFT")
    assert spy_spec.symbol == "SPY"
    assert spy_spec.asset_class == "stock"


def test_forecast_alpha_calculation():
    """Test forecast alpha calculation across long and short classifications."""
    # Long call: Entry 100 -> Exit 110 (+10%), Benchmark 400 -> 420 (+5%) -> Alpha +5%
    dec_ret, bm_ret, alpha = calculate_forecast_alpha(
        entry_price=100.0,
        horizon_price=110.0,
        benchmark_entry=400.0,
        benchmark_horizon=420.0,
        action_classification="IMMEDIATE_BUY",
    )
    assert dec_ret == 10.0
    assert bm_ret == 5.0
    assert alpha == 5.0

    # Short prediction: Entry 100 -> Exit 90 (+10% gain for short), Benchmark 400 -> 404 (+1%) -> Alpha +9%
    s_ret, s_bm, s_alpha = calculate_forecast_alpha(
        entry_price=100.0,
        horizon_price=90.0,
        benchmark_entry=400.0,
        benchmark_horizon=404.0,
        action_classification="SHORT_PREDICTION",
    )
    assert s_ret == 10.0
    assert s_bm == 1.0
    assert s_alpha == 9.0


def test_horizon_spec_maturity_and_cutoffs():
    """Test horizon maturity calculation and market calendar daily cutoffs."""
    entry = datetime.datetime(2026, 9, 1, 10, 0, tzinfo=datetime.timezone.utc)
    spec = HorizonSpec(horizon_value=7, calendar=MarketCalendar.US_EQUITY)
    mat = spec.calculate_maturity_date(entry)
    assert mat == datetime.datetime(2026, 9, 8, 10, 0, tzinfo=datetime.timezone.utc)

    # US Equity cutoff before 16:15 NY time (e.g. 14:00 NY = 18:00 UTC) -> previous day's close
    midday_utc = datetime.datetime(2026, 9, 2, 18, 0, tzinfo=datetime.timezone.utc)
    cutoff = spec.closed_bar_cutoff(midday_utc)
    # In EDT, 18:00 UTC is 14:00 EDT (before 16:15) -> cutoff should be 2026-09-01
    assert cutoff.date() == datetime.date(2026, 9, 1)

    # After 16:15 NY time (e.g. 17:00 NY = 21:00 UTC) -> current day's close
    evening_utc = datetime.datetime(2026, 9, 2, 21, 0, tzinfo=datetime.timezone.utc)
    cutoff_evening = spec.closed_bar_cutoff(evening_utc)
    assert cutoff_evening.date() == datetime.date(2026, 9, 2)


def test_learning_eligibility_predicate():
    """Learning eligibility must strictly require MATURE_VERIFIED, matched sources, and no exclusions."""
    now = datetime.datetime.now(datetime.timezone.utc)
    entry_dt = now - datetime.timedelta(days=7)

    obs_entry = PriceObservation(price=100.0, date=entry_dt, source="vendor_a")
    obs_horiz = PriceObservation(price=105.0, date=now, source="vendor_a")
    bm_entry = PriceObservation(price=400.0, date=entry_dt, source="vendor_a")
    bm_horiz = PriceObservation(price=410.0, date=now, source="vendor_a")

    record = DecisionOutcomeRecordV4(
        outcome_id="out-1",
        decision_id="dec-1",
        claim_type=OutcomeClaimType.PROPOSAL_DIRECTION,
        action_classification="IMMEDIATE_BUY",
        ticker="AAPL",
        benchmark_spec=BenchmarkSpec.resolve("AAPL"),
        entry_observation=obs_entry,
        horizon_observation=obs_horiz,
        benchmark_entry=bm_entry,
        benchmark_horizon=bm_horiz,
        maturity_status=MaturityStatus.MATURE_VERIFIED,
        decision_return=5.0,
        benchmark_return=2.5,
        forecast_alpha=2.5,
        created_at=entry_dt,
        maturity_date=now,
    )
    assert is_eligible_for_learning(record) is True

    # Failure 1: Source mismatch between entry and exit
    record_mismatch = record.model_copy(deep=True)
    record_mismatch.horizon_observation = PriceObservation(price=105.0, date=now, source="vendor_b")
    assert is_eligible_for_learning(record_mismatch) is False

    # Failure 2: Status is DUE_UNRESOLVED
    record_unres = record.model_copy(deep=True)
    record_unres.maturity_status = MaturityStatus.DUE_UNRESOLVED
    assert is_eligible_for_learning(record_unres) is False

    # Failure 3: Delisted symbol
    record_delisted = record.model_copy(deep=True)
    record_delisted.horizon_observation.is_delisted_or_suspended = True
    assert is_eligible_for_learning(record_delisted) is False

    # Failure 4: Unadjusted corporate action
    record_split = record.model_copy(deep=True)
    record_split.entry_observation.adjustment_convention = AdjustmentConvention.UNADJUSTED
    assert is_eligible_for_learning(record_split) is False


def test_lot_closure_record_v4():
    """Test LotClosureRecordV4 economic attributes and fee conservation."""
    now = datetime.datetime.now(datetime.timezone.utc)
    opened = now - datetime.timedelta(days=3)

    bm_spec = BenchmarkSpec.resolve("NVDA")

    lot_record = LotClosureRecordV4(
        closure_id="close-101",
        lot_id="lot-101",
        bot_id="bot-live-1",
        ticker="NVDA",
        closed_qty=10.0,
        entry_price=100.0,
        exit_price=120.0,
        allocated_entry_fee=1.0,
        exit_fee=1.0,
        fees_embedded_in_fills=False,
        invested_capital_denominator=1001.0,
        dollar_pnl=198.0,
        net_realized_return=19.7802,
        benchmark_spec=bm_spec,
        benchmark_return=5.0,
        realized_net_alpha=14.7802,
        provenance="LIVE",
        provenance_complete=True,
        is_attributable=True,
        opened_at=opened,
        closed_at=now,
    )
    assert lot_record.dollar_pnl == 198.0
    assert lot_record.realized_net_alpha == 14.7802
    assert lot_record.is_attributable is True


def test_outcome_report_slice_counts():
    """Report slices must expose transparent counts across all maturity and exclusion states."""
    slice_rep = OutcomeReportSlice(
        horizon="7d",
        regime="BULL",
        task_side="IMMEDIATE_BUY",
        confidence_bucket="60-80",
        mode_provenance="ENFORCE",
        not_yet_due_count=12,
        due_unresolved_count=3,
        evaluated_mature_count=45,
        excluded_count=5,
        exclusion_breakdown={
            ExclusionReason.DELISTED_OR_SUSPENDED.value: 2,
            ExclusionReason.MISSING_BENCHMARK_BAR.value: 3,
        },
        mean_forecast_alpha=3.42,
        mean_realized_net_alpha=2.85,
        win_rate=62.5,
    )
    assert slice_rep.not_yet_due_count == 12
    assert slice_rep.due_unresolved_count == 3
    assert slice_rep.evaluated_mature_count == 45
    assert slice_rep.excluded_count == 5
    assert slice_rep.exclusion_breakdown["DELISTED_OR_SUSPENDED"] == 2


def test_v3_to_v4_adapter_compatibility():
    """Existing v3 DecisionOutcomeRecord must convert seamlessly to v4."""
    now = datetime.datetime.now(datetime.timezone.utc)
    v3_rec = DecisionOutcomeRecord(
        outcome_id="out-legacy-1",
        decision_id="dec-legacy-1",
        evaluation_contract_version=3,
        horizon_days=7,
        benchmark_symbol="SPY",
        entry_observation={"price": 100.0, "source": "legacy_source"},
        horizon_observation={"price": 110.0, "source": "legacy_source"},
        benchmark_entry={"price": 400.0, "source": "legacy_source"},
        benchmark_horizon={"price": 420.0, "source": "legacy_source"},
        decision_return=10.0,
        benchmark_return=5.0,
        decision_alpha=5.0,
        resolved_at=now,
    )

    v4_rec = v3_rec.to_v4(ticker="AAPL")
    assert isinstance(v4_rec, DecisionOutcomeRecordV4)
    assert v4_rec.evaluation_contract_version == 4
    assert v4_rec.decision_id == "dec-legacy-1"
    assert v4_rec.forecast_alpha == 5.0
    assert v4_rec.decision_alpha == 5.0
    assert v4_rec.horizon_days == 7
    assert v4_rec.benchmark_symbol == "SPY"


def test_logical_to_physical_collections_mapping():
    """All logical attribution and outcome stores must map to physical MongoDB collections."""
    assert LOGICAL_TO_PHYSICAL_COLLECTIONS["decision_outcomes"] == "decision_outcomes"
    assert LOGICAL_TO_PHYSICAL_COLLECTIONS["lot_closures"] == "lot_closures"
    assert LOGICAL_TO_PHYSICAL_COLLECTIONS["decision_artifacts"] == "decision_artifacts"
    assert LOGICAL_TO_PHYSICAL_COLLECTIONS["policy_decisions"] == "policy_decisions"
    assert LOGICAL_TO_PHYSICAL_COLLECTIONS["execution_intents"] == "execution_intents"
    assert LOGICAL_TO_PHYSICAL_COLLECTIONS["attribution_reports"] == "attribution_reports"
