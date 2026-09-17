"""Canonical Attribution and Control-Plane Domain Models.

Implements the domain contract for an auditable, replayable, and immutable chain:
DecisionArtifact -> PolicyDecision -> ExecutionIntent -> OrderAttempt
-> ExecutionReconciliation -> DecisionOutcome -> AttributionReport.
"""

from __future__ import annotations

import datetime
from enum import Enum
from typing import Any, Optional
from pydantic import BaseModel, ConfigDict, Field, field_validator


class PolicyDisposition(str, Enum):
    APPROVE = "APPROVE"
    APPROVE_WITH_CAP = "APPROVE_WITH_CAP"
    CONVERT_TO_WATCH = "CONVERT_TO_WATCH"
    QUEUE_RESEARCH = "QUEUE_RESEARCH"
    BLOCK = "BLOCK"
    REJECT = "REJECT"


class IntentStatus(str, Enum):
    CREATED = "CREATED"
    CONSUMED = "CONSUMED"
    EXPIRED = "EXPIRED"
    SUPERSEDED = "SUPERSEDED"
    REVOKED = "REVOKED"


class ReservationStatus(str, Enum):
    ACTIVE = "ACTIVE"
    RELEASED = "RELEASED"
    CONSUMED = "CONSUMED"
    EXPIRED = "EXPIRED"


class OrderAttemptStatus(str, Enum):
    SUBMITTED = "SUBMITTED"
    ACCEPTED = "ACCEPTED"
    REJECTED = "REJECTED"
    ERROR = "ERROR"


class ReconciliationVerdict(str, Enum):
    EXECUTION_MATCHED = "EXECUTION_MATCHED"
    EXECUTION_PARTIAL = "EXECUTION_PARTIAL"
    EXECUTION_REJECTED = "EXECUTION_REJECTED"
    EXECUTION_LATE = "EXECUTION_LATE"
    EXECUTION_SLIPPAGE_BREACH = "EXECUTION_SLIPPAGE_BREACH"
    EXECUTION_COST_BREACH = "EXECUTION_COST_BREACH"
    EXECUTION_DUPLICATE = "EXECUTION_DUPLICATE"
    EXECUTION_LEDGER_MISMATCH = "EXECUTION_LEDGER_MISMATCH"
    EXECUTION_DATA_INVALID = "EXECUTION_DATA_INVALID"
    EXECUTION_ORPHANED = "EXECUTION_ORPHANED"


class OutcomeMaturityStatus(str, Enum):
    PENDING = "PENDING"
    MATURE = "MATURE"
    UNRESOLVED = "UNRESOLVED"
    CONTAMINATED = "CONTAMINATED"
    CANCELED = "CANCELED"


class AttributionClass(str, Enum):
    DECISION_FAILURE = "DECISION_FAILURE"
    POLICY_INTERVENTION = "POLICY_INTERVENTION"
    POLICY_FAILURE = "POLICY_FAILURE"
    EXECUTION_FAILURE = "EXECUTION_FAILURE"
    MARKET_DATA_FAILURE = "MARKET_DATA_FAILURE"
    OUTCOME_LABEL_FAILURE = "OUTCOME_LABEL_FAILURE"
    LEDGER_FAILURE = "LEDGER_FAILURE"
    INFRASTRUCTURE_FAILURE = "INFRASTRUCTURE_FAILURE"
    NO_FAILURE = "NO_FAILURE"
    UNRESOLVED = "UNRESOLVED"


def _ensure_utc(dt: Optional[datetime.datetime]) -> Optional[datetime.datetime]:
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=datetime.timezone.utc)
    return dt.astimezone(datetime.timezone.utc)


class CanonicalAttributionModel(BaseModel):
    """Base model enforcing extra='forbid' while safely ignoring Mongo's internal _id."""
    model_config = ConfigDict(extra="forbid", populate_by_name=True)
    mongo_id: Optional[Any] = Field(default=None, alias="_id", exclude=True)


class DecisionArtifact(CanonicalAttributionModel):
    """Represents the LLM proposal exactly as produced, before any policy modification."""

    decision_id: str
    bot_id: str = "default"
    schema_version: int = 1
    cycle_id: str
    ticker: str
    asset_type: str = "stock"
    currency: str = "USD"
    producer: str
    producer_release: str = "unknown"
    model: str
    prompt_template_hash: str = ""
    requested_action: str
    requested_size_pct: Optional[float] = None
    requested_timing: dict[str, Any] = Field(default_factory=dict)
    confidence: int = Field(ge=0, le=100)
    declared_horizon_days: int = 7
    benchmark_symbol: str = "SPY"
    thesis_summary: str = ""
    invalidation_condition: Optional[str] = None
    evidence_snapshot_id: str = ""
    reference_quote: dict[str, Any] = Field(default_factory=dict)
    contract_validation: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime.datetime = Field(
        default_factory=lambda: datetime.datetime.now(datetime.timezone.utc)
    )

    @field_validator("created_at", mode="after")
    @classmethod
    def validate_utc(cls, v: datetime.datetime) -> datetime.datetime:
        return _ensure_utc(v) or datetime.datetime.now(datetime.timezone.utc)


class PolicyDecision(CanonicalAttributionModel):
    """Represents deterministic policy evaluation of a DecisionArtifact."""

    policy_decision_id: str
    decision_id: str
    policy_version: str = "v1.0"
    config_hash: str
    evaluated_at: datetime.datetime = Field(
        default_factory=lambda: datetime.datetime.now(datetime.timezone.utc)
    )
    requested_values: dict[str, Any]
    normalized_values: dict[str, Any]
    approved_values: dict[str, Any]
    disposition: PolicyDisposition
    reason_codes: list[str] = Field(default_factory=list)
    gate_results: dict[str, Any] = Field(default_factory=dict)
    portfolio_snapshot_ref: str = ""
    effective_mode: str = "OBSERVE"
    evaluation_timestamp: Optional[datetime.datetime] = None
    normalized_ticker: str = ""
    normalized_action: str = ""
    approved_size_pct: float = 0.0

    @property
    def is_approved(self) -> bool:
        return self.disposition in (
            PolicyDisposition.APPROVE,
            PolicyDisposition.APPROVE_WITH_CAP,
        )

    @field_validator("evaluated_at", mode="after")
    @classmethod
    def validate_utc(cls, v: datetime.datetime) -> datetime.datetime:
        return _ensure_utc(v) or datetime.datetime.now(datetime.timezone.utc)


class ExecutionIntent(CanonicalAttributionModel):
    """Represents the only legal instruction for an entry execution attempt."""

    execution_intent_id: str
    decision_id: str
    policy_decision_id: str
    ticker: str
    side: str
    approved_quantity: Optional[float] = None
    approved_notional: Optional[float] = None
    approved_size_pct: float
    sizing_basis: str = "PORTFOLIO_EQUITY"
    order_constraints: dict[str, Any] = Field(default_factory=dict)
    reference_quote: dict[str, Any] = Field(default_factory=dict)
    allowed_slippage_bps: float = 25.0
    valid_from: datetime.datetime
    expires_at: datetime.datetime
    idempotency_key: str
    intent_type: str = "ENTRY"
    schema_version: int = 1
    status: IntentStatus = IntentStatus.CREATED
    consumed_at: Optional[datetime.datetime] = None
    effective_mode: str = "OBSERVE"
    slot_key: Optional[str] = None
    bot_id: Optional[str] = None

    @field_validator("valid_from", "expires_at", "consumed_at", mode="after")
    @classmethod
    def validate_utc(cls, v: Optional[datetime.datetime]) -> Optional[datetime.datetime]:
        return _ensure_utc(v)


class OrderAttempt(CanonicalAttributionModel):
    """Represents each submission attempt to broker/paper trader."""

    order_attempt_id: str
    execution_intent_id: str
    attempt_number: int = 1
    request_hash: str = ""
    submitted_at: datetime.datetime = Field(
        default_factory=lambda: datetime.datetime.now(datetime.timezone.utc)
    )
    status: OrderAttemptStatus = OrderAttemptStatus.SUBMITTED
    reason_code: str = ""
    order_id: Optional[str] = None
    effective_mode: str = "OBSERVE"

    @field_validator("submitted_at", mode="after")
    @classmethod
    def validate_utc(cls, v: datetime.datetime) -> datetime.datetime:
        return _ensure_utc(v) or datetime.datetime.now(datetime.timezone.utc)


class ExecutionReconciliation(CanonicalAttributionModel):
    """Represents intended versus actual broker execution."""

    reconciliation_id: str
    execution_intent_id: str
    order_id: str
    fill_ids: list[str] = Field(default_factory=list)
    intended_qty: float
    filled_qty: float
    reference_price: float
    expected_price: float
    realized_price: float
    fees: float = 0.0
    modeled_spread_bps: float = 0.0
    realized_slippage_bps: float = 0.0
    submission_to_fill_latency_ms: float = 0.0
    residual_qty: float = 0.0
    verdict: ReconciliationVerdict = ReconciliationVerdict.EXECUTION_MATCHED
    evidence_version: str = "v1"
    discrepancy_reasons: list[str] = Field(default_factory=list)
    order_attempt_id: str = ""
    matched_quantity: float = 0.0
    reconciled_at: datetime.datetime = Field(
        default_factory=lambda: datetime.datetime.now(datetime.timezone.utc)
    )
    effective_mode: str = "OBSERVE"

    @property
    def slippage_bps(self) -> float:
        return self.realized_slippage_bps

    @property
    def fees_paid(self) -> float:
        return self.fees

    @property
    def actual_quantity(self) -> float:
        return self.filled_qty

    @property
    def intended_quantity(self) -> float:
        return self.intended_qty

    @property
    def residual_quantity(self) -> float:
        return self.residual_qty

    @field_validator("reconciled_at", mode="after")
    @classmethod
    def validate_utc(cls, v: datetime.datetime) -> datetime.datetime:
        return _ensure_utc(v) or datetime.datetime.now(datetime.timezone.utc)


class DecisionOutcomeRecord(CanonicalAttributionModel):
    """Represents horizon-based evaluation of the underlying thesis claim (Contract v3)."""
    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    outcome_id: str
    decision_id: str
    evaluation_contract_version: int = 3
    horizon_days: int = 7
    benchmark_symbol: str = "SPY"
    entry_observation: dict[str, Any]
    horizon_observation: Optional[dict[str, Any]] = None
    benchmark_entry: Optional[dict[str, Any]] = None
    benchmark_horizon: Optional[dict[str, Any]] = None
    maturity_status: OutcomeMaturityStatus = OutcomeMaturityStatus.PENDING
    decision_return: Optional[float] = None
    benchmark_return: Optional[float] = None
    decision_alpha: Optional[float] = None
    resolved_at: Optional[datetime.datetime] = None

    @field_validator("resolved_at", mode="after")
    @classmethod
    def validate_utc(cls, v: Optional[datetime.datetime]) -> Optional[datetime.datetime]:
        return _ensure_utc(v)

    def to_v4(self, ticker: str = "UNKNOWN", claim_type_val: Optional[str] = None) -> Any:
        """Adapts legacy v3 record to canonical v4 contract."""
        from app.trading.attribution.outcome_contract import (
            BenchmarkSpec,
            DecisionOutcomeRecordV4,
            HorizonSpec,
            MaturityStatus,
            OutcomeClaimType,
            PriceObservation,
        )

        now = datetime.datetime.now(datetime.timezone.utc)
        resolved_dt = self.resolved_at or now

        # Map maturity status
        status_map = {
            OutcomeMaturityStatus.PENDING: MaturityStatus.NOT_YET_DUE,
            OutcomeMaturityStatus.MATURE: MaturityStatus.MATURE_VERIFIED,
            OutcomeMaturityStatus.UNRESOLVED: MaturityStatus.DUE_UNRESOLVED,
            OutcomeMaturityStatus.CONTAMINATED: MaturityStatus.EXCLUDED,
            OutcomeMaturityStatus.CANCELED: MaturityStatus.EXCLUDED,
        }
        v4_status = status_map.get(self.maturity_status, MaturityStatus.DUE_UNRESOLVED)

        # Build observations if present
        p_entry_obs = None
        if self.entry_observation and "price" in self.entry_observation:
            p_entry_obs = PriceObservation(
                price=float(self.entry_observation["price"]),
                date=self.resolved_at or now,
                source=self.entry_observation.get("source", "legacy"),
            )

        p_horiz_obs = None
        if self.horizon_observation and "price" in self.horizon_observation:
            p_horiz_obs = PriceObservation(
                price=float(self.horizon_observation["price"]),
                date=self.resolved_at or now,
                source=self.horizon_observation.get("source", "legacy"),
            )

        bm_entry_obs = None
        if self.benchmark_entry and "price" in self.benchmark_entry:
            bm_entry_obs = PriceObservation(
                price=float(self.benchmark_entry["price"]),
                date=self.resolved_at or now,
                source=self.benchmark_entry.get("source", "legacy"),
            )

        bm_horiz_obs = None
        if self.benchmark_horizon and "price" in self.benchmark_horizon:
            bm_horiz_obs = PriceObservation(
                price=float(self.benchmark_horizon["price"]),
                date=self.resolved_at or now,
                source=self.benchmark_horizon.get("source", "legacy"),
            )

        bm_spec = BenchmarkSpec.resolve(ticker=ticker)
        horizon_spec = HorizonSpec(horizon_value=self.horizon_days)

        c_type = OutcomeClaimType.PROPOSAL_DIRECTION
        if claim_type_val == "flat_wait":
            c_type = OutcomeClaimType.FLAT_WAIT

        return DecisionOutcomeRecordV4(
            outcome_id=f"out-{self.decision_id}-v4",
            decision_id=self.decision_id,
            evaluation_contract_version=4,
            claim_type=c_type,
            action_classification="LEGACY_V3_ADAPTED",
            ticker=ticker,
            horizon_spec=horizon_spec,
            benchmark_spec=bm_spec,
            entry_observation=p_entry_obs,
            horizon_observation=p_horiz_obs,
            benchmark_entry=bm_entry_obs,
            benchmark_horizon=bm_horiz_obs,
            maturity_status=v4_status,
            decision_return=self.decision_return,
            benchmark_return=self.benchmark_return,
            forecast_alpha=self.decision_alpha,
            created_at=resolved_dt - datetime.timedelta(days=self.horizon_days),
            maturity_date=resolved_dt,
            resolved_at=self.resolved_at,
        )


class AttributionReport(CanonicalAttributionModel):
    """Represents a derived causal diagnosis of trade / decision performance."""

    attribution_id: str
    lineage: dict[str, Any] = Field(default_factory=dict)
    classification: AttributionClass
    primary_reason_code: str
    contributing_factors: list[dict[str, Any]] = Field(default_factory=list)
    supporting_record_refs: list[str] = Field(default_factory=list)
    confidence: float = 1.0
    owner_subsystem: str
    remediation_route: str = ""
    attribution_algorithm_version: str = "v1.0"
    created_at: datetime.datetime = Field(
        default_factory=lambda: datetime.datetime.now(datetime.timezone.utc)
    )

    @field_validator("created_at", mode="after")
    @classmethod
    def validate_utc(cls, v: datetime.datetime) -> datetime.datetime:
        return _ensure_utc(v) or datetime.datetime.now(datetime.timezone.utc)


class RiskReservation(CanonicalAttributionModel):
    """Represents a cash risk reservation locking purchasing power for an admitted intent."""

    reservation_id: str
    bot_id: str
    execution_intent_id: str
    slot_key: str
    ticker: str
    side: str
    reserved_notional: float
    status: ReservationStatus = ReservationStatus.ACTIVE
    created_at: datetime.datetime = Field(
        default_factory=lambda: datetime.datetime.now(datetime.timezone.utc)
    )
    expires_at: datetime.datetime
    released_at: Optional[datetime.datetime] = None

    @field_validator("created_at", "expires_at", "released_at", mode="after")
    @classmethod
    def validate_utc(cls, v: Optional[datetime.datetime]) -> Optional[datetime.datetime]:
        return _ensure_utc(v)


# Re-export v4 outcome models and contract specifications
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

