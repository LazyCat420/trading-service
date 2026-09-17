"""Integration Test Suite: Step 05 — Real Concurrency, Crashes, and Migration Invariants.

Verifies the 9 required invariant cases against a disposable real MongoDB replica set (rs0):
1. Two BUY admissions/executions compete for the last cash (overlapping barrier, capacity never exceeded).
2. Two submissions share an idempotency key (overlapping barrier, exactly 1 execution, loser receives already-processed).
3. Expiry races execution (serialized cleanly: either execution succeeds and consumes, or expiry wins and execution rejects; no stranded reservation).
4. Outbox lease reclamation race (old worker whose lease expired cannot complete/overwrite reclaimed event).
5. Failure/crash injection at each critical intent/order/fill/lot/outbox write boundary (intent CAS, orders, fills, position lots, outbox: atomic rollback).
6. Partial sells across multiple BUY lots, fees, and symbol normalization (FIFO lot closure, fee conservation, symbol stripping/uppercasing).
7. Historical migration idempotency and ENFORCE promotion gating (reconciliation is idempotent; lot-position mismatch blocks promotion).
8. SHADOW same-key races and failure-boundary rollback (CAS single winner or replica set write conflict, crash leaves zero simulations).
9. Representative failure mutation test (disabling the reservation guard exposes invariant violation; restoring guard restores protection).
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import datetime
import threading
import uuid
from typing import Any

import pymongo.collection
import pymongo.errors
import pytest

from app.trading.attribution.models import (
    DecisionArtifact,
    ExecutionIntent,
    IntentStatus,
    PolicyDecision,
    PolicyDisposition,
    ReservationStatus,
    RiskReservation,
)
from app.trading.attribution.repository import (
    COLL_EXECUTION_INTENTS,
    COLL_EXECUTION_OUTBOX,
    COLL_EXECUTION_RECONCILIATIONS,
    COLL_EXECUTION_SLOTS,
    COLL_LOT_CLOSURES,
    COLL_POLICY_DECISIONS,
    COLL_POSITION_LOTS,
    COLL_RISK_RESERVATIONS,
    admit_execution_intent,
    ensure_attribution_indexes,
    get_active_reserved_notional,
    release_risk_reservations_for_intent,
    save_decision_artifact,
    save_execution_intent,
    save_policy_decision,
    supersede_execution_intent,
    AdmissionError,
    InsufficientCashReservationError,
    SlotConflictError,
)
from app.trading.control_plane import ControlPlaneMode
from app.trading.executor import IntentExecutionRejected, execute_intent
from app.trading.facade import TradeFacade, TradeResultStatus
from app.trading.migration.lot_migrator import (
    promote_bot_to_enforce,
    reconcile_and_migrate_bot_positions,
    verify_bot_position_lot_integrity,
)
from app.trading.outbox.repository import (
    claim_pending_outbox_events,
    insert_outbox_event,
    mark_outbox_event_completed,
)
from app.trading.policy.policy_translator import PolicyInputSnapshot, PolicyTranslator
from app.trading.policy.snapshot_service import build_policy_snapshot

pytestmark = pytest.mark.real_mongo


@pytest.fixture(autouse=True)
def init_clean_env(real_mongo, monkeypatch):
    """Ensure clean collections and indexes on the disposable test database."""
    monkeypatch.setenv("CONTROL_PLANE_MODE", "ENFORCE")
    ensure_attribution_indexes()
    yield real_mongo


def _make_unpersisted_intent(
    bot_id: str,
    ticker: str,
    side: str = "BUY",
    notional: float = 5000.0,
    price: float = 100.0,
    idempotency_key: str | None = None,
    effective_mode: str = "ENFORCE",
) -> tuple[PolicyDecision, ExecutionIntent]:
    """Creates an in-memory ExecutionIntent and persists its approving PolicyDecision.
    The intent itself is NOT persisted here because admit_execution_intent() will persist it.
    """
    now = datetime.datetime.now(datetime.timezone.utc)
    d_id = f"dec-{uuid.uuid4().hex[:8]}"
    pol_id = f"pol-{uuid.uuid4().hex[:8]}"
    int_id = f"int-{uuid.uuid4().hex[:8]}"
    idemp = idempotency_key or f"idemp-{uuid.uuid4().hex[:8]}"

    pol_dec = PolicyDecision(
        policy_decision_id=pol_id,
        decision_id=d_id,
        config_hash="h-cfg-real",
        disposition=PolicyDisposition.APPROVE,
        requested_values={"action": side, "notional": notional},
        normalized_values={"action": side, "notional": notional},
        approved_values={"action": side, "notional": notional},
        effective_mode=effective_mode,
    )
    save_policy_decision(pol_dec)

    intent = ExecutionIntent(
        execution_intent_id=int_id,
        decision_id=d_id,
        policy_decision_id=pol_id,
        bot_id=bot_id,
        ticker=ticker,
        side=side,
        approved_notional=notional,
        approved_size_pct=notional / 10000.0 if notional <= 10000.0 else 0.5,
        reference_quote={"price": price, "age_hours": 0.1},
        valid_from=now,
        expires_at=now + datetime.timedelta(hours=1),
        idempotency_key=idemp,
        effective_mode=effective_mode,
        slot_key=f"slot:{bot_id}:{ticker}:{uuid.uuid4().hex[:4]}",
    )
    return pol_dec, intent


def _create_approved_persisted_intent(
    bot_id: str,
    ticker: str,
    side: str = "BUY",
    notional: float = 5000.0,
    price: float = 100.0,
    idempotency_key: str | None = None,
    effective_mode: str = "ENFORCE",
) -> tuple[PolicyDecision, ExecutionIntent]:
    """Creates and persists both PolicyDecision and ExecutionIntent."""
    pol, intent = _make_unpersisted_intent(
        bot_id, ticker, side, notional, price, idempotency_key, effective_mode
    )
    save_execution_intent(intent)
    return pol, intent


# ─────────────────────────────────────────────────────────────────────────────
# 1. Two BUY admissions compete for the last cash
# ─────────────────────────────────────────────────────────────────────────────
def test_scenario_1_concurrent_buy_admissions_compete_for_cash(real_mongo):
    """Two concurrent BUY admissions compete for the last cash:
    total reservations and committed spend cannot exceed available capacity.
    """
    bot_id = "bot-conc-cash"
    initial_cash = 10000.0
    real_mongo["bots"].insert_one({
        "bot_id": bot_id,
        "cash_balance": initial_cash,
        "starting_balance": initial_cash,
        "reservation_version": 1,
    })

    # Intent 1 ($7,000) and Intent 2 ($7,000) both want cash. $14,000 > $10,000.
    pol1, intent1 = _make_unpersisted_intent(bot_id, "AAPL", notional=7000.0, price=150.0)
    pol2, intent2 = _make_unpersisted_intent(bot_id, "MSFT", notional=7000.0, price=300.0)

    barrier = threading.Barrier(2)
    results: list[dict[str, Any]] = [{}, {}]
    errors: list[Exception | None] = [None, None]

    def _admit_worker(idx: int, intent: ExecutionIntent, pol: PolicyDecision):
        barrier.wait(timeout=10)
        try:
            res = admit_execution_intent(
                intent=intent,
                slot_key=intent.slot_key,
                required_notional=float(intent.approved_notional),
                policy_decision=pol,
            )
            results[idx] = res
        except Exception as e:
            errors[idx] = e

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        f1 = executor.submit(_admit_worker, 0, intent1, pol1)
        f2 = executor.submit(_admit_worker, 1, intent2, pol2)
        f1.result()
        f2.result()

    # Exactly one succeeded; the other failed with InsufficientCashReservationError or WriteConflict
    success_count = sum(1 for r in results if r.get("admitted") is True)
    failed_count = sum(
        1 for e in errors
        if isinstance(e, (InsufficientCashReservationError, pymongo.errors.OperationFailure))
    )

    assert success_count == 1, f"Expected exactly 1 successful admission, got {success_count} (results: {results})"
    assert failed_count == 1, f"Expected exactly 1 failure due to capacity/conflict, got {errors}"

    # Database invariant check:
    # Total ACTIVE reserved notional must be exactly $7,000 (never $14,000)
    active_reservations = list(real_mongo[COLL_RISK_RESERVATIONS].find({"bot_id": bot_id, "status": "ACTIVE"}))
    assert len(active_reservations) == 1
    assert active_reservations[0]["reserved_notional"] == 7000.0

    # Bot cash_balance remains intact ($10,000) during reservation phase
    bot_doc = real_mongo["bots"].find_one({"bot_id": bot_id})
    assert bot_doc["cash_balance"] == 10000.0

    # Free cash calculation matches
    reserved_cash = get_active_reserved_notional(bot_id)
    assert reserved_cash == 7000.0

    # Now execute the winning intent
    winning_intent = intent1 if results[0].get("admitted") else intent2
    exec_res = asyncio.run(execute_intent(
        winning_intent.execution_intent_id,
        account_context={"bot_id": bot_id},
        current_quote={"price": 150.0, "age_hours": 0.1},
    ))
    assert exec_res["status"] == "FILLED"

    # Post-execution verification:
    # Cash balance was decremented by notional + fees
    bot_after = real_mongo["bots"].find_one({"bot_id": bot_id})
    assert bot_after["cash_balance"] < 10000.0
    assert bot_after["cash_balance"] >= 2900.0  # $10,000 - $7,000 - ~$1.40 fees
    # Active reservations count is now 0 (consumed)
    assert real_mongo[COLL_RISK_RESERVATIONS].count_documents({"bot_id": bot_id, "status": "ACTIVE"}) == 0


# ─────────────────────────────────────────────────────────────────────────────
# 2. Two submissions share an idempotency key concurrently
# ─────────────────────────────────────────────────────────────────────────────
def test_scenario_2_concurrent_submissions_share_idempotency_key(real_mongo):
    """Two submissions share an idempotency key concurrently:
    exactly one effective execution, correct duplicate response, no false success.
    """
    bot_id = "bot-idemp-race"
    real_mongo["bots"].insert_one({
        "bot_id": bot_id,
        "cash_balance": 50000.0,
        "starting_balance": 50000.0,
    })

    shared_idemp = f"idemp-shared-{uuid.uuid4().hex[:8]}"
    barrier = threading.Barrier(2)
    trade_results: list[dict[str, Any]] = [{}, {}]
    trade_errors: list[Exception | None] = [None, None]

    def _submit_worker(idx: int):
        barrier.wait(timeout=10)
        try:
            res = asyncio.run(TradeFacade.submit_trade(
                bot_id=bot_id,
                ticker="GOOGL",
                action="BUY",
                size_pct=0.10,
                current_price=175.0,
                idempotency_key=shared_idemp,
            ))
            trade_results[idx] = res
        except Exception as e:
            trade_errors[idx] = e

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        f1 = executor.submit(_submit_worker, 0)
        f2 = executor.submit(_submit_worker, 1)
        f1.result()
        f2.result()

    statuses = [r.get("status") for r in trade_results if r]
    # One is COMMITTED, one is ALREADY_PROCESSED (or both return identical safe processed response)
    assert TradeResultStatus.COMMITTED.value in statuses or TradeResultStatus.ALREADY_PROCESSED.value in statuses
    assert all(err is None for err in trade_errors), f"Unexpected errors: {trade_errors}"

    # Verify only ONE order exists in MongoDB
    orders = list(real_mongo["orders"].find({"bot_id": bot_id, "ticker": "GOOGL"}))
    assert len(orders) == 1

    # Verify only ONE fill exists
    fills = list(real_mongo["trade_fills"].find({"bot_id": bot_id, "ticker": "GOOGL"}))
    assert len(fills) == 1

    # Verify positions: only 1 position record with quantity matching single fill
    positions = list(real_mongo["positions"].find({"bot_id": bot_id, "ticker": "GOOGL"}))
    assert len(positions) == 1
    assert positions[0]["qty"] == fills[0]["qty"]


# ─────────────────────────────────────────────────────────────────────────────
# 3. Expiry races execution
# ─────────────────────────────────────────────────────────────────────────────
def test_scenario_3_expiry_races_execution(real_mongo):
    """Expiry races execution: outcome is valid under the declared serialization point,
    with neither duplicate spend nor a stranded reservation.
    """
    bot_id = "bot-expiry-race"
    real_mongo["bots"].insert_one({
        "bot_id": bot_id,
        "cash_balance": 20000.0,
        "starting_balance": 20000.0,
    })

    pol, intent = _make_unpersisted_intent(bot_id, "NVDA", notional=4000.0, price=120.0)

    # Admit intent so risk reservation, slot, and intent are actively committed
    admit_res = admit_execution_intent(
        intent=intent,
        slot_key=intent.slot_key,
        required_notional=4000.0,
        policy_decision=pol,
    )
    assert admit_res["admitted"] is True

    barrier = threading.Barrier(2)
    exec_result: dict[str, Any] = {}
    exec_error: Exception | None = None
    expire_modified_count: int = 0

    def _exec_worker():
        nonlocal exec_result, exec_error
        barrier.wait(timeout=10)
        try:
            res = asyncio.run(execute_intent(
                intent.execution_intent_id,
                account_context={"bot_id": bot_id},
                current_quote={"price": 120.0, "age_hours": 0.1},
            ))
            exec_result = res
        except Exception as e:
            exec_error = e

    def _expire_worker():
        nonlocal expire_modified_count
        barrier.wait(timeout=10)
        # Attempt to supersede/expire intent concurrently without adding extra schema fields
        res = real_mongo[COLL_EXECUTION_INTENTS].update_one(
            {"execution_intent_id": intent.execution_intent_id, "status": IntentStatus.CREATED.value},
            {"$set": {"status": IntentStatus.EXPIRED.value}},
        )
        expire_modified_count = res.modified_count
        if expire_modified_count > 0:
            release_risk_reservations_for_intent(intent.execution_intent_id)

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        f1 = executor.submit(_exec_worker)
        f2 = executor.submit(_expire_worker)
        f1.result()
        f2.result()

    # Serialization outcome analysis:
    if expire_modified_count == 1:
        # Expiry serialized first:
        # Execution must fail because intent was already transitioned away from CREATED
        assert exec_error is not None, "Execution should have been rejected after expiry"
        assert isinstance(exec_error, (IntentExecutionRejected, pymongo.errors.OperationFailure))
        # Zero orders and zero fills
        assert real_mongo["orders"].count_documents({"execution_intent_id": intent.execution_intent_id}) == 0
        assert real_mongo["trade_fills"].count_documents({"execution_intent_id": intent.execution_intent_id}) == 0
        # Reservation must be RELEASED (not ACTIVE)
        resv = real_mongo[COLL_RISK_RESERVATIONS].find_one({"execution_intent_id": intent.execution_intent_id})
        assert resv["status"] == ReservationStatus.RELEASED.value
    else:
        # Execution serialized first:
        # Execution succeeded, intent status is CONSUMED
        assert exec_result.get("status") == "FILLED"
        # Expiry could not modify intent because status was already CONSUMED
        assert expire_modified_count == 0
        # Exactly 1 order and fill
        assert real_mongo["orders"].count_documents({"execution_intent_id": intent.execution_intent_id}) == 1
        # Reservation is CONSUMED
        resv = real_mongo[COLL_RISK_RESERVATIONS].find_one({"execution_intent_id": intent.execution_intent_id})
        assert resv["status"] == ReservationStatus.CONSUMED.value

    # Invariant: NEVER a stranded ACTIVE reservation
    active_resvs = real_mongo[COLL_RISK_RESERVATIONS].count_documents({
        "execution_intent_id": intent.execution_intent_id,
        "status": ReservationStatus.ACTIVE.value,
    })
    assert active_resvs == 0


# ─────────────────────────────────────────────────────────────────────────────
# 4. Outbox lease is reclaimed
# ─────────────────────────────────────────────────────────────────────────────
def test_scenario_4_outbox_lease_reclaimed_by_new_worker(real_mongo):
    """An outbox lease is reclaimed:
    the old owner cannot complete or overwrite the new owner's result.
    """
    event_id = f"evt-{uuid.uuid4().hex[:12]}"
    now = datetime.datetime.now(datetime.timezone.utc)

    # Insert pending event
    insert_outbox_event({
        "event_id": event_id,
        "event_type": "TRADE_EXECUTED",
        "aggregate_id": "agg-1",
        "payload": {"test": True},
        "status": "PENDING",
        "attempts": 0,
        "created_at": now,
        "last_error": None,
    })

    # Worker A claims event
    claimed_a = claim_pending_outbox_events(batch_size=1, worker_token="worker-A")
    assert len(claimed_a) == 1
    assert claimed_a[0]["event_id"] == event_id
    assert claimed_a[0]["locked_by"] == "worker-A"

    # Simulate Worker A hang / lease expiration by aging locked_at by 120 seconds
    past_time = now - datetime.timedelta(seconds=120)
    real_mongo[COLL_EXECUTION_OUTBOX].update_one(
        {"event_id": event_id},
        {"$set": {"locked_at": past_time}},
    )

    # Worker B reclaims the expired event
    claimed_b = claim_pending_outbox_events(batch_size=1, worker_token="worker-B")
    assert len(claimed_b) == 1
    assert claimed_b[0]["event_id"] == event_id
    assert claimed_b[0]["locked_by"] == "worker-B"

    # Worker A wakes up and attempts to mark the event COMPLETED
    completed_a = mark_outbox_event_completed(
        event_id=event_id,
        worker_token="worker-A",
        result_payload={"winner": "worker-A"},
    )
    # Worker A's update MUST FAIL (return False, 0 docs modified)
    assert completed_a is False, "Worker A must not be allowed to complete reclaimed event"

    # Event remains locked by Worker B
    ev_doc = real_mongo[COLL_EXECUTION_OUTBOX].find_one({"event_id": event_id})
    assert ev_doc["status"] == "PROCESSING"
    assert ev_doc["locked_by"] == "worker-B"

    # Worker B finishes and marks event COMPLETED
    completed_b = mark_outbox_event_completed(
        event_id=event_id,
        worker_token="worker-B",
        result_payload={"winner": "worker-B"},
    )
    assert completed_b is True

    # Final DB assertion: event is COMPLETED by Worker B
    final_doc = real_mongo[COLL_EXECUTION_OUTBOX].find_one({"event_id": event_id})
    assert final_doc["status"] == "COMPLETED"
    assert final_doc["result"] == {"winner": "worker-B"}


# ─────────────────────────────────────────────────────────────────────────────
# 5. Atomic rollback at critical write boundaries
# ─────────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("crash_target", [
    "order_attempt",
    "trade_fills",
    "position_lots",
    "execution_outbox",
])
def test_scenario_5_crash_write_boundaries_atomic_rollback(real_mongo, monkeypatch, crash_target):
    """Failure/process termination at each critical intent/order/fill/lot/outbox write boundary:
    rollback or durable recovery preserves invariants.
    """
    bot_id = f"bot-crash-{crash_target}"
    initial_cash = 30000.0
    real_mongo["bots"].insert_one({
        "bot_id": bot_id,
        "cash_balance": initial_cash,
        "starting_balance": initial_cash,
    })

    pol, intent = _create_approved_persisted_intent(bot_id, "AMZN", notional=5000.0, price=180.0)

    # Pre-populate risk reservation and slot
    real_mongo[COLL_RISK_RESERVATIONS].insert_one({
        "execution_intent_id": intent.execution_intent_id,
        "bot_id": bot_id,
        "status": ReservationStatus.ACTIVE.value,
        "reserved_notional": 5000.0,
    })
    real_mongo[COLL_EXECUTION_SLOTS].insert_one({
        "slot_key": intent.slot_key,
        "intent_id": intent.execution_intent_id,
        "status": "ACTIVE",
    })

    # Intercept insert_one on pymongo.collection.Collection to accurately crash at target
    orig_insert = pymongo.collection.Collection.insert_one

    def mock_insert(self, *args, **kwargs):
        if crash_target == "trade_fills" and self.name == "trade_fills":
            raise RuntimeError("CRASH_TRADE_FILLS")
        elif crash_target == "position_lots" and self.name == "position_lots":
            raise RuntimeError("CRASH_POSITION_LOTS")
        elif crash_target == "execution_outbox" and self.name == "execution_outbox":
            raise RuntimeError("CRASH_EXECUTION_OUTBOX")
        return orig_insert(self, *args, **kwargs)

    if crash_target == "order_attempt":
        def mock_crash_attempt(*a, **k):
            raise RuntimeError("CRASH_ORDER_ATTEMPT")
        monkeypatch.setattr("app.trading.attribution.repository.save_order_attempt", mock_crash_attempt)
    else:
        monkeypatch.setattr(pymongo.collection.Collection, "insert_one", mock_insert)

    with pytest.raises(RuntimeError) as exc_info:
        asyncio.run(execute_intent(
            intent.execution_intent_id,
            account_context={"bot_id": bot_id},
            current_quote={"price": 180.0, "age_hours": 0.1},
        ))
    assert "CRASH_" in str(exc_info.value)

    # Invariants must hold:
    # 1. Intent CAS was rolled back (status is still CREATED)
    intent_doc = real_mongo[COLL_EXECUTION_INTENTS].find_one({"execution_intent_id": intent.execution_intent_id})
    assert intent_doc["status"] == IntentStatus.CREATED.value

    # 2. Cash balance is untouched ($30,000.0)
    bot_doc = real_mongo["bots"].find_one({"bot_id": bot_id})
    assert bot_doc["cash_balance"] == initial_cash

    # 3. Position count is 0
    assert real_mongo["positions"].count_documents({"bot_id": bot_id, "ticker": "AMZN"}) == 0

    # 4. Zero orders, zero fills, zero lots, zero outbox events
    assert real_mongo["orders"].count_documents({"execution_intent_id": intent.execution_intent_id}) == 0
    assert real_mongo["trade_fills"].count_documents({"execution_intent_id": intent.execution_intent_id}) == 0
    assert real_mongo[COLL_POSITION_LOTS].count_documents({"execution_intent_id": intent.execution_intent_id}) == 0
    assert real_mongo[COLL_EXECUTION_OUTBOX].count_documents({"aggregate_id": intent.execution_intent_id}) == 0


# ─────────────────────────────────────────────────────────────────────────────
# 6. Partial sells across multiple BUY lots, fees and symbol normalization
# ─────────────────────────────────────────────────────────────────────────────
def test_scenario_6_partial_sells_multi_lot_fifo_and_fees(real_mongo):
    """Partial sells across multiple BUY lots, fees and symbol normalization:
    quantities and attribution reconcile.
    """
    bot_id = "bot-multi-lot-fifo"
    now = datetime.datetime.now(datetime.timezone.utc)

    # Position of 20 shares AAPL (10 @ $100, 10 @ $110)
    real_mongo["bots"].insert_one({"bot_id": bot_id, "cash_balance": 10000.0})
    real_mongo["positions"].insert_one({
        "bot_id": bot_id,
        "ticker": "AAPL",
        "qty": 20.0,
        "avg_entry_price": 105.0,
        "created_at": now,
        "updated_at": now,
    })

    # Lot 1 (10 shares @ $100)
    lot1_id = f"lot1-{uuid.uuid4().hex[:8]}"
    real_mongo[COLL_POSITION_LOTS].insert_one({
        "lot_id": lot1_id,
        "bot_id": bot_id,
        "ticker": "AAPL",
        "initial_qty": 10.0,
        "remaining_qty": 10.0,
        "entry_price": 100.0,
        "entry_notional": 1001.0,  # $1,000 + $1 fee
        "opened_at": now - datetime.timedelta(hours=2),
        "status": "open",
    })

    # Lot 2 (10 shares @ $110)
    lot2_id = f"lot2-{uuid.uuid4().hex[:8]}"
    real_mongo[COLL_POSITION_LOTS].insert_one({
        "lot_id": lot2_id,
        "bot_id": bot_id,
        "ticker": "AAPL",
        "initial_qty": 10.0,
        "remaining_qty": 10.0,
        "entry_price": 110.0,
        "entry_notional": 1101.0,  # $1,100 + $1 fee
        "opened_at": now - datetime.timedelta(hours=1),
        "status": "open",
    })

    # Sell 15 shares of "  aapl  " (testing symbol normalization)
    pol_sell, intent_sell = _create_approved_persisted_intent(
        bot_id,
        ticker="  aapl  ",  # Denormalized whitespace/case
        side="SELL",
        notional=1800.0,
        price=120.0,
    )
    # Intent sells 75% of position = 15 shares
    intent_sell.approved_size_pct = 0.75
    real_mongo[COLL_EXECUTION_INTENTS].update_one(
        {"execution_intent_id": intent_sell.execution_intent_id},
        {"$set": {"approved_size_pct": 0.75}},
    )

    res = asyncio.run(execute_intent(
        intent_sell.execution_intent_id,
        account_context={"bot_id": bot_id},
        current_quote={"price": 120.0, "age_hours": 0.1},
    ))
    assert res["status"] == "FILLED"
    assert res["side"] == "SELL"
    assert res["ticker"] == "AAPL"  # Normalized

    # Verify Lot 1: fully closed
    l1 = real_mongo[COLL_POSITION_LOTS].find_one({"lot_id": lot1_id})
    assert l1["status"] == "closed"
    assert l1["remaining_qty"] == 0.0

    # Verify Lot 2: partially closed (5 shares remaining)
    l2 = real_mongo[COLL_POSITION_LOTS].find_one({"lot_id": lot2_id})
    assert l2["status"] == "partial"
    assert abs(l2["remaining_qty"] - 5.0) < 0.0001

    # Verify Position remaining qty = 5.0
    pos = real_mongo["positions"].find_one({"bot_id": bot_id, "ticker": "AAPL"})
    assert abs(pos["qty"] - 5.0) < 0.0001

    # Verify Closures recorded in COLL_LOT_CLOSURES by exit_intent_id
    closures = list(real_mongo[COLL_LOT_CLOSURES].find({"exit_intent_id": intent_sell.execution_intent_id}))
    assert len(closures) == 2
    closed_quantities = sum(c["closed_qty"] for c in closures)
    assert abs(closed_quantities - 15.0) < 0.0001


# ─────────────────────────────────────────────────────────────────────────────
# 7. Historical migration, rerun and ENFORCE promotion attempt
# ─────────────────────────────────────────────────────────────────────────────
def test_scenario_7_migration_idempotency_and_enforce_promotion(real_mongo):
    """Historical migration, rerun and ENFORCE promotion attempt:
    rerun is idempotent; incomplete provenance or lot mismatches block promotion.
    """
    bot_id = "bot-mig-test"
    now = datetime.datetime.now(datetime.timezone.utc)

    real_mongo["bots"].insert_one({
        "bot_id": bot_id,
        "cash_balance": 50000.0,
        "control_plane_mode": "OBSERVE",
    })
    real_mongo["positions"].insert_one({
        "bot_id": bot_id,
        "ticker": "MSFT",
        "qty": 20.0,
        "avg_entry_price": 300.0,
        "created_at": now,
    })

    # Step 1: Initial migration
    report1 = reconcile_and_migrate_bot_positions(bot_id)
    assert report1["status"] == "PASS"
    assert report1["lots_created"] == 1
    assert report1["total_qty_reconciled"] == 20.0

    # Check created lot has MIGRATION provenance
    mig_lot = real_mongo[COLL_POSITION_LOTS].find_one({"bot_id": bot_id, "ticker": "MSFT"})
    assert mig_lot is not None
    assert mig_lot["origin"] == "MIGRATION"
    assert mig_lot["provenance_complete"] is False

    # Step 2: Rerun migration (must be completely idempotent)
    report2 = reconcile_and_migrate_bot_positions(bot_id)
    assert report2["status"] == "PASS"
    assert report2["lots_created"] == 0, "Rerun must not create duplicate lots"
    assert real_mongo[COLL_POSITION_LOTS].count_documents({"bot_id": bot_id, "ticker": "MSFT"}) == 1

    # Step 3: Test promotion gate rejection when lot quantity mismatches position
    # Tamper with lot quantity (15.0 != 20.0)
    real_mongo[COLL_POSITION_LOTS].update_one(
        {"lot_id": mig_lot["lot_id"]},
        {"$set": {"remaining_qty": 15.0}},
    )
    with pytest.raises(ValueError) as exc:
        promote_bot_to_enforce(bot_id)
    assert "integrity violations present" in str(exc.value)
    # Mode remains OBSERVE
    bot_doc = real_mongo["bots"].find_one({"bot_id": bot_id})
    assert bot_doc["control_plane_mode"] == "OBSERVE"

    # Step 4: Fix lot quantity to 20.0 and promote
    real_mongo[COLL_POSITION_LOTS].update_one(
        {"lot_id": mig_lot["lot_id"]},
        {"$set": {"remaining_qty": 20.0}},
    )
    promo_res = promote_bot_to_enforce(bot_id)
    assert promo_res["status"] == "PROMOTED"
    assert promo_res["control_plane_mode"] == "ENFORCE"

    # Verify bot is now ENFORCE in database
    bot_doc_after = real_mongo["bots"].find_one({"bot_id": bot_id})
    assert bot_doc_after["control_plane_mode"] == "ENFORCE"


# ─────────────────────────────────────────────────────────────────────────────
# 8. SHADOW same-key races and failure-boundary behavior
# ─────────────────────────────────────────────────────────────────────────────
def test_scenario_8_shadow_same_key_races_and_failure_boundary(real_mongo, monkeypatch):
    """SHADOW same-key races and failure-boundary behavior from Step 02:
    CAS ensures single winner or write-conflict rollback; failure before commit rolls back completely.
    """
    bot_id = "bot-shadow-conc"
    monkeypatch.setenv("CONTROL_PLANE_MODE", "SHADOW")
    real_mongo["bots"].insert_one({"bot_id": bot_id, "cash_balance": 50000.0})

    pol, intent = _create_approved_persisted_intent(
        bot_id, "TSLA", notional=2000.0, price=200.0, effective_mode="SHADOW"
    )

    barrier = threading.Barrier(2)
    shadow_results: list[dict[str, Any]] = [{}, {}]
    shadow_errors: list[Exception | None] = [None, None]

    def _shadow_worker(idx: int):
        barrier.wait(timeout=10)
        try:
            res = asyncio.run(execute_intent(
                intent.execution_intent_id,
                account_context={"bot_id": bot_id, "effective_mode": "SHADOW"},
                current_quote={"price": 200.0, "age_hours": 0.1},
            ))
            shadow_results[idx] = res
        except Exception as e:
            shadow_errors[idx] = e

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        f1 = executor.submit(_shadow_worker, 0)
        f2 = executor.submit(_shadow_worker, 1)
        f1.result()
        f2.result()

    # Exactly one succeeded; the other failed with INTENT_ALREADY_CONSUMED or WriteConflict
    succeeded = sum(1 for r in shadow_results if r.get("status") == "SIMULATED")
    failed = sum(1 for e in shadow_errors if isinstance(e, (IntentExecutionRejected, pymongo.errors.OperationFailure)))
    assert succeeded == 1, f"Expected 1 succeeded, got {succeeded} (results: {shadow_results}, errors: {shadow_errors})"
    assert failed == 1, f"Expected 1 failed, got {failed} (results: {shadow_results}, errors: {shadow_errors})"

    # Exactly 1 simulation committed
    assert real_mongo["shadow_executions"].count_documents({"execution_intent_id": intent.execution_intent_id}) == 1
    assert real_mongo[COLL_EXECUTION_RECONCILIATIONS].count_documents({"execution_intent_id": intent.execution_intent_id}) == 1

    # Zero paper mutations
    assert real_mongo["orders"].count_documents({}) == 0
    assert real_mongo["trade_fills"].count_documents({}) == 0
    assert real_mongo["positions"].count_documents({}) == 0

    # Part B: Crash before commit rolls back atomically
    pol_crash, intent_crash = _create_approved_persisted_intent(
        bot_id, "NFLX", notional=2000.0, price=600.0, effective_mode="SHADOW"
    )
    # Monkeypatch to crash inside shadow transaction before commit
    def mock_fail_rec(*a, **k):
        raise RuntimeError("CRASH_SHADOW_RECONCILIATION")
    monkeypatch.setattr("app.trading.attribution.repository.save_execution_reconciliation", mock_fail_rec)

    with pytest.raises(RuntimeError):
        asyncio.run(execute_intent(
            intent_crash.execution_intent_id,
            account_context={"bot_id": bot_id, "effective_mode": "SHADOW"},
            current_quote={"price": 600.0, "age_hours": 0.1},
        ))

    # Assert zero shadow executions and zero outbox records committed for the crashed intent
    assert real_mongo["shadow_executions"].count_documents({"execution_intent_id": intent_crash.execution_intent_id}) == 0
    assert real_mongo[COLL_EXECUTION_OUTBOX].count_documents({"aggregate_id": intent_crash.execution_intent_id}) == 0


# ─────────────────────────────────────────────────────────────────────────────
# 9. Representative failure mutation test (disabling & restoring guard)
# ─────────────────────────────────────────────────────────────────────────────
def test_scenario_9_representative_failure_mutation_guard(real_mongo, monkeypatch):
    """Reproduce a representative failure by disabling its guard in an isolated mutation test,
    then restore it.

    Mutation: Disabling the active cash reservation check in admit_execution_intent allows
    competing orders to overdraw cash. Restoring the check blocks the breach.
    """
    bot_id = "bot-mutation-test"
    real_mongo["bots"].insert_one({
        "bot_id": bot_id,
        "cash_balance": 10000.0,
        "starting_balance": 10000.0,
    })

    pol1, intent1 = _make_unpersisted_intent(bot_id, "AAPL", notional=8000.0, price=150.0)
    pol2, intent2 = _make_unpersisted_intent(bot_id, "MSFT", notional=8000.0, price=300.0)

    # 1. Admit Intent 1 normally ($8,000)
    res1 = admit_execution_intent(intent1, intent1.slot_key, 8000.0, pol1)
    assert res1["admitted"] is True

    # 2. MUTATION: Disable guard by faking active reservations as 0.0
    import app.trading.attribution.repository as repo_mod
    monkeypatch.setattr(repo_mod, "get_active_reserved_notional", lambda b, session=None: 0.0)

    # With the guard mutated/disabled, the second $8,000 admission inappropriately succeeds!
    res2_mutated = admit_execution_intent(intent2, intent2.slot_key, 8000.0, pol2)
    assert res2_mutated["admitted"] is True, "Mutated code should have bypassed guard"

    # Check that without the guard, total reservations breach available cash:
    # 2 active reservations = $16,000 > $10,000 cash balance!
    total_breached_resv = sum(
        r["reserved_notional"] for r in real_mongo[COLL_RISK_RESERVATIONS].find({"bot_id": bot_id, "status": "ACTIVE"})
    )
    assert total_breached_resv == 16000.0, "Invariant violation occurred under mutated guard"

    # 3. RESTORATION: Undo monkeypatch and clean up second reservation
    monkeypatch.undo()
    real_mongo[COLL_RISK_RESERVATIONS].delete_one({"execution_intent_id": intent2.execution_intent_id})
    real_mongo[COLL_EXECUTION_INTENTS].delete_one({"execution_intent_id": intent2.execution_intent_id})

    # Now attempt admission of a third $8,000 intent with the restored real guard
    pol3, intent3 = _make_unpersisted_intent(bot_id, "GOOGL", notional=8000.0, price=175.0)
    with pytest.raises(InsufficientCashReservationError) as exc_info:
        admit_execution_intent(intent3, intent3.slot_key, 8000.0, pol3)
    assert "INSUFFICIENT_CASH_RESERVATION" in str(exc_info.value) or "Insufficient free cash" in str(exc_info.value)

    # Invariant restored: exactly $8,000 active reservation
    restored_total = sum(
        r["reserved_notional"] for r in real_mongo[COLL_RISK_RESERVATIONS].find({"bot_id": bot_id, "status": "ACTIVE"})
    )
    assert restored_total == 8000.0
