"""Unified Production Trade Facade.

The sole authoritative trade execution entry point for:
- Pipeline Stage 4
- Interactive Trading Tools (buy_stock, sell_stock)
- Emergency and Scheduled Risk Exits (stop-loss, take-profit)
- Recovery and Administrative Actions

Resolves effective control-plane mode (OBSERVE, SHADOW, ENFORCE) once per request.
Guarantees:
1. Entry points cannot independently choose execution behavior.
2. In SHADOW and ENFORCE, policy denial invokes neither executor nor legacy trader.
3. In OBSERVE, policy is advisory and evaluated for measurement while legacy trader executes.
4. In ENFORCE, execution routes strictly through execute_intent() with atomic slot/reservation admission.
5. Never falls back to legacy execution after an ENFORCE error.
6. Returns a stable, typed result contract distinguishing:
   - POLICY_DENIED
   - SIMULATED
   - COMMITTED
   - ALREADY_PROCESSED
   - SUPERSEDED
   - EXPIRED
   - RETRYABLE_ERROR
"""

from __future__ import annotations

import datetime
import hashlib
import logging
import uuid
from enum import Enum
from typing import Any, Optional

from app.db import mongo_query, mongo_store
from app.trading.attribution.models import (
    DecisionArtifact,
    ExecutionIntent,
    IntentStatus,
    PolicyDecision,
    PolicyDisposition,
)
from app.trading.attribution import repository
from app.trading.control_plane import ControlPlaneMode, resolve_control_plane_mode
from app.trading.executor import IntentExecutionRejected, execute_intent
from app.trading.policy.policy_translator import PolicyInputSnapshot, PolicyTranslator
from app.trading.policy.snapshot_service import build_policy_snapshot

logger = logging.getLogger(__name__)


class TradeResultStatus(str, Enum):
    COMMITTED = "COMMITTED"
    SIMULATED = "SIMULATED"
    POLICY_DENIED = "POLICY_DENIED"
    ALREADY_PROCESSED = "ALREADY_PROCESSED"
    SUPERSEDED = "SUPERSEDED"
    EXPIRED = "EXPIRED"
    RETRYABLE_ERROR = "RETRYABLE_ERROR"
    RECOVERY_REQUIRED = "RECOVERY_REQUIRED"


class TradeFacade:
    """Production Trade Facade governing all trading operations."""

    @classmethod
    async def submit_trade(
        cls,
        bot_id: str,
        ticker: str,
        action: str,  # "BUY" or "SELL"
        decision_artifact: Optional[DecisionArtifact] = None,
        size_pct: Optional[float] = None,
        current_price: Optional[float] = None,
        cycle_id: Optional[str] = None,
        idempotency_key: Optional[str] = None,
        producer: str = "trade_facade",
        model: str = "system",
        confidence: int = 80,
        stop_loss_price: Optional[float] = None,
        take_profit_price: Optional[float] = None,
        exit_style: Optional[str] = None,
        is_emergency: bool = False,
        allow_supersede: bool = True,
        slot_key: Optional[str] = None,
        strict_capacity: bool = False,
    ) -> dict[str, Any]:
        """Execute or simulate a trade through the unified control plane."""
        ticker = ticker.upper().strip()
        action = action.upper().strip()
        now = datetime.datetime.now(datetime.timezone.utc)

        # 1. Resolve Effective Mode
        effective_mode = resolve_control_plane_mode(bot_id)

        # 2. Derive Request Idempotency Key
        if not idempotency_key:
            raw_key = f"{bot_id}:{ticker}:{action}:{cycle_id or 'adhoc'}"
            idempotency_key = hashlib.sha256(raw_key.encode("utf-8")).hexdigest()

        is_approved = True
        policy_dec = None
        intent = None
        quote_price = current_price
        quote_age_hours = 0.0

        try:
            # 3. Check Idempotency Replay
            existing_intent = mongo_store.find_docs(
                repository.COLL_EXECUTION_INTENTS,
                {"idempotency_key": idempotency_key},
                limit=1,
            )
            if existing_intent:
                intent_doc = existing_intent[0]
                intent_id = intent_doc.get("execution_intent_id")
                intent_status = intent_doc.get("status")

                if intent_status == IntentStatus.CONSUMED.value:
                    # Query order/fill result
                    fill = mongo_query.find_row(
                        "trade_fills",
                        {"execution_intent_id": intent_id},
                        ["fill_id", "order_id", "price", "qty", "fees", "side"],
                    )
                    return {
                        "status": TradeResultStatus.ALREADY_PROCESSED.value,
                        "effective_mode": effective_mode.value,
                        "execution_intent_id": intent_id,
                        "idempotency_key": idempotency_key,
                        "trade_executed": True,
                        "fill": {
                            "fill_id": fill[0] if fill else None,
                            "order_id": fill[1] if fill else None,
                            "price": float(fill[2]) if fill and fill[2] is not None else None,
                            "qty": float(fill[3]) if fill and fill[3] is not None else None,
                            "fees": float(fill[4]) if fill and fill[4] is not None else 0.0,
                        } if fill else {},
                    }
                elif intent_status == IntentStatus.SUPERSEDED.value:
                    return {
                        "status": TradeResultStatus.SUPERSEDED.value,
                        "effective_mode": effective_mode.value,
                        "execution_intent_id": intent_id,
                        "trade_executed": False,
                    }
                elif intent_status == IntentStatus.EXPIRED.value:
                    return {
                        "status": TradeResultStatus.EXPIRED.value,
                        "effective_mode": effective_mode.value,
                        "execution_intent_id": intent_id,
                        "trade_executed": False,
                    }

            # 4. Fetch Market Quote
            if quote_price is None or quote_price <= 0:
                from app.trading.paper_trader import _get_current_price
                p_val, p_age = _get_current_price(ticker)
                quote_price = p_val
                quote_age_hours = p_age or 0.0

            if quote_price is None or quote_price <= 0:
                if effective_mode != ControlPlaneMode.OBSERVE:
                    return {
                        "status": TradeResultStatus.RETRYABLE_ERROR.value,
                        "error": f"No price data available for {ticker}",
                        "effective_mode": effective_mode.value,
                        "trade_executed": False,
                    }

            # 5. Build Policy Snapshot
            snapshot, snapshot_payload = build_policy_snapshot(
                bot_id=bot_id,
                ticker=ticker,
                quote_price=quote_price or 1.0,
                quote_age_hours=quote_age_hours,
            )

            # 6. Build or Validate Decision Artifact
            if decision_artifact is None:
                c_id = cycle_id or f"cycle-{uuid.uuid4().hex[:8]}"
                d_id = f"dec-{uuid.uuid4().hex[:12]}"
                decision_artifact = DecisionArtifact(
                    decision_id=d_id,
                    cycle_id=c_id,
                    ticker=ticker,
                    producer=producer,
                    model=model,
                    requested_action=action,
                    requested_size_pct=size_pct,
                    confidence=confidence,
                    reference_quote={"price": quote_price or 1.0, "age_hours": quote_age_hours},
                )
                repository.save_decision_artifact(decision_artifact)
            else:
                repository.save_decision_artifact(decision_artifact)

            # 7. Evaluate Policy via PolicyTranslator
            policy_dec, intent = PolicyTranslator.evaluate(
                decision_artifact,
                snapshot,
            )
            policy_dec.effective_mode = effective_mode.value
            repository.save_policy_decision(policy_dec)

            if intent:
                intent.effective_mode = effective_mode.value
                intent.idempotency_key = idempotency_key
                repository.save_execution_intent(intent)

            is_approved = policy_dec.is_approved
        except Exception as advisory_err:
            if effective_mode == ControlPlaneMode.OBSERVE:
                logger.warning(
                    "[TradeFacade] Advisory policy evaluation skipped in OBSERVE mode: %s",
                    advisory_err,
                )
            else:
                raise

        # 8. Route Based on Mode and Policy Approval
        # Rule: In SHADOW and ENFORCE, policy denial invokes neither executor nor legacy trader.
        if policy_dec and not is_approved and effective_mode in (ControlPlaneMode.SHADOW, ControlPlaneMode.ENFORCE):
            logger.info(
                "[TradeFacade] Policy DENIED %s %s for bot %s in %s mode (reasons: %s)",
                action, ticker, bot_id, effective_mode.value, policy_dec.reason_codes,
            )
            return {
                "status": TradeResultStatus.POLICY_DENIED.value,
                "effective_mode": effective_mode.value,
                "disposition": policy_dec.disposition.value,
                "reason_codes": policy_dec.reason_codes,
                "decision_id": decision_artifact.decision_id if decision_artifact else None,
                "policy_decision_id": policy_dec.policy_decision_id,
                "trade_executed": False,
            }

        # Handle OBSERVE Mode
        if effective_mode == ControlPlaneMode.OBSERVE:
            logger.info(
                "[TradeFacade] OBSERVE mode active for %s %s (policy disposition: %s)",
                action, ticker, policy_dec.disposition.value if policy_dec else "SKIPPED",
            )
            legacy_res = await cls._execute_legacy(
                bot_id=bot_id,
                ticker=ticker,
                action=action,
                size_pct=size_pct or (intent.approved_size_pct if intent else 0.10),
                current_price=quote_price or 1.0,
                cycle_id=cycle_id,
                stop_loss_price=stop_loss_price,
                take_profit_price=take_profit_price,
                exit_style=exit_style,
                is_emergency=is_emergency,
                decision_id=decision_artifact.decision_id if decision_artifact else None,
                execution_intent_id=intent.execution_intent_id if intent else None,
                strict_capacity=strict_capacity,
            )
            return {
                "status": TradeResultStatus.COMMITTED.value if not legacy_res.get("error") else TradeResultStatus.RETRYABLE_ERROR.value,
                "effective_mode": "OBSERVE",
                "enforced": False,
                "policy_disposition": policy_dec.disposition.value if policy_dec else "SKIPPED",
                "policy_would_deny": not is_approved if policy_dec else False,
                "policy_reasons": policy_dec.reason_codes if policy_dec else [],
                "trade_executed": bool(not legacy_res.get("error")),
                "trade": legacy_res,
                "trade_res": legacy_res,
            }

        # Handle SHADOW Mode
        if effective_mode == ControlPlaneMode.SHADOW:
            logger.info("[TradeFacade] SHADOW mode active: simulating %s %s", action, ticker)
            if not intent:
                return {
                    "status": TradeResultStatus.POLICY_DENIED.value,
                    "effective_mode": "SHADOW",
                    "trade_executed": False,
                    "reason_codes": policy_dec.reason_codes,
                }
            intent.effective_mode = "SHADOW"
            repository.save_execution_intent(intent)
            sim_res = await execute_intent(
                intent.execution_intent_id,
                account_context={"bot_id": bot_id},
                current_quote={"price": quote_price, "age_hours": quote_age_hours},
            )
            return {
                "status": TradeResultStatus.SIMULATED.value,
                "effective_mode": "SHADOW",
                "enforced": False,
                "simulated": True,
                "trade_executed": False,  # live trade was not executed
                "simulation": sim_res,
                "decision_id": decision_artifact.decision_id,
                "execution_intent_id": intent.execution_intent_id,
            }

        # Handle ENFORCE Mode
        if effective_mode == ControlPlaneMode.ENFORCE:
            logger.info("[TradeFacade] ENFORCE mode active: executing %s %s exclusively via execute_intent()", action, ticker)
            if not intent:
                return {
                    "status": TradeResultStatus.POLICY_DENIED.value,
                    "effective_mode": "ENFORCE",
                    "trade_executed": False,
                    "reason_codes": policy_dec.reason_codes,
                }

            target_slot_key = slot_key or f"slot:{bot_id}:{ticker}"
            required_notional = float(intent.approved_notional or 0.0)

            # Atomic Admission
            try:
                admit_res = repository.admit_execution_intent(
                    intent=intent,
                    slot_key=target_slot_key,
                    required_notional=required_notional,
                    policy_decision=policy_dec,
                    policy_snapshot=snapshot,
                    allow_supersede=allow_supersede,
                )
            except repository.AdmissionError as adm_err:
                logger.warning("[TradeFacade] Admission rejected in ENFORCE mode: %s", adm_err)
                return {
                    "status": TradeResultStatus.POLICY_DENIED.value if isinstance(adm_err, repository.DegradedSnapshotAdmissionError) else TradeResultStatus.RETRYABLE_ERROR.value,
                    "effective_mode": "ENFORCE",
                    "error": str(adm_err),
                    "reason_code": getattr(adm_err, "reason_code", "ADMISSION_FAILED"),
                    "trade_executed": False,
                }

            if admit_res.get("is_duplicate"):
                return {
                    "status": TradeResultStatus.ALREADY_PROCESSED.value,
                    "effective_mode": "ENFORCE",
                    "trade_executed": True,
                    "execution_intent_id": intent.execution_intent_id,
                }

            # Transactional Execution via execute_intent
            try:
                exec_res = await execute_intent(
                    intent.execution_intent_id,
                    account_context={"bot_id": bot_id},
                    current_quote={"price": quote_price, "age_hours": quote_age_hours},
                )
                return {
                    "status": TradeResultStatus.COMMITTED.value,
                    "effective_mode": "ENFORCE",
                    "enforced": True,
                    "trade_executed": True,
                    "trade": exec_res,
                    "decision_id": decision_artifact.decision_id,
                    "execution_intent_id": intent.execution_intent_id,
                    "order_id": exec_res.get("order_id"),
                }
            except IntentExecutionRejected as rej_err:
                logger.error("[TradeFacade] execute_intent rejected in ENFORCE mode: %s", rej_err)
                # NEVER fall back to legacy execution on ENFORCE error
                return {
                    "status": TradeResultStatus.RETRYABLE_ERROR.value,
                    "effective_mode": "ENFORCE",
                    "error": str(rej_err),
                    "reason_code": rej_err.reason_code,
                    "trade_executed": False,
                }
            except Exception as unk_err:
                logger.exception("[TradeFacade] Unexpected execution failure in ENFORCE mode: %s", unk_err)
                return {
                    "status": TradeResultStatus.RECOVERY_REQUIRED.value,
                    "effective_mode": "ENFORCE",
                    "error": str(unk_err),
                    "trade_executed": False,
                }

        return {
            "status": TradeResultStatus.RETRYABLE_ERROR.value,
            "error": f"Invalid mode configuration: {effective_mode}",
            "trade_executed": False,
        }

    @classmethod
    async def _execute_legacy(
        cls,
        bot_id: str,
        ticker: str,
        action: str,
        size_pct: float,
        current_price: float,
        cycle_id: Optional[str] = None,
        stop_loss_price: Optional[float] = None,
        take_profit_price: Optional[float] = None,
        exit_style: Optional[str] = None,
        is_emergency: bool = False,
        decision_id: Optional[str] = None,
        execution_intent_id: Optional[str] = None,
        strict_capacity: bool = False,
    ) -> dict[str, Any]:
        """Adapter invoking legacy buy() or sell() exclusively for OBSERVE mode."""
        from app.trading.paper_trader import buy, sell
        if action == "BUY":
            return await buy(
                bot_id=bot_id,
                ticker=ticker,
                size_pct=size_pct,
                current_price=current_price,
                cycle_id=cycle_id,
                stop_loss_price=stop_loss_price,
                take_profit_price=take_profit_price,
                exit_style=exit_style,
                execution_intent_id=execution_intent_id,
                decision_id=decision_id,
                called_via_facade=True,
                strict_capacity=strict_capacity,
            )
        elif action == "SELL":
            return await sell(
                bot_id=bot_id,
                ticker=ticker,
                current_price=current_price,
                cycle_id=cycle_id,
                qty_pct=size_pct if size_pct and size_pct <= 1.0 else 1.0,
                execution_intent_id=execution_intent_id,
                decision_id=decision_id,
                is_emergency_risk_exit=is_emergency,
                called_via_facade=True,
            )
        return {"error": f"Unsupported action {action}"}
