"""Intent-as-Source-of-Truth Paper Executor.

Guarantees that paper trades can ONLY execute against valid, approved, and
unexpired ExecutionIntents. Implements atomic MongoDB multi-document transactions
with CAS intent consumption, tax-lot accounting, and outbox event persistence.
"""

from __future__ import annotations

import datetime
import logging
import uuid
from typing import Any, Optional

from app.db import mongo_query, mongo_store
from app.trading.attribution.models import (
    ExecutionIntent,
    IntentStatus,
    OrderAttempt,
    OrderAttemptStatus,
    PolicyDecision,
    PolicyDisposition,
    ReservationStatus,
)
from app.trading.attribution import repository
from app.trading.attribution.repository import (
    COLL_EXECUTION_INTENTS,
    COLL_EXECUTION_OUTBOX,
    COLL_EXECUTION_SLOTS,
    COLL_LOT_CLOSURES,
    COLL_ORDER_ATTEMPTS,
    COLL_POLICY_DECISIONS,
    COLL_POSITION_LOTS,
    COLL_RISK_RESERVATIONS,
)
from app.trading.control_plane import ControlPlaneMode, resolve_control_plane_mode
from app.trading.paper_trader import _apply_execution_cost, _get_current_price

logger = logging.getLogger(__name__)


def _ensure_utc(dt: Optional[datetime.datetime]) -> Optional[datetime.datetime]:
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=datetime.timezone.utc)
    return dt.astimezone(datetime.timezone.utc)


class IntentExecutionRejected(Exception):
    """Raised when an intent fails invariant verification."""
    def __init__(self, message: str, reason_code: str):
        super().__init__(f"{reason_code}: {message}")
        self.reason_code = reason_code


async def execute_intent(
    intent_id: str,
    account_context: dict[str, Any],
    current_quote: Optional[dict[str, Any]] = None,
    session: Any = None,
) -> dict[str, Any]:
    """Source-of-truth runner: atomically executes an approved ExecutionIntent."""
    bot_id = account_context.get("bot_id", "default")
    mode_arg = account_context.get("effective_mode") or getattr(account_context, "mode", None)

    # 1. Fetch Intent
    docs = mongo_store.find_docs(COLL_EXECUTION_INTENTS, {"execution_intent_id": intent_id}, limit=1)
    if not docs:
        raise IntentExecutionRejected(f"Intent {intent_id} not found", "INTENT_NOT_FOUND")

    intent = ExecutionIntent.model_validate(docs[0])
    now = datetime.datetime.now(datetime.timezone.utc)

    # Resolve and validate effective execution mode:
    # 1. Caller explicit effective_mode
    # 2. Bot-specific or global control plane mode
    # 3. Intent recorded mode if set beyond default OBSERVE
    resolved_mode = mode_arg
    if not resolved_mode:
        bot_mode = resolve_control_plane_mode(bot_id).value
        if bot_mode in (ControlPlaneMode.SHADOW.value, ControlPlaneMode.ENFORCE.value):
            resolved_mode = bot_mode
        elif intent.effective_mode and intent.effective_mode != ControlPlaneMode.OBSERVE.value:
            resolved_mode = intent.effective_mode
        else:
            resolved_mode = bot_mode

    try:
        effective_mode = ControlPlaneMode(resolved_mode)
    except ValueError:
        raise IntentExecutionRejected(f"Invalid execution mode {resolved_mode}", "INVALID_EXECUTION_MODE")

    if effective_mode not in (ControlPlaneMode.SHADOW, ControlPlaneMode.ENFORCE):
        raise IntentExecutionRejected(f"Cannot execute intent in {effective_mode.value} mode", "INVALID_EXECUTION_MODE")

    # 2. Invariant Verifications
    if intent.status != IntentStatus.CREATED:
        raise IntentExecutionRejected(f"Intent status is {intent.status}, expected CREATED", "INTENT_NOT_CREATED")

    exp_intent = _ensure_utc(intent.expires_at)
    if exp_intent and exp_intent < now:
        raise IntentExecutionRejected(f"Intent expired at {intent.expires_at}", "INTENT_EXPIRED")

    if intent.bot_id and intent.bot_id != bot_id:
        raise IntentExecutionRejected(f"Account mismatch: intent bot {intent.bot_id} != context bot {bot_id}", "ACCOUNT_MISMATCH")

    # Verify parent policy decision is approved
    pol_docs = mongo_store.find_docs(COLL_POLICY_DECISIONS, {"policy_decision_id": intent.policy_decision_id}, limit=1)
    if pol_docs:
        pol_dec = PolicyDecision.model_validate(pol_docs[0])
        if pol_dec.disposition not in (PolicyDisposition.APPROVE, PolicyDisposition.APPROVE_WITH_CAP):
            raise IntentExecutionRejected(f"Parent policy disposition is {pol_dec.disposition}", "POLICY_NOT_APPROVED")

    # 3. Resolve Reference Quote
    quote = current_quote or intent.reference_quote or {}
    price = quote.get("price")
    age_hours = quote.get("age_hours", 0.0)
    if not price or price <= 0:
        p_curr, p_age = _get_current_price(intent.ticker)
        price = p_curr
        age_hours = p_age or 0.0

    if not price or price <= 0:
        raise IntentExecutionRejected(f"No price data available for {intent.ticker}", "PRICE_UNAVAILABLE")

    if age_hours > 12.0:
        raise IntentExecutionRejected(f"Quote age {age_hours:.1f}h exceeds threshold (12h)", "STALE_QUOTE")

    ticker = intent.ticker.upper().strip()
    side = intent.side.upper().strip()

    # 4. Quantity and Sizing Resolution
    order_id = f"ord-{uuid.uuid4().hex[:12]}"
    fill_id = f"fill-{uuid.uuid4().hex[:12]}"
    attempt_id = f"att-{uuid.uuid4().hex[:12]}"

    if side == "BUY":
        approved_notional = float(intent.approved_notional or 0.0)
        if approved_notional < 1.0:
            # Fallback to approved_size_pct * cash
            bot_row = mongo_query.find_row("bots", {"bot_id": bot_id}, ["cash_balance", "starting_balance"], session=session)
            cash = float(bot_row[0]) if bot_row and bot_row[0] is not None else 100000.0
            approved_notional = cash * intent.approved_size_pct

        fill_price, cost = _apply_execution_cost(ticker, price, "BUY", approved_notional)
        fees = float(cost.get("commission", 0.0) + cost.get("exchange_fee", 0.0))
        qty = approved_notional / fill_price
        total_spent = approved_notional + fees
    elif side == "SELL":
        pos = mongo_query.find_row("positions", {"bot_id": bot_id, "ticker": ticker}, ["id", "qty", "avg_entry_price"], session=session)
        if not pos or float(pos[1]) <= 0:
            raise IntentExecutionRejected(f"No open position to sell for {ticker}", "NO_OPEN_POSITION")

        held_qty = float(pos[1])
        qty = held_qty if intent.approved_size_pct >= 1.0 else held_qty * intent.approved_size_pct
        notional_approx = qty * price
        fill_price, cost = _apply_execution_cost(ticker, price, "SELL", notional_approx)
        fees = float(cost.get("commission", 0.0) + cost.get("exchange_fee", 0.0))
        total_proceeds = (qty * fill_price) - fees
    else:
        raise IntentExecutionRejected(f"Unsupported side {side}", "INVALID_SIDE")

    # 5. SHADOW Mode Atomic Execution (Simulate execution without mutating operational portfolio)
    if effective_mode == ControlPlaneMode.SHADOW:
        logger.info("[Executor] SHADOW mode active: simulating execution for intent %s", intent_id)
        db = mongo_store.get_doc_db()

        # Fidelity metrics: preserve distinct reference vs realized prices and cost components
        modeled_spread = float(cost.get("spread_bps", 0.0) or 0.0)
        if price > 0:
            realized_slippage = round(abs(fill_price - price) / price * 10000.0, 4)
        else:
            realized_slippage = float(cost.get("impact_bps", 0.0) or 0.0)

        from app.trading.attribution.models import ExecutionReconciliation, ReconciliationVerdict
        allowed_slip = float(intent.allowed_slippage_bps or 25.0)
        if realized_slippage > allowed_slip:
            verdict = ReconciliationVerdict.EXECUTION_SLIPPAGE_BREACH
        else:
            verdict = ReconciliationVerdict.EXECUTION_MATCHED

        rec_id = f"rec-{intent_id}"

        def _shadow_txn_op(s):
            # A. Atomic CAS intent consumption
            if not repository.consume_execution_intent(intent_id, session=s):
                raise IntentExecutionRejected(
                    f"Intent {intent_id} could not be consumed (CAS failed)",
                    "INTENT_ALREADY_CONSUMED",
                )

            # B. Consume any risk reservations for this intent
            repository.consume_risk_reservations_for_intent(intent_id, session=s)

            # C. Insert into shadow_executions with distinct reference and fill prices
            shadow_exec = {
                "execution_intent_id": intent_id,
                "order_id": order_id,
                "bot_id": bot_id,
                "ticker": ticker,
                "side": side,
                "reference_price": price,
                "fill_price": fill_price,
                "qty": qty,
                "fees": fees,
                "modeled_spread_bps": modeled_spread,
                "realized_slippage_bps": realized_slippage,
                "simulated": True,
                "reconciliation_id": rec_id,
                "executed_at": now,
            }
            db["shadow_executions"].insert_one(shadow_exec, session=s)

            # D. Save ExecutionReconciliation with accurate pricing fidelity
            rec = ExecutionReconciliation(
                reconciliation_id=rec_id,
                execution_intent_id=intent_id,
                order_id=order_id,
                fill_ids=[f"sim-fill-{order_id}"],
                intended_qty=qty,
                filled_qty=qty,
                reference_price=price,
                expected_price=price,
                realized_price=fill_price,
                fees=fees,
                modeled_spread_bps=modeled_spread,
                realized_slippage_bps=realized_slippage,
                submission_to_fill_latency_ms=0.0,
                residual_qty=0.0,
                verdict=verdict,
                effective_mode="SHADOW",
                reconciled_at=now,
            )
            repository.save_execution_reconciliation(rec, session=s)

            # E. Emit Transactional Outbox Event for SHADOW execution
            outbox_event = {
                "event_id": f"outbox-{uuid.uuid4().hex[:16]}",
                "event_type": "SHADOW_TRADE_EXECUTED",
                "aggregate_id": intent_id,
                "payload": {
                    "intent_id": intent_id,
                    "decision_id": intent.decision_id,
                    "policy_decision_id": intent.policy_decision_id,
                    "bot_id": bot_id,
                    "ticker": ticker,
                    "side": side,
                    "reference_price": price,
                    "fill_price": fill_price,
                    "fill_qty": qty,
                    "fees": fees,
                    "order_id": order_id,
                    "simulated": True,
                    "reconciliation_id": rec_id,
                    "executed_at": now.isoformat(),
                },
                "status": "PENDING",
                "attempts": 0,
                "created_at": now,
                "last_error": None,
            }
            db[COLL_EXECUTION_OUTBOX].insert_one(outbox_event, session=s)

            # F. Save OrderAttempt record
            attempt = OrderAttempt(
                order_attempt_id=attempt_id,
                execution_intent_id=intent_id,
                attempt_number=1,
                submitted_at=now,
                status=OrderAttemptStatus.ACCEPTED,
                order_id=order_id,
                effective_mode="SHADOW",
            )
            repository.save_order_attempt(attempt, session=s)

        if session is not None:
            _shadow_txn_op(session)
        else:
            with mongo_store.with_txn() as s:
                _shadow_txn_op(s)

        return {
            "status": "SIMULATED",
            "effective_mode": "SHADOW",
            "execution_intent_id": intent_id,
            "order_id": order_id,
            "ticker": ticker,
            "side": side,
            "reference_price": price,
            "fill_price": fill_price,
            "qty": qty,
            "fees": fees,
            "modeled_spread_bps": modeled_spread,
            "realized_slippage_bps": realized_slippage,
            "simulated": True,
            "reconciliation_id": rec_id,
        }

    # 6. Atomic Execution Transaction in MongoDB
    db = mongo_store.get_doc_db()

    def _txn_op(s):
        # A. CAS consume intent
        if not repository.consume_execution_intent(intent_id, session=s):
            raise IntentExecutionRejected(f"Intent {intent_id} could not be consumed (CAS failed)", "INTENT_ALREADY_CONSUMED")

        # Validate reservation validity if present
        resv_doc = db[COLL_RISK_RESERVATIONS].find_one({"execution_intent_id": intent_id}, session=s)
        if resv_doc:
            if resv_doc.get("status") != ReservationStatus.ACTIVE.value:
                raise IntentExecutionRejected(
                    f"Reservation for intent {intent_id} is not active ({resv_doc.get('status')})",
                    "RESERVATION_NOT_ACTIVE",
                )
            if resv_doc.get("expires_at"):
                exp_resv = _ensure_utc(resv_doc["expires_at"])
                if exp_resv and exp_resv < now:
                    raise IntentExecutionRejected(
                        f"Reservation for intent {intent_id} has expired",
                        "RESERVATION_EXPIRED",
                    )

        # Consume any active risk reservations for this intent
        repository.consume_risk_reservations_for_intent(intent_id, session=s)

        # Update slot if bound, validating ownership
        if intent.slot_key:
            existing_slot = db[COLL_EXECUTION_SLOTS].find_one({"slot_key": intent.slot_key}, session=s)
            if existing_slot:
                if existing_slot.get("intent_id") != intent_id or existing_slot.get("status") not in ("ACTIVE", "RESERVED"):
                    raise IntentExecutionRejected(
                        f"Slot {intent.slot_key} not held or not reserved for intent {intent_id}",
                        "SLOT_OWNERSHIP_INVALID",
                    )
                db[COLL_EXECUTION_SLOTS].update_one(
                    {"slot_key": intent.slot_key, "intent_id": intent_id},
                    {"$set": {"status": "CONSUMED", "consumed_at": now}},
                    session=s,
                )

        # B. Mutate Cash & Positions & Tax Lots
        if side == "BUY":
            # Check cash
            bot_row = mongo_query.find_row("bots", {"bot_id": bot_id}, ["cash_balance"], session=s)
            cash_avail = float(bot_row[0]) if bot_row and bot_row[0] is not None else 100000.0
            if cash_avail < total_spent:
                raise IntentExecutionRejected(f"Insufficient cash ${cash_avail:.2f} for order ${total_spent:.2f}", "INSUFFICIENT_CASH")

            # Deduct cash
            db["bots"].update_one(
                {"bot_id": bot_id},
                {"$set": {"cash_balance": cash_avail - total_spent, "updated_at": now}},
                session=s,
            )

            # Insert or update position
            existing_pos = mongo_query.find_row("positions", {"bot_id": bot_id, "ticker": ticker}, ["id", "qty", "avg_entry_price"], session=s)
            if existing_pos:
                old_qty = float(existing_pos[1])
                old_px = float(existing_pos[2])
                new_qty = old_qty + qty
                new_avg_px = ((old_qty * old_px) + (qty * fill_price)) / new_qty
                db["positions"].update_one(
                    {"bot_id": bot_id, "ticker": ticker},
                    {"$set": {"qty": new_qty, "avg_entry_price": new_avg_px, "updated_at": now}},
                    session=s,
                )
            else:
                db["positions"].insert_one(
                    {
                        "bot_id": bot_id,
                        "ticker": ticker,
                        "qty": qty,
                        "avg_entry_price": fill_price,
                        "stop_loss_pct": intent.order_constraints.get("stop_loss_pct") if intent.order_constraints else None,
                        "take_profit_pct": intent.order_constraints.get("take_profit_pct") if intent.order_constraints else None,
                        "exit_style": intent.order_constraints.get("exit_style", "hard_stop") if intent.order_constraints else "hard_stop",
                        "created_at": now,
                        "updated_at": now,
                    },
                    session=s,
                )

            # Insert Tax Lot
            lot_id = f"lot-{uuid.uuid4().hex[:12]}"
            db[COLL_POSITION_LOTS].insert_one(
                {
                    "lot_id": lot_id,
                    "bot_id": bot_id,
                    "ticker": ticker,
                    "initial_qty": qty,
                    "remaining_qty": qty,
                    "entry_price": fill_price,
                    "entry_notional": total_spent,
                    "opened_at": now,
                    "status": "open",
                    "decision_id": intent.decision_id,
                    "execution_intent_id": intent_id,
                },
                session=s,
            )

        elif side == "SELL":
            # Deduct position qty
            existing_pos = mongo_query.find_row("positions", {"bot_id": bot_id, "ticker": ticker}, ["id", "qty", "avg_entry_price"], session=s)
            if not existing_pos:
                raise IntentExecutionRejected(f"No open position to sell for {ticker}", "NO_OPEN_POSITION")
            held_qty = float(existing_pos[1] or 0.0)
            if held_qty < qty:
                raise IntentExecutionRejected(
                    f"Cannot sell {qty} shares of {ticker}; only {held_qty} held",
                    "INSUFFICIENT_POSITION_QTY",
                )

            new_qty = held_qty - qty
            if new_qty <= 0.0001:
                db["positions"].delete_one({"bot_id": bot_id, "ticker": ticker}, session=s)
            else:
                db["positions"].update_one(
                    {"bot_id": bot_id, "ticker": ticker},
                    {"$set": {"qty": new_qty, "updated_at": now}},
                    session=s,
                )

            # Credit Cash
            bot_row = mongo_query.find_row("bots", {"bot_id": bot_id}, ["cash_balance"], session=s)
            cash_avail = float(bot_row[0]) if bot_row and bot_row[0] is not None else 100000.0
            db["bots"].update_one(
                {"bot_id": bot_id},
                {"$set": {"cash_balance": cash_avail + total_proceeds, "updated_at": now}},
                session=s,
            )

            # FIFO Lot Closures
            open_lots = list(
                db[COLL_POSITION_LOTS]
                .find({"bot_id": bot_id, "ticker": ticker, "status": {"$in": ["open", "partial"]}}, session=s)
                .sort("opened_at", 1)
            )
            unclosed = qty
            for lot in open_lots:
                if unclosed <= 0:
                    break
                rem = float(lot["remaining_qty"])
                close_amt = min(rem, unclosed)
                new_rem = rem - close_amt
                lot_status = "closed" if new_rem <= 0.0001 else "partial"

                db[COLL_POSITION_LOTS].update_one(
                    {"lot_id": lot["lot_id"]},
                    {"$set": {"remaining_qty": new_rem, "status": lot_status, "updated_at": now}},
                    session=s,
                )

                gross_pnl = close_amt * (fill_price - float(lot["entry_price"]))
                db[COLL_LOT_CLOSURES].insert_one(
                    {
                        "closure_id": f"close-{uuid.uuid4().hex[:12]}",
                        "lot_id": lot["lot_id"],
                        "bot_id": bot_id,
                        "ticker": ticker,
                        "closed_qty": close_amt,
                        "entry_price": float(lot["entry_price"]),
                        "exit_price": fill_price,
                        "gross_pnl": gross_pnl,
                        "fees": fees * (close_amt / qty),
                        "net_pnl": gross_pnl - (fees * (close_amt / qty)),
                        "closed_at": now,
                        "entry_decision_id": lot.get("decision_id"),
                        "exit_decision_id": intent.decision_id,
                        "entry_intent_id": lot.get("execution_intent_id"),
                        "exit_intent_id": intent_id,
                    },
                    session=s,
                )
                unclosed -= close_amt

            if unclosed > 0.0001:
                raise IntentExecutionRejected(
                    f"Cannot sell {qty} shares of {ticker}: only {qty - unclosed:.4f} shares matched to open lots",
                    "UNMATCHED_LOT_QUANTITY",
                )

        # C. Insert Order & TradeFill
        db["orders"].insert_one(
            {
                "order_id": order_id,
                "bot_id": bot_id,
                "ticker": ticker,
                "side": side,
                "qty": qty,
                "price": fill_price,
                "status": "filled",
                "filled_at": now,
                "execution_intent_id": intent_id,
                "created_at": now,
            },
            session=s,
        )

        db["trade_fills"].insert_one(
            {
                "fill_id": fill_id,
                "order_id": order_id,
                "bot_id": bot_id,
                "ticker": ticker,
                "side": side,
                "qty": qty,
                "price": fill_price,
                "fees": fees,
                "filled_at": now,
                "execution_intent_id": intent_id,
            },
            session=s,
        )

        # D. Insert Transactional Outbox Event
        outbox_event = {
            "event_id": f"outbox-{uuid.uuid4().hex[:16]}",
            "event_type": "TRADE_EXECUTED",
            "aggregate_id": intent_id,
            "payload": {
                "intent_id": intent_id,
                "decision_id": intent.decision_id,
                "policy_decision_id": intent.policy_decision_id,
                "bot_id": bot_id,
                "ticker": ticker,
                "side": side,
                "fill_price": fill_price,
                "fill_qty": qty,
                "reference_price": price,
                "fees": fees,
                "order_id": order_id,
                "executed_at": now.isoformat(),
            },
            "status": "PENDING",
            "attempts": 0,
            "created_at": now,
            "last_error": None,
        }
        db[COLL_EXECUTION_OUTBOX].insert_one(outbox_event, session=s)

        # E. Insert OrderAttempt record
        attempt = OrderAttempt(
            order_attempt_id=attempt_id,
            execution_intent_id=intent_id,
            attempt_number=1,
            submitted_at=now,
            status=OrderAttemptStatus.ACCEPTED,
            order_id=order_id,
            effective_mode=effective_mode.value,
        )
        repository.save_order_attempt(attempt, session=s)

    # Run transaction
    if session is not None:
        _txn_op(session)
    else:
        with mongo_store.with_txn() as s:
            _txn_op(s)

    logger.info(
        "[Executor] Intent %s executed: %s %s %.4f @ $%.4f (order %s)",
        intent_id, side, ticker, qty, fill_price, order_id,
    )

    return {
        "status": "FILLED",
        "order_id": order_id,
        "execution_intent_id": intent_id,
        "ticker": ticker,
        "side": side,
        "fill_price": fill_price,
        "qty": qty,
        "fees": fees,
        "executed_at": now.isoformat(),
    }
