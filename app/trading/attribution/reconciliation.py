"""Pure Measured Execution Reconciliation with Adverse Signed Slippage.

Implements pure domain arithmetic for comparing intended versus actual broker/paper execution:
- Adverse signed slippage (bps) where positive is adverse and negative is favorable price improvement
- Separated cost breakdown: decision-to-execution latency slippage vs liquidity/spread impact
- Deterministic verdict selection: MATCHED, PARTIAL, SLIPPAGE_BREACH, COST_BREACH, LATE, REJECTED
"""

from __future__ import annotations

import datetime
import uuid
from typing import Any, Optional

from app.trading.attribution.models import (
    ExecutionIntent,
    ExecutionReconciliation,
    OrderAttempt,
    ReconciliationVerdict,
)


def compute_adverse_signed_slippage_bps(side: str, reference_price: float, fill_price: float) -> float:
    """Computes signed slippage in basis points where positive represents adverse execution.

    BUY:  (fill_price - reference_price) / reference_price * 10,000
          Higher fill price is adverse (positive bps).
          Lower fill price is favorable price improvement (negative bps).

    SELL: (reference_price - fill_price) / reference_price * 10,000
          Lower fill price is adverse (positive bps).
          Higher fill price is favorable price improvement (negative bps).
    """
    if reference_price <= 0:
        return 0.0

    side_norm = side.upper().strip()
    if side_norm == "BUY":
        return ((fill_price - reference_price) / reference_price) * 10000.0
    elif side_norm == "SELL":
        return ((reference_price - fill_price) / reference_price) * 10000.0
    return 0.0


def reconcile_execution(
    intent: ExecutionIntent,
    attempts: list[OrderAttempt],
    order: dict[str, Any],
    fills: list[dict[str, Any]],
    reference_quote: Optional[dict[str, Any]] = None,
    modeled_friction_bps: float = 10.0,
) -> ExecutionReconciliation:
    """Pure function calculating execution reconciliation and verdict."""
    reconciliation_id = f"rec-{uuid.uuid4().hex[:12]}"
    now = datetime.datetime.now(datetime.timezone.utc)

    # 1. Total filled quantity and volume-weighted average price (VWAP)
    total_qty = 0.0
    total_value = 0.0
    actual_fees = 0.0
    last_fill_time: Optional[datetime.datetime] = None

    for fill in fills:
        fq = float(fill.get("qty", 0.0))
        fp = float(fill.get("price", 0.0))
        total_qty += fq
        total_value += (fq * fp)
        actual_fees += float(fill.get("fees", 0.0))
        f_time = fill.get("filled_at")
        if f_time:
            if isinstance(f_time, str):
                try:
                    f_time = datetime.datetime.fromisoformat(f_time.replace("Z", "+00:00"))
                except Exception:
                    f_time = None
            if isinstance(f_time, datetime.datetime) and f_time.tzinfo is None:
                f_time = f_time.replace(tzinfo=datetime.timezone.utc)
            if f_time and (last_fill_time is None or f_time > last_fill_time):
                last_fill_time = f_time

    vwap = (total_value / total_qty) if total_qty > 0 else 0.0

    # 2. Reference Price Resolution
    quote = reference_quote or intent.reference_quote or {}
    decision_ref_price = float(quote.get("price") or vwap or 1.0)
    exec_ref_price = float(order.get("price") or decision_ref_price)

    # 3. Slippage Calculations
    adverse_slippage_bps = compute_adverse_signed_slippage_bps(intent.side, decision_ref_price, vwap) if vwap > 0 else 0.0
    latency_slippage_bps = compute_adverse_signed_slippage_bps(intent.side, decision_ref_price, exec_ref_price)
    spread_impact_bps = compute_adverse_signed_slippage_bps(intent.side, exec_ref_price, vwap) if vwap > 0 else 0.0

    # 4. Quantity and Residual Resolution
    approved_qty = intent.approved_quantity or (
        (intent.approved_notional / decision_ref_price) if intent.approved_notional and decision_ref_price > 0 else total_qty
    )
    residual_qty = max(0.0, approved_qty - total_qty)
    matched_qty = min(total_qty, approved_qty)

    # 5. Verdict Determination
    discrepancy_reasons: list[str] = []
    tolerance_bps = intent.allowed_slippage_bps or 25.0

    norm_expires_at = intent.expires_at
    if norm_expires_at and norm_expires_at.tzinfo is None:
        norm_expires_at = norm_expires_at.replace(tzinfo=datetime.timezone.utc)
    if last_fill_time and last_fill_time.tzinfo is None:
        last_fill_time = last_fill_time.replace(tzinfo=datetime.timezone.utc)

    if total_qty <= 0.0001:
        verdict = ReconciliationVerdict.EXECUTION_REJECTED
        discrepancy_reasons.append("ZERO_QUANTITY_FILLED")
    elif residual_qty > 0.001 and (residual_qty / approved_qty) > 0.05:
        verdict = ReconciliationVerdict.EXECUTION_PARTIAL
        discrepancy_reasons.append(f"RESIDUAL_QUANTITY_{residual_qty:.4f}")
    elif last_fill_time and norm_expires_at and last_fill_time > norm_expires_at:
        verdict = ReconciliationVerdict.EXECUTION_LATE
        discrepancy_reasons.append("FILL_EXCEEDED_EXPIRY")
    elif adverse_slippage_bps > tolerance_bps:
        verdict = ReconciliationVerdict.EXECUTION_SLIPPAGE_BREACH
        discrepancy_reasons.append(f"SLIPPAGE_BREACH_{adverse_slippage_bps:.1f}bps_GT_{tolerance_bps:.1f}bps")
    elif actual_fees > 0 and (actual_fees / max(total_value, 1.0)) * 10000.0 > (modeled_friction_bps * 1.5):
        verdict = ReconciliationVerdict.EXECUTION_COST_BREACH
        discrepancy_reasons.append("FEE_EXCEEDED_MODELED_FRICTION")
    else:
        verdict = ReconciliationVerdict.EXECUTION_MATCHED

    cost_breakdown = {
        "decision_ref_price": decision_ref_price,
        "exec_ref_price": exec_ref_price,
        "fill_vwap": vwap,
        "adverse_slippage_bps": adverse_slippage_bps,
        "latency_slippage_bps": latency_slippage_bps,
        "spread_impact_bps": spread_impact_bps,
        "modeled_friction_bps": modeled_friction_bps,
        "actual_fees": actual_fees,
    }

    return ExecutionReconciliation(
        reconciliation_id=reconciliation_id,
        execution_intent_id=intent.execution_intent_id,
        order_attempt_id=attempts[0].order_attempt_id if attempts else "",
        order_id=order.get("order_id") or "",
        intended_qty=approved_qty,
        filled_qty=total_qty,
        reference_price=decision_ref_price,
        expected_price=exec_ref_price,
        realized_price=vwap,
        fees=actual_fees,
        realized_slippage_bps=adverse_slippage_bps,
        residual_qty=residual_qty,
        matched_quantity=matched_qty,
        verdict=verdict,
        discrepancy_reasons=discrepancy_reasons,
        reconciled_at=now,
        effective_mode=intent.effective_mode,
    )
