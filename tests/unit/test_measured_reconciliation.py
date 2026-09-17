"""Unit tests for Measured Reconciliation and Adverse Signed Slippage."""

import datetime
import pytest
from app.trading.attribution.models import (
    ExecutionIntent,
    OrderAttempt,
    OrderAttemptStatus,
    ReconciliationVerdict,
)
from app.trading.attribution.reconciliation import (
    compute_adverse_signed_slippage_bps,
    reconcile_execution,
)


def test_adverse_signed_slippage_formulas():
    """Verify sign convention: positive is adverse, negative is price improvement."""
    # BUY: ref $100, fill $101 -> +100 bps (adverse)
    assert compute_adverse_signed_slippage_bps("BUY", 100.0, 101.0) == 100.0

    # BUY: ref $100, fill $99 -> -100 bps (price improvement)
    assert compute_adverse_signed_slippage_bps("BUY", 100.0, 99.0) == -100.0

    # SELL: ref $100, fill $99 -> +100 bps (adverse)
    assert compute_adverse_signed_slippage_bps("SELL", 100.0, 99.0) == 100.0

    # SELL: ref $100, fill $101 -> -100 bps (price improvement)
    assert compute_adverse_signed_slippage_bps("SELL", 100.0, 101.0) == -100.0


def test_reconciliation_favorable_price_improvement():
    """Negative adverse slippage is favorable price improvement and must NOT breach."""
    now = datetime.datetime.now(datetime.timezone.utc)
    intent = ExecutionIntent(
        execution_intent_id="int-favorable-1",
        decision_id="dec-1",
        policy_decision_id="pol-1",
        ticker="AAPL",
        side="BUY",
        approved_notional=1000.0,
        approved_size_pct=0.1,
        reference_quote={"price": 100.0},
        valid_from=now,
        expires_at=now + datetime.timedelta(hours=1),
        idempotency_key="idemp-1",
    )
    attempts = [OrderAttempt(order_attempt_id="att-1", execution_intent_id=intent.execution_intent_id, attempt_number=1, submitted_at=now)]
    order = {"order_id": "ord-1", "price": 100.0}
    # Filled at $99.80 (20 bps price improvement)
    fills = [{"qty": 10.0, "price": 99.80, "fees": 0.50, "filled_at": now}]

    rec = reconcile_execution(intent, attempts, order, fills)
    assert rec.verdict == ReconciliationVerdict.EXECUTION_MATCHED
    assert rec.slippage_bps == pytest.approx(-20.0, 0.01)
    assert rec.matched_quantity == 10.0
    assert rec.residual_quantity == 0.0


def test_reconciliation_adverse_slippage_breach():
    """Adverse slippage exceeding tolerance bps must trigger EXECUTION_SLIPPAGE_BREACH."""
    now = datetime.datetime.now(datetime.timezone.utc)
    intent = ExecutionIntent(
        execution_intent_id="int-breach-1",
        decision_id="dec-2",
        policy_decision_id="pol-2",
        ticker="TSLA",
        side="BUY",
        approved_notional=1000.0,
        approved_size_pct=0.1,
        allowed_slippage_bps=25.0,
        reference_quote={"price": 100.0},
        valid_from=now,
        expires_at=now + datetime.timedelta(hours=1),
        idempotency_key="idemp-2",
    )
    attempts = [OrderAttempt(order_attempt_id="att-2", execution_intent_id=intent.execution_intent_id, attempt_number=1, submitted_at=now)]
    order = {"order_id": "ord-2", "price": 100.0}
    # Filled at $100.50 (50 bps adverse slippage > 25 bps allowed)
    fills = [{"qty": 10.0, "price": 100.50, "fees": 0.50, "filled_at": now}]

    rec = reconcile_execution(intent, attempts, order, fills)
    assert rec.verdict == ReconciliationVerdict.EXECUTION_SLIPPAGE_BREACH
    assert rec.slippage_bps == pytest.approx(50.0, 0.01)
    assert any("SLIPPAGE_BREACH" in r for r in rec.discrepancy_reasons)


def test_reconciliation_partial_fill():
    """Partial fills report exact residual quantity and EXECUTION_PARTIAL."""
    now = datetime.datetime.now(datetime.timezone.utc)
    intent = ExecutionIntent(
        execution_intent_id="int-partial-1",
        decision_id="dec-3",
        policy_decision_id="pol-3",
        ticker="MSFT",
        side="BUY",
        approved_quantity=100.0,
        approved_size_pct=0.1,
        reference_quote={"price": 300.0},
        valid_from=now,
        expires_at=now + datetime.timedelta(hours=1),
        idempotency_key="idemp-3",
    )
    attempts = [OrderAttempt(order_attempt_id="att-3", execution_intent_id=intent.execution_intent_id, attempt_number=1, submitted_at=now)]
    order = {"order_id": "ord-3", "price": 300.0}
    # Only 60 shares filled out of 100 approved
    fills = [{"qty": 60.0, "price": 300.0, "fees": 1.0, "filled_at": now}]

    rec = reconcile_execution(intent, attempts, order, fills)
    assert rec.verdict == ReconciliationVerdict.EXECUTION_PARTIAL
    assert rec.matched_quantity == 60.0
    assert rec.residual_quantity == 40.0


def test_reconciliation_late_fill_past_expiry():
    """Fills occurring after intent expiration must trigger EXECUTION_LATE."""
    start = datetime.datetime(2026, 9, 16, 22, 0, 0, tzinfo=datetime.timezone.utc)
    expires = datetime.datetime(2026, 9, 16, 22, 15, 0, tzinfo=datetime.timezone.utc)
    fill_time = datetime.datetime(2026, 9, 16, 22, 20, 0, tzinfo=datetime.timezone.utc)

    intent = ExecutionIntent(
        execution_intent_id="int-late-1",
        decision_id="dec-4",
        policy_decision_id="pol-4",
        ticker="NVDA",
        side="BUY",
        approved_quantity=10.0,
        approved_size_pct=0.1,
        reference_quote={"price": 100.0},
        valid_from=start,
        expires_at=expires,
        idempotency_key="idemp-4",
    )
    attempts = [OrderAttempt(order_attempt_id="att-4", execution_intent_id=intent.execution_intent_id, attempt_number=1, submitted_at=start)]
    order = {"order_id": "ord-4", "price": 100.0}
    fills = [{"qty": 10.0, "price": 100.0, "fees": 0.5, "filled_at": fill_time}]

    rec = reconcile_execution(intent, attempts, order, fills)
    assert rec.verdict == ReconciliationVerdict.EXECUTION_LATE
    assert "FILL_EXCEEDED_EXPIRY" in rec.discrepancy_reasons
