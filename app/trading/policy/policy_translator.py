"""Deterministic Policy Translator.

Translates a DecisionArtifact and a pinned PolicyInputSnapshot into a PolicyDecision
and, when approved, an ExecutionIntent.

Guiding Invariant: Deterministic policy is the sole authority for entry approval
and sizing. The LLM's proposal is never executed directly.
"""

from __future__ import annotations

import datetime
import hashlib
import json
import uuid
from typing import Any, Optional
from pydantic import BaseModel, Field

from app.trading.attribution.models import (
    DecisionArtifact,
    ExecutionIntent,
    IntentStatus,
    PolicyDecision,
    PolicyDisposition,
)

POLICY_VERSION = "v1.0"
DEFAULT_INTENT_TTL_SECONDS = 900  # 15 minutes
MAX_QUOTE_AGE_HOURS = 24.0
MIN_CONFIDENCE_THRESHOLD = 50


class PolicyInputSnapshot(BaseModel):
    """Pinned environmental snapshot representing portfolio and risk state at decision time."""

    portfolio_equity: float
    cash_balance: float
    bot_id: str = "default"
    held_ticker_value: float = 0.0
    max_concentration_pct: float = 0.25
    max_position_size_pct: float = 0.10
    drawdown_breaker_active: bool = False
    strategy_health_status: str = "OK"  # OK, REDUCE, CUT
    internal_consensus_score: Optional[int] = None
    data_quality: Optional[float] = None
    quote_price: float
    quote_age_hours: float = 0.0
    quote_source: str = "stored_bar"
    is_held: bool = False
    snapshot_timestamp: datetime.datetime = Field(
        default_factory=lambda: datetime.datetime.now(datetime.timezone.utc)
    )
    snapshot_id: str = ""
    cycle_id: str = ""
    held_positions: dict[str, float] = Field(default_factory=dict)
    quote_timestamp: Optional[datetime.datetime] = None
    as_of: Optional[datetime.datetime] = None
    circuit_breaker_active: bool = False
    is_degraded: bool = False
    degraded_reasons: list[str] = Field(default_factory=list)
    position_marks: dict[str, dict[str, Any]] = Field(default_factory=dict)

    def compute_hash(self) -> str:
        payload = {
            "equity": round(self.portfolio_equity, 2),
            "cash": round(self.cash_balance, 2),
            "held_val": round(self.held_ticker_value, 2),
            "conc_pct": self.max_concentration_pct,
            "pos_pct": self.max_position_size_pct,
            "breaker": self.drawdown_breaker_active,
            "health": self.strategy_health_status,
            "quote": round(self.quote_price, 4),
            "is_degraded": self.is_degraded,
        }
        encoded = json.dumps(payload, sort_keys=True)
        return hashlib.sha256(encoded.encode()).hexdigest()[:16]


def resolve_policy_size_pct(
    base_fraction: float,
    confidence: int,
    max_position_pct: float,
    consensus_score: Optional[int] = None,
    data_quality: Optional[float] = None,
) -> float:
    """Deterministic formula for sizing equity allocation based on confidence and consensus."""
    conf_scale = max(0.0, min(1.0, confidence / 100.0))
    size = min(base_fraction, max_position_pct) * conf_scale
    if consensus_score is not None and consensus_score < 70:
        size *= 0.8  # Haircut for internal dissension
    if data_quality is not None and data_quality < 0.8:
        size *= 0.8  # Haircut for data quality
    return max(0.0, round(size, 4))


class PolicyTranslator:
    """Translates proposals into authoritative policy decisions and execution intents."""

    @staticmethod
    def evaluate(
        artifact: DecisionArtifact,
        snapshot: PolicyInputSnapshot,
        policy_decision_id: Optional[str] = None,
        execution_intent_id: Optional[str] = None,
        now: Optional[datetime.datetime] = None,
        bot_id: Optional[str] = None,
    ) -> tuple[PolicyDecision, Optional[ExecutionIntent]]:
        eval_time = now or datetime.datetime.now(datetime.timezone.utc)
        pol_id = policy_decision_id or f"pol-{uuid.uuid4().hex[:12]}"
        config_hash = snapshot.compute_hash()

        # Resolve explicit bot_id
        resolved_bot_id = bot_id or getattr(snapshot, "bot_id", None) or getattr(artifact, "bot_id", None)
        if resolved_bot_id == "default":
            candidate = getattr(snapshot, "bot_id", None)
            if candidate and candidate != "default":
                resolved_bot_id = candidate
            else:
                candidate = getattr(artifact, "bot_id", None)
                if candidate and candidate != "default":
                    resolved_bot_id = candidate

        # 1. Capture requested values
        requested_values = {
            "action": artifact.requested_action,
            "size_pct": artifact.requested_size_pct,
            "timing": artifact.requested_timing,
            "confidence": artifact.confidence,
        }

        # 2. Normalize values
        action = str(artifact.requested_action or "HOLD").strip().upper()
        if action not in ("BUY", "SELL", "HOLD"):
            action = "HOLD"

        timing = artifact.requested_timing or {}
        entry_mode = timing.get("entry_mode") or ("enter_now" if action in ("BUY", "SELL") else "watch_only")
        trigger_purpose = timing.get("trigger_purpose") or "none"

        normalized_values = {
            "action": action,
            "entry_mode": entry_mode,
            "trigger_purpose": trigger_purpose,
            "ticker": artifact.ticker.upper().strip(),
            "confidence": artifact.confidence,
        }

        reason_codes: list[str] = []
        gate_results: dict[str, Any] = {}

        # 3. Check Environmental Degradation and Quote Staleness
        if snapshot.is_degraded and action == "BUY":
            reason_codes.append("DEGRADED_ENVIRONMENT")
            gate_results["data_freshness_and_capacity"] = {
                "status": "FAIL",
                "degraded_reasons": snapshot.degraded_reasons,
            }
            pol_dec = PolicyDecision(
                policy_decision_id=pol_id,
                decision_id=artifact.decision_id,
                policy_version=POLICY_VERSION,
                config_hash=config_hash,
                evaluated_at=eval_time,
                requested_values=requested_values,
                normalized_values=normalized_values,
                approved_values={},
                disposition=PolicyDisposition.REJECT,
                reason_codes=reason_codes,
                gate_results=gate_results,
            )
            return pol_dec, None

        if snapshot.quote_age_hours > MAX_QUOTE_AGE_HOURS:
            reason_codes.append("STALE_QUOTE")
            gate_results["quote_freshness"] = {"status": "FAIL", "age_hours": snapshot.quote_age_hours}
            pol_dec = PolicyDecision(
                policy_decision_id=pol_id,
                decision_id=artifact.decision_id,
                policy_version=POLICY_VERSION,
                config_hash=config_hash,
                evaluated_at=eval_time,
                requested_values=requested_values,
                normalized_values=normalized_values,
                approved_values={},
                disposition=PolicyDisposition.REJECT,
                reason_codes=reason_codes,
                gate_results=gate_results,
            )
            return pol_dec, None

        gate_results["quote_freshness"] = {"status": "PASS", "age_hours": snapshot.quote_age_hours}

        # 4. Handle Non-Entry Actions
        if action == "HOLD":
            reason_codes.append("HOLD_ACTION_REQUESTED")
            pol_dec = PolicyDecision(
                policy_decision_id=pol_id,
                decision_id=artifact.decision_id,
                policy_version=POLICY_VERSION,
                config_hash=config_hash,
                evaluated_at=eval_time,
                requested_values=requested_values,
                normalized_values=normalized_values,
                approved_values={"action": "HOLD"},
                disposition=PolicyDisposition.CONVERT_TO_WATCH,
                reason_codes=reason_codes,
                gate_results=gate_results,
            )
            return pol_dec, None

        if entry_mode == "enter_on_condition":
            reason_codes.append("CONDITIONAL_ENTRY_ARMED")
            gate_results["conditional_trigger"] = timing.get("dynamic_trigger")
            pol_dec = PolicyDecision(
                policy_decision_id=pol_id,
                decision_id=artifact.decision_id,
                policy_version=POLICY_VERSION,
                config_hash=config_hash,
                evaluated_at=eval_time,
                requested_values=requested_values,
                normalized_values=normalized_values,
                approved_values={"action": "BUY", "entry_mode": "enter_on_condition"},
                disposition=PolicyDisposition.QUEUE_RESEARCH,
                reason_codes=reason_codes,
                gate_results=gate_results,
            )
            return pol_dec, None

        # 5. SELL Evaluation
        if action == "SELL":
            if not snapshot.is_held:
                reason_codes.append("NO_OPEN_POSITION")
                gate_results["position_check"] = {"status": "FAIL", "is_held": False}
                pol_dec = PolicyDecision(
                    policy_decision_id=pol_id,
                    decision_id=artifact.decision_id,
                    policy_version=POLICY_VERSION,
                    config_hash=config_hash,
                    evaluated_at=eval_time,
                    requested_values=requested_values,
                    normalized_values=normalized_values,
                    approved_values={},
                    disposition=PolicyDisposition.BLOCK,
                    reason_codes=reason_codes,
                    gate_results=gate_results,
                )
                return pol_dec, None

            # Approved SELL
            reason_codes.append("SELL_APPROVED")
            approved_values = {
                "action": "SELL",
                "qty_pct": 1.0,
            }
            pol_dec = PolicyDecision(
                policy_decision_id=pol_id,
                decision_id=artifact.decision_id,
                policy_version=POLICY_VERSION,
                config_hash=config_hash,
                evaluated_at=eval_time,
                requested_values=requested_values,
                normalized_values=normalized_values,
                approved_values=approved_values,
                disposition=PolicyDisposition.APPROVE,
                reason_codes=reason_codes,
                gate_results=gate_results,
            )

            intent_id = execution_intent_id or f"int-{uuid.uuid4().hex[:12]}"
            valid_to = eval_time + datetime.timedelta(seconds=DEFAULT_INTENT_TTL_SECONDS)
            slot_key = f"slot:{artifact.ticker.upper().strip()}:{artifact.decision_id}:SELL:{eval_time.strftime('%Y%m%d%H%M')}_{valid_to.strftime('%Y%m%d%H%M')}"
            idemp_key = hashlib.sha256(f"{slot_key}:{pol_id}".encode("utf-8")).hexdigest()
            intent = ExecutionIntent(
                execution_intent_id=intent_id,
                decision_id=artifact.decision_id,
                policy_decision_id=pol_id,
                bot_id=resolved_bot_id,
                ticker=artifact.ticker.upper().strip(),
                side="SELL",
                approved_size_pct=1.0,
                sizing_basis="POSITION_QTY",
                order_constraints={},
                reference_quote={
                    "price": snapshot.quote_price,
                    "timestamp": eval_time.isoformat(),
                    "source": snapshot.quote_source,
                    "age_hours": snapshot.quote_age_hours,
                },
                valid_from=eval_time,
                expires_at=valid_to,
                idempotency_key=idemp_key,
                slot_key=slot_key,
                status=IntentStatus.CREATED,
            )
            return pol_dec, intent

        # 6. BUY Evaluation (Entry Gates)
        # Check Confidence Gate
        if artifact.confidence < MIN_CONFIDENCE_THRESHOLD:
            reason_codes.append("CONFIDENCE_TOO_LOW")
            gate_results["confidence_check"] = {
                "status": "FAIL",
                "confidence": artifact.confidence,
                "threshold": MIN_CONFIDENCE_THRESHOLD,
            }
            pol_dec = PolicyDecision(
                policy_decision_id=pol_id,
                decision_id=artifact.decision_id,
                policy_version=POLICY_VERSION,
                config_hash=config_hash,
                evaluated_at=eval_time,
                requested_values=requested_values,
                normalized_values=normalized_values,
                approved_values={},
                disposition=PolicyDisposition.BLOCK,
                reason_codes=reason_codes,
                gate_results=gate_results,
            )
            return pol_dec, None

        # Check Degraded Snapshot (Stale/Missing marks on portfolio holdings block BUY)
        if snapshot.is_degraded:
            reason_codes.append("DEGRADED_SNAPSHOT_BLOCK_BUY")
            reason_codes.extend(snapshot.degraded_reasons)
            gate_results["snapshot_integrity"] = {"status": "FAIL", "reasons": snapshot.degraded_reasons}
            pol_dec = PolicyDecision(
                policy_decision_id=pol_id,
                decision_id=artifact.decision_id,
                policy_version=POLICY_VERSION,
                config_hash=config_hash,
                evaluated_at=eval_time,
                requested_values=requested_values,
                normalized_values=normalized_values,
                approved_values={},
                disposition=PolicyDisposition.BLOCK,
                reason_codes=reason_codes,
                gate_results=gate_results,
            )
            return pol_dec, None

        # Check Portfolio Drawdown Circuit Breaker
        if snapshot.drawdown_breaker_active:
            reason_codes.append("DRAWDOWN_BREAKER_ACTIVE")
            gate_results["drawdown_breaker"] = {"status": "FAIL"}
            pol_dec = PolicyDecision(
                policy_decision_id=pol_id,
                decision_id=artifact.decision_id,
                policy_version=POLICY_VERSION,
                config_hash=config_hash,
                evaluated_at=eval_time,
                requested_values=requested_values,
                normalized_values=normalized_values,
                approved_values={},
                disposition=PolicyDisposition.BLOCK,
                reason_codes=reason_codes,
                gate_results=gate_results,
            )
            return pol_dec, None

        # Check Strategy Health
        if snapshot.strategy_health_status == "CUT":
            reason_codes.append("STRATEGY_HEALTH_CUT")
            gate_results["strategy_health"] = {"status": "CUT"}
            pol_dec = PolicyDecision(
                policy_decision_id=pol_id,
                decision_id=artifact.decision_id,
                policy_version=POLICY_VERSION,
                config_hash=config_hash,
                evaluated_at=eval_time,
                requested_values=requested_values,
                normalized_values=normalized_values,
                approved_values={},
                disposition=PolicyDisposition.BLOCK,
                reason_codes=reason_codes,
                gate_results=gate_results,
            )
            return pol_dec, None

        # Sizing Calculation
        raw_size = resolve_policy_size_pct(
            base_fraction=snapshot.max_position_size_pct,
            confidence=artifact.confidence,
            max_position_pct=snapshot.max_position_size_pct,
            consensus_score=snapshot.internal_consensus_score,
            data_quality=snapshot.data_quality,
        )

        if snapshot.strategy_health_status == "REDUCE":
            raw_size *= 0.5
            reason_codes.append("STRATEGY_HEALTH_HALVED")

        # Capacity & Concentration Checks
        target_amount = snapshot.portfolio_equity * raw_size
        max_allowed_ticker_val = snapshot.portfolio_equity * snapshot.max_concentration_pct
        capped_amount = min(target_amount, snapshot.cash_balance)

        if snapshot.held_ticker_value + capped_amount > max_allowed_ticker_val:
            capped_amount = max(0.0, max_allowed_ticker_val - snapshot.held_ticker_value)
            reason_codes.append("CONCENTRATION_CAPPED")

        if capped_amount < 1.0 or snapshot.cash_balance < 1.0:
            reason_codes.append("INSUFFICIENT_CASH_OR_CAPACITY")
            gate_results["capacity"] = {
                "status": "FAIL",
                "capped_amount": capped_amount,
                "cash": snapshot.cash_balance,
            }
            pol_dec = PolicyDecision(
                policy_decision_id=pol_id,
                decision_id=artifact.decision_id,
                policy_version=POLICY_VERSION,
                config_hash=config_hash,
                evaluated_at=eval_time,
                requested_values=requested_values,
                normalized_values=normalized_values,
                approved_values={},
                disposition=PolicyDisposition.BLOCK,
                reason_codes=reason_codes,
                gate_results=gate_results,
            )
            return pol_dec, None

        final_size_pct = round(capped_amount / max(snapshot.portfolio_equity, 1.0), 4)
        disposition = (
            PolicyDisposition.APPROVE_WITH_CAP
            if (final_size_pct < raw_size or "CONCENTRATION_CAPPED" in reason_codes)
            else PolicyDisposition.APPROVE
        )

        approved_values = {
            "action": "BUY",
            "size_pct": final_size_pct,
            "approved_notional": round(capped_amount, 2),
        }
        reason_codes.append("BUY_APPROVED")

        pol_dec = PolicyDecision(
            policy_decision_id=pol_id,
            decision_id=artifact.decision_id,
            policy_version=POLICY_VERSION,
            config_hash=config_hash,
            evaluated_at=eval_time,
            requested_values=requested_values,
            normalized_values=normalized_values,
            approved_values=approved_values,
            disposition=disposition,
            reason_codes=reason_codes,
            gate_results=gate_results,
        )

        intent_id = execution_intent_id or f"int-{uuid.uuid4().hex[:12]}"
        valid_to = eval_time + datetime.timedelta(seconds=DEFAULT_INTENT_TTL_SECONDS)
        slot_key = f"slot:{artifact.ticker.upper().strip()}:{artifact.decision_id}:BUY:{eval_time.strftime('%Y%m%d%H%M')}_{valid_to.strftime('%Y%m%d%H%M')}"
        idemp_key = hashlib.sha256(f"{slot_key}:{pol_id}".encode("utf-8")).hexdigest()
        intent = ExecutionIntent(
            execution_intent_id=intent_id,
            decision_id=artifact.decision_id,
            policy_decision_id=pol_id,
            bot_id=resolved_bot_id,
            ticker=artifact.ticker.upper().strip(),
            side="BUY",
            approved_notional=round(capped_amount, 2),
            approved_size_pct=final_size_pct,
            sizing_basis="CASH_AND_CONCENTRATION_CAPPED",
            order_constraints={},
            reference_quote={
                "price": snapshot.quote_price,
                "timestamp": eval_time.isoformat(),
                "source": snapshot.quote_source,
                "age_hours": snapshot.quote_age_hours,
            },
            valid_from=eval_time,
            expires_at=valid_to,
            idempotency_key=idemp_key,
            slot_key=slot_key,
            status=IntentStatus.CREATED,
        )
        return pol_dec, intent
