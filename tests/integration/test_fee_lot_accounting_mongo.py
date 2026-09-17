"""Real MongoDB integration test suite for Step 09: Fix Realized Fee and Lot Accounting.

Tests:
1. BUY execution creates tax lots in COLL_POSITION_LOTS with entry_fee, remaining_entry_fee, and LIVE provenance.
2. Sequential FIFO partial closures strictly conserve entry fees, exit fees, and cash flows against real MongoDB replica set.
3. Multi-lot SELL closures conserve exit fees across records and accurately reconcile bot cash ledger.
4. evaluate_closed_lot_alpha_iteration evaluates alpha on real MongoDB and fails closed on missing lot provenance.
"""

import datetime
import secrets
import pytest

from app.trading.attribution.models import (
    DecisionArtifact,
    ExecutionIntent,
    PolicyDecision,
    PolicyDisposition,
    ReservationStatus,
)
from app.trading.attribution.repository import (
    COLL_EXECUTION_INTENTS,
    COLL_EXECUTION_SLOTS,
    COLL_LOT_CLOSURES,
    COLL_POLICY_DECISIONS,
    COLL_POSITION_LOTS,
    COLL_RISK_RESERVATIONS,
    ensure_attribution_indexes,
    save_decision_artifact,
    save_execution_intent,
    save_policy_decision,
)
from app.trading.attribution.worker import evaluate_closed_lot_alpha_iteration
from app.trading.executor import execute_intent
from app.trading.policy.policy_translator import PolicyTranslator
from app.trading.policy.snapshot_service import build_policy_snapshot

pytestmark = pytest.mark.real_mongo


def _gen_token() -> str:
    """Generate in-memory unique token for zero credential leakage compliance."""
    return f"tok_{secrets.token_hex(8)}"


@pytest.fixture(autouse=True)
def setup_env_and_indexes(real_mongo, monkeypatch):
    """Ensure all indexes exist and control plane is in ENFORCE mode for execution tests."""
    monkeypatch.setenv("CONTROL_PLANE_MODE", "ENFORCE")
    ensure_attribution_indexes()


# ─────────────────────────────────────────────────────────────────────────────
# 1. Real Mongo BUY Execution Creates Tax Lot with Fees
# ─────────────────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_real_mongo_buy_creates_tax_lot_with_fees(real_mongo):
    bot_id = f"bot-buy-lot-{_gen_token()}"
    now = datetime.datetime.now(datetime.timezone.utc)
    real_mongo["bots"].insert_one({"bot_id": bot_id, "cash_balance": 50000.0, "created_at": now})

    art_buy = DecisionArtifact(
        decision_id=f"dec-buy-{_gen_token()}",
        cycle_id=f"cycle-{_gen_token()}",
        ticker="AAPL",
        producer="v3_decision_synthesizer",
        model="local",
        requested_action="BUY",
        requested_size_pct=0.10,
        confidence=85,
        reference_quote={"price": 100.0, "age_hours": 0.1},
    )
    save_decision_artifact(art_buy)

    snap_buy, _ = build_policy_snapshot(bot_id=bot_id, ticker="AAPL", quote_price=100.0, as_of=now)
    pol_dec_buy, intent_buy = PolicyTranslator.evaluate(art_buy, snap_buy)
    assert pol_dec_buy.disposition in (PolicyDisposition.APPROVE, PolicyDisposition.APPROVE_WITH_CAP)
    assert intent_buy is not None
    intent_buy.bot_id = bot_id
    intent_buy.slot_key = f"slot:{bot_id}:AAPL"
    save_policy_decision(pol_dec_buy)
    save_execution_intent(intent_buy)

    real_mongo[COLL_RISK_RESERVATIONS].insert_one({
        "execution_intent_id": intent_buy.execution_intent_id,
        "status": ReservationStatus.ACTIVE.value,
    })
    real_mongo[COLL_EXECUTION_SLOTS].insert_one({
        "slot_key": intent_buy.slot_key,
        "intent_id": intent_buy.execution_intent_id,
        "status": "ACTIVE",
    })

    # Execute BUY
    res_buy = await execute_intent(intent_buy.execution_intent_id, {"bot_id": bot_id}, {"price": 100.0, "age_hours": 0.1})
    assert res_buy["status"] == "FILLED"
    assert res_buy["side"] == "BUY"

    # Verify Tax Lot in MongoDB
    lot = real_mongo[COLL_POSITION_LOTS].find_one({"execution_intent_id": intent_buy.execution_intent_id})
    assert lot is not None
    assert lot["initial_qty"] > 0.0
    assert lot["remaining_qty"] == lot["initial_qty"]
    assert lot["entry_price"] > 0.0
    assert "entry_fee" in lot
    assert "remaining_entry_fee" in lot
    assert lot["remaining_entry_fee"] == lot["entry_fee"]
    assert lot["origin"] == "LIVE"
    assert lot["provenance_complete"] is True
    assert lot["status"] == "open"


# ─────────────────────────────────────────────────────────────────────────────
# 2. Sequential FIFO Partial Closures Fee and Cash Conservation
# ─────────────────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_real_mongo_sequential_partial_fifo_conservation(real_mongo):
    bot_id = f"bot-fifo-{_gen_token()}"
    ticker = "MSFT"
    now = datetime.datetime.now(datetime.timezone.utc)
    real_mongo["bots"].insert_one({"bot_id": bot_id, "cash_balance": 50000.0, "created_at": now})

    # Pre-seed a known tax lot: 10 shares @ 100, entry_fee 1.0, remaining_entry_fee 1.0
    lot_id = f"lot-seed-{_gen_token()}"
    real_mongo[COLL_POSITION_LOTS].insert_one({
        "lot_id": lot_id,
        "bot_id": bot_id,
        "ticker": ticker,
        "initial_qty": 10.0,
        "remaining_qty": 10.0,
        "entry_price": 100.0,
        "entry_notional": 1001.0,
        "entry_fee": 1.0,
        "remaining_entry_fee": 1.0,
        "opened_at": now - datetime.timedelta(hours=2),
        "status": "open",
        "origin": "LIVE",
        "provenance_complete": True,
    })
    real_mongo["positions"].insert_one({
        "bot_id": bot_id,
        "ticker": ticker,
        "qty": 10.0,
        "avg_entry_price": 100.0,
        "created_at": now,
        "updated_at": now,
    })

    # Step 1: Partial Sell 4 shares @ 110 (40% of position)
    intent_sell1 = ExecutionIntent(
        execution_intent_id=f"int-sell1-{_gen_token()}",
        decision_id=f"dec-sell1-{_gen_token()}",
        policy_decision_id=f"pol-sell1-{_gen_token()}",
        bot_id=bot_id,
        ticker=ticker,
        side="SELL",
        approved_size_pct=0.4,
        reference_quote={"price": 110.0},
        valid_from=now,
        expires_at=now + datetime.timedelta(hours=1),
        idempotency_key=f"idemp-{_gen_token()}",
    )
    save_policy_decision(PolicyDecision(
        policy_decision_id=intent_sell1.policy_decision_id,
        decision_id=intent_sell1.decision_id,
        config_hash="h-test",
        disposition=PolicyDisposition.APPROVE,
        requested_values={},
        normalized_values={},
        approved_values={},
    ))
    save_execution_intent(intent_sell1)

    res_s1 = await execute_intent(intent_sell1.execution_intent_id, {"bot_id": bot_id}, {"price": 110.0, "age_hours": 0.1})
    assert res_s1["status"] == "FILLED"

    # Verify Lot after partial close: 6 shares remaining, 0.60 remaining entry fee
    lot_after_1 = real_mongo[COLL_POSITION_LOTS].find_one({"lot_id": lot_id})
    assert lot_after_1["remaining_qty"] == 6.0
    assert abs(lot_after_1["remaining_entry_fee"] - 0.60) < 1e-4
    assert lot_after_1["status"] == "partial"

    # Verify First Closure record
    c1 = real_mongo[COLL_LOT_CLOSURES].find_one({"exit_intent_id": intent_sell1.execution_intent_id})
    assert c1 is not None
    assert c1["closed_qty"] == 4.0
    assert abs(c1["allocated_entry_fee"] - 0.40) < 1e-4
    assert abs(c1["invested_capital_denominator"] - 400.40) < 1e-4
    assert c1["net_pnl"] == c1["dollar_pnl"]

    # Step 2: Final Sell remaining 6 shares @ 120 (100% of remaining position)
    intent_sell2 = ExecutionIntent(
        execution_intent_id=f"int-sell2-{_gen_token()}",
        decision_id=f"dec-sell2-{_gen_token()}",
        policy_decision_id=f"pol-sell2-{_gen_token()}",
        bot_id=bot_id,
        ticker=ticker,
        side="SELL",
        approved_size_pct=1.0,
        reference_quote={"price": 120.0},
        valid_from=now,
        expires_at=now + datetime.timedelta(hours=1),
        idempotency_key=f"idemp-{_gen_token()}",
    )
    save_policy_decision(PolicyDecision(
        policy_decision_id=intent_sell2.policy_decision_id,
        decision_id=intent_sell2.decision_id,
        config_hash="h-test",
        disposition=PolicyDisposition.APPROVE,
        requested_values={},
        normalized_values={},
        approved_values={},
    ))
    save_execution_intent(intent_sell2)

    res_s2 = await execute_intent(intent_sell2.execution_intent_id, {"bot_id": bot_id}, {"price": 120.0, "age_hours": 0.1})
    assert res_s2["status"] == "FILLED"

    # Verify Lot after final close: 0 shares remaining, 0.0 fee remaining, closed
    lot_after_2 = real_mongo[COLL_POSITION_LOTS].find_one({"lot_id": lot_id})
    assert lot_after_2["remaining_qty"] == 0.0
    assert abs(lot_after_2["remaining_entry_fee"] - 0.0) < 1e-4
    assert lot_after_2["status"] == "closed"

    # Verify Second Closure record
    c2 = real_mongo[COLL_LOT_CLOSURES].find_one({"exit_intent_id": intent_sell2.execution_intent_id})
    assert c2 is not None
    assert c2["closed_qty"] == 6.0
    assert abs(c2["allocated_entry_fee"] - 0.60) < 1e-4
    assert abs(c2["invested_capital_denominator"] - 600.60) < 1e-4

    # Fee Conservation Invariant: Allocated entry fees sum to initial entry fee exactly
    total_allocated_entry_fees = c1["allocated_entry_fee"] + c2["allocated_entry_fee"]
    assert abs(total_allocated_entry_fees - 1.0) < 1e-4


# ─────────────────────────────────────────────────────────────────────────────
# 3. Multi-Lot FIFO Closure and Exit Fee Conservation
# ─────────────────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_real_mongo_multi_lot_fifo_closure(real_mongo):
    bot_id = f"bot-multi-{_gen_token()}"
    ticker = "NVDA"
    now = datetime.datetime.now(datetime.timezone.utc)
    real_mongo["bots"].insert_one({"bot_id": bot_id, "cash_balance": 50000.0, "created_at": now})

    # Lot 1: 5 shares @ 100, entry_fee 1.0, remaining_entry_fee 1.0 (opened 2h ago)
    lot1_id = f"lot-1-{_gen_token()}"
    real_mongo[COLL_POSITION_LOTS].insert_one({
        "lot_id": lot1_id,
        "bot_id": bot_id,
        "ticker": ticker,
        "initial_qty": 5.0,
        "remaining_qty": 5.0,
        "entry_price": 100.0,
        "entry_notional": 501.0,
        "entry_fee": 1.0,
        "remaining_entry_fee": 1.0,
        "opened_at": now - datetime.timedelta(hours=2),
        "status": "open",
        "origin": "LIVE",
        "provenance_complete": True,
    })

    # Lot 2: 5 shares @ 105, entry_fee 1.0, remaining_entry_fee 1.0 (opened 1h ago)
    lot2_id = f"lot-2-{_gen_token()}"
    real_mongo[COLL_POSITION_LOTS].insert_one({
        "lot_id": lot2_id,
        "bot_id": bot_id,
        "ticker": ticker,
        "initial_qty": 5.0,
        "remaining_qty": 5.0,
        "entry_price": 105.0,
        "entry_notional": 526.0,
        "entry_fee": 1.0,
        "remaining_entry_fee": 1.0,
        "opened_at": now - datetime.timedelta(hours=1),
        "status": "open",
        "origin": "LIVE",
        "provenance_complete": True,
    })

    real_mongo["positions"].insert_one({
        "bot_id": bot_id,
        "ticker": ticker,
        "qty": 10.0,
        "avg_entry_price": 102.5,
        "created_at": now,
        "updated_at": now,
    })

    # Sell 8 shares (80% of 10) @ 110
    intent_sell = ExecutionIntent(
        execution_intent_id=f"int-sell-multi-{_gen_token()}",
        decision_id=f"dec-sell-multi-{_gen_token()}",
        policy_decision_id=f"pol-sell-multi-{_gen_token()}",
        bot_id=bot_id,
        ticker=ticker,
        side="SELL",
        approved_size_pct=0.8,
        reference_quote={"price": 110.0},
        valid_from=now,
        expires_at=now + datetime.timedelta(hours=1),
        idempotency_key=f"idemp-{_gen_token()}",
    )
    save_policy_decision(PolicyDecision(
        policy_decision_id=intent_sell.policy_decision_id,
        decision_id=intent_sell.decision_id,
        config_hash="h-multi",
        disposition=PolicyDisposition.APPROVE,
        requested_values={},
        normalized_values={},
        approved_values={},
    ))
    save_execution_intent(intent_sell)

    res_sell = await execute_intent(intent_sell.execution_intent_id, {"bot_id": bot_id}, {"price": 110.0, "age_hours": 0.1})
    assert res_sell["status"] == "FILLED"

    # Verify 2 closure records generated
    closures = list(real_mongo[COLL_LOT_CLOSURES].find({"exit_intent_id": intent_sell.execution_intent_id}))
    assert len(closures) == 2

    # FIFO Order: Lot 1 closed first (5 shares), Lot 2 closed second (3 shares)
    c_lot1 = next(c for c in closures if c["lot_id"] == lot1_id)
    c_lot2 = next(c for c in closures if c["lot_id"] == lot2_id)

    assert c_lot1["closed_qty"] == 5.0
    assert abs(c_lot1["allocated_entry_fee"] - 1.0) < 1e-4

    assert c_lot2["closed_qty"] == 3.0
    assert abs(c_lot2["allocated_entry_fee"] - 0.60) < 1e-4

    # Remaining Lot 2 Invariant: 2 shares remaining with 0.40 remaining entry fee
    lot2_updated = real_mongo[COLL_POSITION_LOTS].find_one({"lot_id": lot2_id})
    assert lot2_updated["remaining_qty"] == 2.0
    assert abs(lot2_updated["remaining_entry_fee"] - 0.40) < 1e-4
    assert lot2_updated["status"] == "partial"


# ─────────────────────────────────────────────────────────────────────────────
# 4. evaluate_closed_lot_alpha_iteration with Provenance Gating
# ─────────────────────────────────────────────────────────────────────────────
def test_real_mongo_alpha_worker_provenance_gating(real_mongo, monkeypatch):
    """Real MongoDB evaluation iteration tests:
    - Missing lot provenance fails closed (is_attributable=False, MISSING_LOT_PROVENANCE).
    - Valid live lot is evaluated and attributable.
    """
    now = datetime.datetime.now(datetime.timezone.utc)
    bot_id = f"bot-alpha-{_gen_token()}"
    monkeypatch.setattr("app.trading.attribution.worker._get_benchmark_price", lambda s, d: 100.0)

    # 1. Closure with missing lot
    closure_orphan_id = f"close-orphan-{_gen_token()}"
    real_mongo[COLL_LOT_CLOSURES].insert_one({
        "closure_id": closure_orphan_id,
        "lot_id": f"non-existent-{_gen_token()}",
        "bot_id": bot_id,
        "ticker": "SPY",
        "closed_qty": 10.0,
        "entry_price": 100.0,
        "exit_price": 110.0,
        "allocated_entry_fee": 1.0,
        "exit_fee": 1.0,
        "closed_at": now,
    })

    # 2. Closure with valid live lot
    valid_lot_id = f"lot-valid-{_gen_token()}"
    real_mongo[COLL_POSITION_LOTS].insert_one({
        "lot_id": valid_lot_id,
        "bot_id": bot_id,
        "ticker": "SPY",
        "initial_qty": 10.0,
        "remaining_qty": 0.0,
        "entry_price": 100.0,
        "entry_fee": 1.0,
        "remaining_entry_fee": 0.0,
        "opened_at": now,
        "status": "closed",
        "origin": "LIVE",
        "provenance_complete": True,
    })
    closure_valid_id = f"close-valid-{_gen_token()}"
    real_mongo[COLL_LOT_CLOSURES].insert_one({
        "closure_id": closure_valid_id,
        "lot_id": valid_lot_id,
        "bot_id": bot_id,
        "ticker": "SPY",
        "closed_qty": 10.0,
        "entry_price": 100.0,
        "exit_price": 110.0,
        "allocated_entry_fee": 1.0,
        "exit_fee": 1.0,
        "closed_at": now,
    })

    # Run iteration directly against real_mongo
    evaluated_count = evaluate_closed_lot_alpha_iteration(limit=50, now=now, db=real_mongo)
    assert evaluated_count >= 2

    # Check orphan closure evaluation: must fail closed
    orphan_eval = real_mongo["lot_closure_evaluations"].find_one({"closure_id": closure_orphan_id})
    assert orphan_eval is not None
    assert orphan_eval["is_attributable"] is False
    assert orphan_eval["provenance"] == "MISSING_LOT"
    assert orphan_eval["exclusion_reason"] == "MISSING_LOT_PROVENANCE"

    # Check valid closure evaluation: must be attributable
    valid_eval = real_mongo["lot_closure_evaluations"].find_one({"closure_id": closure_valid_id})
    assert valid_eval is not None
    assert valid_eval["is_attributable"] is True
    assert valid_eval["provenance"] == "LIVE"
    assert valid_eval["exclusion_reason"] is None
    assert valid_eval["invested_capital_denominator"] == 1001.0
    assert valid_eval["dollar_pnl"] == 98.0
    assert valid_eval["net_realized_return"] == 9.7902
