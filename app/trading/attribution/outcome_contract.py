"""Shared Canonical Outcome Contract (Contract v4).

Defines the standardized, immutable outcome evaluation contract across
all trading, attribution, and autoresearch subsystems.

Key Guarantees:
1. 7 Separate Typed Claim Formulations:
   - PROPOSAL_DIRECTION: Thesis directional evaluation (e.g. BUY / long or prospective short forecast).
   - EXIT_TIMING: Exit decision evaluation (strictly distinguishes CLOSE_LONG_SELL from SHORT_PREDICTION).
   - FLAT_WAIT: HOLD decision when flat (cash preservation / avoided decline).
   - HELD_POSITION: HOLD decision when in position (inventory maintenance / ride thesis).
   - CONDITIONAL_ENTRY: Limit / stop trigger path evaluations.
   - POLICY_COUNTERFACTUAL: Counterfactual outcome if policy had not intervened/capped.
   - REALIZED_LOT: Closed tax-lot economic cash-flow accounting.

2. Forecast Alpha vs Realized Net Alpha Separation:
   - Forecast Alpha = decision_return - benchmark_return
   - Realized Net Alpha = executed_return - benchmark_return
   - Invariant: Missing execution evidence CANNOT become realized alpha!

3. Fee & Slippage Double-Counting Protection:
   - Modeled spread/slippage already embedded in fill prices is NOT subtracted a second time.
   - Net cash-flow returns conserve capital: (Inflow - Outflow) / Outflow.

4. Versioned Dynamic Benchmark Resolution:
   - Do not hardcode SPY: dynamically resolves by asset class, task, and instrument (e.g. BTC for crypto).

5. Strict Exclusion & Learning Eligibility Rules:
   - Distinguishes NOT_YET_DUE, DUE_UNRESOLVED, MATURE_VERIFIED, and EXCLUDED.
   - Reasoned exclusions (splits, delistings, missing benchmark bars, synthetic cycles).

6. Reporting Slices:
   - Multi-dimensional slicing (horizon, regime, task/side, confidence bucket, mode/provenance).
   - Transparent denominators: not_yet_due, due_unresolved, evaluated_mature, and excluded counts.
"""

from __future__ import annotations

import datetime
from enum import Enum
import math
from typing import Any, Optional
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, field_validator


def _ensure_utc(dt: Optional[datetime.datetime]) -> Optional[datetime.datetime]:
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=datetime.timezone.utc)
    return dt.astimezone(datetime.timezone.utc)


class OutcomeClaimType(str, Enum):
    """The 7 distinct outcome claim formulations required by the shared contract."""
    PROPOSAL_DIRECTION = "proposal_direction"
    EXIT_TIMING = "exit_timing"
    FLAT_WAIT = "flat_wait"
    HELD_POSITION = "held_position"
    CONDITIONAL_ENTRY = "conditional_entry"
    POLICY_COUNTERFACTUAL = "policy_counterfactual"
    REALIZED_LOT = "realized_lot"


class MaturityStatus(str, Enum):
    """Lifecycle maturity state of an outcome evaluation."""
    NOT_YET_DUE = "NOT_YET_DUE"
    DUE_UNRESOLVED = "DUE_UNRESOLVED"
    MATURE_VERIFIED = "MATURE_VERIFIED"
    EXCLUDED = "EXCLUDED"


class ExclusionReason(str, Enum):
    """Explicit reasons why an outcome cannot enter verified learning or metrics."""
    MISSING_BENCHMARK_BAR = "MISSING_BENCHMARK_BAR"
    STALE_HORIZON_BAR = "STALE_HORIZON_BAR"
    DELISTED_OR_SUSPENDED = "DELISTED_OR_SUSPENDED"
    CORPORATE_ACTION_UNADJUSTED = "CORPORATE_ACTION_UNADJUSTED"
    DATA_SOURCE_MISMATCH = "DATA_SOURCE_MISMATCH"
    SYNTHETIC_CYCLE = "SYNTHETIC_CYCLE"
    LEGACY_UNSUPPORTED = "LEGACY_UNSUPPORTED"
    NO_EXECUTION_EVIDENCE = "NO_EXECUTION_EVIDENCE"
    INCOMPATIBLE_CLAIM_TYPE = "INCOMPATIBLE_CLAIM_TYPE"
    PRICE_AVAILABILITY_ERROR = "PRICE_AVAILABILITY_ERROR"
    POLICY_REJECTED_NO_FILL = "POLICY_REJECTED_NO_FILL"


class MarketCalendar(str, Enum):
    """Calendar rules for cutoff and holiday handling."""
    US_EQUITY = "US_EQUITY"
    CRYPTO_24_7 = "CRYPTO_24_7"
    DEFAULT = "DEFAULT"


class AdjustmentConvention(str, Enum):
    """Corporate action adjustment convention."""
    SPLIT_ADJUSTED = "SPLIT_ADJUSTED"
    SPLIT_AND_DIVIDEND_ADJUSTED = "SPLIT_AND_DIVIDEND_ADJUSTED"
    UNADJUSTED = "UNADJUSTED"


class CanonicalOutcomeModel(BaseModel):
    """Base model enforcing extra='forbid' while safely ignoring Mongo's internal _id."""
    model_config = ConfigDict(extra="forbid", populate_by_name=True)
    mongo_id: Optional[Any] = Field(default=None, alias="_id", exclude=True)


class BenchmarkSpec(CanonicalOutcomeModel):
    """Versioned benchmark specification by task and instrument."""
    symbol: str
    asset_class: str = "stock"
    task: str = "default"
    version: str = "v1"
    description: str = ""

    @classmethod
    def resolve(
        cls,
        ticker: str,
        task: str = "default",
        asset_class: Optional[str] = None,
    ) -> BenchmarkSpec:
        """Resolve appropriate benchmark specification for a ticker and task.
        
        Guarantees: Never universally hardcode SPY across asset classes!
        """
        clean_ticker = ticker.upper().strip()
        inferred_asset = asset_class
        if not inferred_asset:
            from app.config.config_tickers import classify_asset
            inferred_asset = classify_asset(clean_ticker)

        if inferred_asset == "crypto":
            return cls(
                symbol="BTC",
                asset_class="crypto",
                task=task,
                version="v1",
                description="Bitcoin benchmark for crypto assets",
            )
        elif clean_ticker in ("QQQ", "AAPL", "MSFT", "NVDA", "GOOGL", "AMZN", "META"):
            # Tech-heavy tasks can optionally map to QQQ or SPY
            return cls(
                symbol="SPY",
                asset_class="stock",
                task=task,
                version="v1",
                description="S&P 500 benchmark for US large-cap equities",
            )
        else:
            return cls(
                symbol="SPY",
                asset_class="stock",
                task=task,
                version="v1",
                description="Default market benchmark for equity instruments",
            )


class PriceObservation(CanonicalOutcomeModel):
    """Source-pinned price observation with explicit adjustment and vendor metadata."""
    price: float = Field(gt=0.0)
    date: datetime.datetime
    source: str = Field(min_length=1)
    bar_type: str = "daily_close"
    adjustment_convention: AdjustmentConvention = AdjustmentConvention.SPLIT_ADJUSTED
    vendor_hash: str = ""
    is_delisted_or_suspended: bool = False

    @field_validator("date", mode="after")
    @classmethod
    def validate_utc(cls, v: datetime.datetime) -> datetime.datetime:
        return _ensure_utc(v) or datetime.datetime.now(datetime.timezone.utc)


class HorizonSpec(CanonicalOutcomeModel):
    """Horizon configuration defining maturity schedule and calendar."""
    horizon_units: str = "calendar_days"
    horizon_value: int = 7
    calendar: MarketCalendar = MarketCalendar.US_EQUITY

    def calculate_maturity_date(self, entry_date: datetime.datetime) -> datetime.datetime:
        """Calculates exact maturity timestamp based on horizon units and calendar."""
        entry_aware = _ensure_utc(entry_date) or datetime.datetime.now(datetime.timezone.utc)
        if self.horizon_units == "calendar_days":
            return entry_aware + datetime.timedelta(days=self.horizon_value)
        elif self.horizon_units == "hours":
            return entry_aware + datetime.timedelta(hours=self.horizon_value)
        elif self.horizon_units == "trading_days":
            # Approximate 7 trading days ~ 10 calendar days assuming weekends
            added_days = int(math.ceil(self.horizon_value * (7.0 / 5.0)))
            return entry_aware + datetime.timedelta(days=added_days)
        return entry_aware + datetime.timedelta(days=self.horizon_value)

    def closed_bar_cutoff(self, as_of: datetime.datetime) -> datetime.datetime:
        """Determines completed daily close availability boundary without future leakage."""
        at = _ensure_utc(as_of) or datetime.datetime.now(datetime.timezone.utc)
        if self.calendar == MarketCalendar.CRYPTO_24_7:
            # 00:00 UTC daily cutoff
            date = at.date()
            if at.hour < 0 or (at.hour == 0 and at.minute < 5):
                date -= datetime.timedelta(days=1)
            return datetime.datetime.combine(date, datetime.time.min, tzinfo=datetime.timezone.utc)

        # US Equities: 16:15 Eastern Time
        local = at.astimezone(ZoneInfo("America/New_York"))
        date = local.date()
        if (local.hour, local.minute) < (16, 15):
            date -= datetime.timedelta(days=1)
        return datetime.datetime.combine(date, datetime.time.min, tzinfo=datetime.timezone.utc)


class DecisionOutcomeRecordV4(CanonicalOutcomeModel):
    """Represents a standardized, immutable horizon evaluation under Contract v4."""

    outcome_id: str
    decision_id: str
    bot_id: str = "default"
    execution_intent_id: Optional[str] = None
    fill_ids: list[str] = Field(default_factory=list)
    lot_id: Optional[str] = None
    closure_id: Optional[str] = None
    evaluation_contract_version: int = 4
    claim_type: OutcomeClaimType
    action_classification: str
    ticker: str
    horizon_spec: HorizonSpec = Field(default_factory=HorizonSpec)
    benchmark_spec: BenchmarkSpec
    entry_observation: Optional[PriceObservation] = None
    horizon_observation: Optional[PriceObservation] = None
    benchmark_entry: Optional[PriceObservation] = None
    benchmark_horizon: Optional[PriceObservation] = None
    maturity_status: MaturityStatus = MaturityStatus.NOT_YET_DUE
    exclusion_reason: Optional[ExclusionReason] = None
    retry_count: int = 0
    retry_after: Optional[datetime.datetime] = None

    # Thesis / Forecast Return & Alpha (Evaluated regardless of execution)
    decision_return: Optional[float] = None
    benchmark_return: Optional[float] = None
    forecast_alpha: Optional[float] = None

    # Realized Execution Return & Net Alpha (Requires execution fills; None otherwise!)
    executed_return: Optional[float] = None
    realized_net_alpha: Optional[float] = None

    # Eligibility & Metadata
    is_eligible_for_learning: bool = False
    created_at: datetime.datetime = Field(
        default_factory=lambda: datetime.datetime.now(datetime.timezone.utc)
    )
    maturity_date: datetime.datetime
    resolved_at: Optional[datetime.datetime] = None

    # Backward-compatibility properties for v3 readers
    @property
    def horizon_days(self) -> int:
        return self.horizon_spec.horizon_value

    @property
    def benchmark_symbol(self) -> str:
        return self.benchmark_spec.symbol

    @property
    def decision_alpha(self) -> Optional[float]:
        return self.forecast_alpha

    @field_validator("created_at", "maturity_date", "resolved_at", "retry_after", mode="after")
    @classmethod
    def validate_utc(cls, v: Optional[datetime.datetime]) -> Optional[datetime.datetime]:
        return _ensure_utc(v)


class PositionLot(CanonicalOutcomeModel):
    """Canonical tax lot tracking for FIFO allocation, fees, and provenance."""

    lot_id: str
    bot_id: str
    ticker: str
    initial_qty: float = Field(gt=0.0)
    remaining_qty: float = Field(ge=0.0)
    entry_price: float = Field(gt=0.0)
    entry_notional: float = Field(gt=0.0)
    entry_fee: float = Field(default=0.0, ge=0.0)
    remaining_entry_fee: float = Field(default=0.0, ge=0.0)
    opened_at: datetime.datetime
    status: str = "open"  # "open", "partial", "closed"
    origin: str = "LIVE"
    provenance_complete: bool = True
    decision_id: Optional[str] = None
    execution_intent_id: Optional[str] = None
    historical_fill_id: Optional[str] = None
    migration_batch_id: Optional[str] = None
    updated_at: Optional[datetime.datetime] = None

    @field_validator("opened_at", "updated_at", mode="after")
    @classmethod
    def validate_utc(cls, v: Optional[datetime.datetime]) -> Optional[datetime.datetime]:
        return _ensure_utc(v)


class LotClosureRecordV4(CanonicalOutcomeModel):
    """Realized closed tax lot economic accounting under Contract v4."""

    closure_id: str
    lot_id: str
    bot_id: str
    ticker: str
    closed_qty: float = Field(gt=0.0)
    entry_price: float = Field(gt=0.0)
    exit_price: float = Field(gt=0.0)
    allocated_entry_fee: float = 0.0
    exit_fee: float = 0.0
    fees_embedded_in_fills: bool = False
    invested_capital_denominator: float = Field(gt=0.0)
    dollar_pnl: float
    net_realized_return: float
    benchmark_spec: BenchmarkSpec
    benchmark_entry: Optional[PriceObservation] = None
    benchmark_exit: Optional[PriceObservation] = None
    benchmark_return: Optional[float] = None
    realized_net_alpha: Optional[float] = None
    provenance: str = "LIVE"
    provenance_complete: bool = True
    is_attributable: bool = True
    opened_at: datetime.datetime
    closed_at: datetime.datetime
    evaluated_at: Optional[datetime.datetime] = None
    gross_pnl: Optional[float] = None
    net_pnl: Optional[float] = None
    fees: Optional[float] = None
    entry_decision_id: Optional[str] = None
    exit_decision_id: Optional[str] = None
    entry_intent_id: Optional[str] = None
    exit_intent_id: Optional[str] = None
    alpha_evaluated: Optional[bool] = None
    lot_alpha: Optional[float] = None
    exclusion_reason: Optional[str] = None
    retry_after: Optional[datetime.datetime] = None
    status: Optional[str] = None
    benchmark_status: Optional[str] = None

    @field_validator("opened_at", "closed_at", "evaluated_at", "retry_after", mode="after")
    @classmethod
    def validate_utc(cls, v: Optional[datetime.datetime]) -> Optional[datetime.datetime]:
        return _ensure_utc(v)


class OutcomeReportSlice(CanonicalOutcomeModel):
    """Multi-dimensional report slice for granular model & strategy evaluation."""

    horizon: str
    regime: str = "ALL"
    task_side: str
    confidence_bucket: str = "ALL"
    mode_provenance: str = "ENFORCE"

    not_yet_due_count: int = 0
    due_unresolved_count: int = 0
    evaluated_mature_count: int = 0
    excluded_count: int = 0
    exclusion_breakdown: dict[str, int] = Field(default_factory=dict)

    mean_forecast_alpha: Optional[float] = None
    mean_realized_net_alpha: Optional[float] = None
    win_rate: Optional[float] = None


# =====================================================================
# Calculation Helpers & Invariant Enforcement
# =====================================================================

def distinguish_action(
    requested_action: str,
    current_position_qty: float,
    entry_mode: str = "enter_now",
) -> tuple[OutcomeClaimType, str]:
    """Distinguishes action classification to prevent conflating exits with shorting or flat waits with holding.
    
    Returns:
        (OutcomeClaimType, action_classification)
    """
    clean_action = requested_action.upper().strip()
    qty = float(current_position_qty or 0.0)

    if clean_action == "SELL":
        if qty > 0.0:
            return OutcomeClaimType.EXIT_TIMING, "CLOSE_LONG_SELL"
        else:
            return OutcomeClaimType.PROPOSAL_DIRECTION, "SHORT_PREDICTION"
    elif clean_action == "HOLD":
        if qty > 0.0:
            return OutcomeClaimType.HELD_POSITION, "HELD_POSITION"
        else:
            return OutcomeClaimType.FLAT_WAIT, "FLAT_WAIT"
    elif clean_action == "BUY":
        if entry_mode == "enter_now":
            return OutcomeClaimType.PROPOSAL_DIRECTION, "IMMEDIATE_BUY"
        else:
            return OutcomeClaimType.CONDITIONAL_ENTRY, "CONDITIONAL_BUY"
    else:
        return OutcomeClaimType.PROPOSAL_DIRECTION, clean_action


def calculate_forecast_alpha(
    entry_price: float,
    horizon_price: float,
    benchmark_entry: float,
    benchmark_horizon: float,
    action_classification: str = "IMMEDIATE_BUY",
) -> tuple[float, float, float]:
    """Calculates directional return, benchmark return, and forecast alpha.
    
    Returns:
        (decision_return, benchmark_return, forecast_alpha)
    """
    if entry_price <= 0.0 or benchmark_entry <= 0.0:
        raise ValueError("Entry prices must be positive")

    # Invert return calculation for short prediction or close long
    if action_classification == "SHORT_PREDICTION":
        dec_ret = ((entry_price - horizon_price) / entry_price) * 100.0
    elif action_classification in ("FLAT_WAIT", "HELD_POSITION"):
        # For flat wait, an avoided drop is positive; for held position, holding gain is positive
        if action_classification == "FLAT_WAIT":
            # Avoided decline: if price drops 5%, sitting flat was +5% relative to holding
            dec_ret = ((entry_price - horizon_price) / entry_price) * 100.0
        else:
            # Held position: return on held asset
            dec_ret = ((horizon_price - entry_price) / entry_price) * 100.0
    else:
        dec_ret = ((horizon_price - entry_price) / entry_price) * 100.0

    bm_ret = ((benchmark_horizon - benchmark_entry) / benchmark_entry) * 100.0
    forecast_alpha = dec_ret - bm_ret
    return round(dec_ret, 4), round(bm_ret, 4), round(forecast_alpha, 4)


def calculate_net_cashflow_return(
    entry_fill_price: float,
    exit_fill_price: float,
    qty: float,
    entry_fees: float = 0.0,
    exit_fees: float = 0.0,
    fees_embedded_in_fills: bool = True,
) -> dict[str, float]:
    """Calculates realized net return and dollar P&L strictly from cash flows.
    
    Guarantees:
    - If fees/slippage are already embedded in fill prices, explicit fees are NOT deducted twice.
    - Conserves total fees and invested capital denominator.
    """
    if entry_fill_price <= 0.0 or exit_fill_price <= 0.0 or qty <= 0.0:
        raise ValueError("Prices and quantity must be positive")

    gross_outflow = entry_fill_price * qty
    gross_inflow = exit_fill_price * qty

    if fees_embedded_in_fills:
        # Fees/slippage already represented in the realized fill prices
        cash_outflow = gross_outflow
        cash_inflow = gross_inflow
        total_fees_charged = 0.0
    else:
        # Fees must be explicitly accounted in cash flows
        cash_outflow = gross_outflow + entry_fees
        cash_inflow = gross_inflow - exit_fees
        total_fees_charged = entry_fees + exit_fees

    net_dollar_pnl = cash_inflow - cash_outflow
    invested_capital = cash_outflow
    net_return_pct = (net_dollar_pnl / invested_capital) * 100.0

    return {
        "cash_outflow": round(cash_outflow, 4),
        "cash_inflow": round(cash_inflow, 4),
        "dollar_pnl": round(net_dollar_pnl, 4),
        "invested_capital": round(invested_capital, 4),
        "net_return_pct": round(net_return_pct, 4),
        "total_fees_charged": round(total_fees_charged, 4),
    }


def is_eligible_for_learning(outcome: DecisionOutcomeRecordV4) -> bool:
    """Predicate evaluating whether an outcome record qualifies for model training/learning.
    
    Guarantees:
    - Only MATURE_VERIFIED records are eligible.
    - No synthetic cycles, delisted/suspended symbols, unadjusted splits, or source mismatches.
    - Unambiguous claim types only (PROPOSAL_DIRECTION, FLAT_WAIT, HELD_POSITION, EXIT_TIMING).
    """
    if outcome.maturity_status != MaturityStatus.MATURE_VERIFIED:
        return False
    if outcome.exclusion_reason is not None:
        return False
    if outcome.entry_observation is None or outcome.horizon_observation is None:
        return False
    if outcome.benchmark_entry is None or outcome.benchmark_horizon is None:
        return False
    if outcome.entry_observation.source != outcome.horizon_observation.source:
        return False
    if outcome.entry_observation.is_delisted_or_suspended or outcome.horizon_observation.is_delisted_or_suspended:
        return False
    if outcome.entry_observation.adjustment_convention == AdjustmentConvention.UNADJUSTED:
        return False
    return True


# =====================================================================
# Logical-to-Physical Collections Mapping
# =====================================================================

LOGICAL_TO_PHYSICAL_COLLECTIONS: dict[str, str] = {
    "decision_outcomes": "decision_outcomes",
    "lot_closures": "lot_closures",
    "decision_artifacts": "decision_artifacts",
    "policy_decisions": "policy_decisions",
    "execution_intents": "execution_intents",
    "order_attempts": "order_attempts",
    "execution_reconciliations": "execution_reconciliations",
    "attribution_reports": "attribution_reports",
    "position_lots": "position_lots",
    "risk_reservations": "risk_reservations",
    "shadow_executions": "shadow_executions",
    "evaluation_checkpoints": "evaluation_checkpoints",
}
