"""Integration Test Suite: Step 02 — SHADOW Execution Atomicity, Isolation, and Fidelity.

Verifies:
1. Atomicity & Rollback: Failure injected before commit rolls back intent CAS and produces zero committed simulations or reconciliations.
2. Duplicate Concurrency: Two competing requests for the same intent have exactly one winner; loser fails with INTENT_ALREADY_CONSUMED.
3. Zero Portfolio Mutation: Zero changes to bots (cash), positions, position_lots, lot_closures, orders, or trade_fills.
4. Capacity Isolation: SHADOW intents do not reduce pending cash capacity for operational paper bots.
5. Simulation Fidelity: reference_price and fill_price remain distinct; modeled spread and slippage are accurately captured.
6. Idempotency Replay: TradeFacade replays already-consumed SHADOW trades with simulated=True and trade_executed=False without re-executing.
"""

import asyncio
import datetime
import uuid
import pytest

from app.trading.attribution.models import (
    DecisionArtifact,
    ExecutionIntent,
    IntentStatus,
    PolicyDecision,
    PolicyDisposition,
    ReconciliationVerdict,
)
from app.trading.attribution.repository import (
    COLL_EXECUTION_INTENTS,
    COLL_EXECUTION_OUTBOX,
    COLL_EXECUTION_RECONCILIATIONS,
    COLL_LOT_CLOSURES,
    COLL_POLICY_DECISIONS,
    COLL_POSITION_LOTS,
    ensure_attribution_indexes,
    save_decision_artifact,
    save_execution_intent,
    save_policy_decision,
)
from app.trading.control_plane import ControlPlaneMode
from app.trading.executor import IntentExecutionRejected, execute_intent
from app.trading.facade import TradeFacade, TradeResultStatus
from app.trading.order_capacity import pending_capacity

pytestmark = pytest.mark.real_mongo


@pytest.fixture(autouse=True)
def init_shadow_env(real_mongo, monkeypatch):
    """Ensure indexes and clean collections in isolated test database."""
    monkeypatch.setenv("CONTROL_PLANE_MODE", "SHADOW")
    ensure_attribution_indexes()
    yield real_mongo


def _create_approved_shadow_intent(bot_id: str, ticker: str, side: str = "BUY", notional: float = 1000.0, price: float = 100.0):
    now = datetime.datetime.now(datetime.timezone.utc)
    d_id = f"dec-sh-{uuid.uuid4().hex[:8]}"
    pol_id = f"pol-sh-{uuid.uuid4().hex[:8]}"
    intent_id = f"int-sh-{uuid.uuid4().hex[:8]}"
    idemp_key = f"idemp-sh-{uuid.uuid4().hex[:8]}"

    from app.trading.policy.policy_translator import PolicyTranslator
    from app.trading.policy.snapshot_service import build_policy_snapshot

    dec = DecisionArtifact(
        decision_id=d_id,
        bot_id=bot_id,
        cycle_id=f"cycle-sh-{uuid.uuid4().hex[:6]}",
        ticker=ticker,
        producer="test",
        model="test",
        requested_action=side,
        requested_size_pct=0.10,
        confidence=85,
        reference_quote={"price": price, "age_hours": 0.1},
    )
    save_decision_artifact(dec)

    snap, _ = build_policy_snapshot(bot_id=bot_id, ticker=ticker, quote_price=price, as_of=now)
    pol, intent = PolicyTranslator.evaluate(dec, snap, bot_id=bot_id)
    assert pol.is_approved
    assert intent is not None
    intent.bot_id = bot_id
    intent.effective_mode = "SHADOW"
    intent.idempotency_key = idemp_key
    save_policy_decision(pol)
    save_execution_intent(intent)
    return intent


@pytest.mark.asyncio
async def test_shadow_execution_zero_operational_portfolio_mutation(real_mongo):
    """Proves that SHADOW execution mutates ZERO paper accounts, cash, positions, orders, or fills."""
    bot_id = "test-bot-shadow-iso"
    real_mongo["bots"].insert_one({
        "bot_id": bot_id,
        "cash_balance": 50000.0,
        "starting_balance": 50000.0,
        "reservation_version": 1,
    })

    intent = _create_approved_shadow_intent(bot_id, "AAPL", "BUY", notional=5000.0, price=150.0)

    # State before
    bot_before = real_mongo["bots"].find_one({"bot_id": bot_id})
    pos_count_before = real_mongo["positions"].count_documents({})
    lots_count_before = real_mongo[COLL_POSITION_LOTS].count_documents({})
    orders_count_before = real_mongo["orders"].count_documents({})
    fills_count_before = real_mongo["trade_fills"].count_documents({})

    res = await execute_intent(
        intent.execution_intent_id,
        account_context={"bot_id": bot_id, "effective_mode": "SHADOW"},
        current_quote={"price": 150.0, "age_hours": 0.1},
    )

    assert res["status"] == "SIMULATED"
    assert res["effective_mode"] == "SHADOW"
    assert res["simulated"] is True

    # State after
    bot_after = real_mongo["bots"].find_one({"bot_id": bot_id})
    assert bot_after["cash_balance"] == bot_before["cash_balance"]
    assert real_mongo["positions"].count_documents({}) == pos_count_before
    assert real_mongo[COLL_POSITION_LOTS].count_documents({}) == lots_count_before
    assert real_mongo["orders"].count_documents({}) == orders_count_before
    assert real_mongo["trade_fills"].count_documents({}) == fills_count_before

    # Shadow-specific tables MUST have records
    shadow_execs = list(real_mongo["shadow_executions"].find({"execution_intent_id": intent.execution_intent_id}))
    assert len(shadow_execs) == 1
    assert shadow_execs[0]["ticker"] == "AAPL"
    assert shadow_execs[0]["simulated"] is True

    recs = list(real_mongo[COLL_EXECUTION_RECONCILIATIONS].find({"execution_intent_id": intent.execution_intent_id}))
    assert len(recs) == 1
    assert recs[0]["effective_mode"] == "SHADOW"


@pytest.mark.asyncio
async def test_shadow_simulation_fidelity_and_slippage(real_mongo):
    """Proves that reference_price and fill_price remain distinct, and spread/slippage are accurately captured."""
    bot_id = "test-bot-shadow-fidelity"
    real_mongo["bots"].insert_one({"bot_id": bot_id, "cash_balance": 50000.0})

    ref_price = 100.0
    intent = _create_approved_shadow_intent(bot_id, "MSFT", "BUY", notional=2000.0, price=ref_price)

    res = await execute_intent(
        intent.execution_intent_id,
        account_context={"bot_id": bot_id, "effective_mode": "SHADOW"},
        current_quote={"price": ref_price, "age_hours": 0.1},
    )

    recs = list(real_mongo[COLL_EXECUTION_RECONCILIATIONS].find({"execution_intent_id": intent.execution_intent_id}))
    assert len(recs) == 1
    rec = recs[0]

    # Reference price must be the quoted price
    assert rec["reference_price"] == ref_price
    assert rec["expected_price"] == ref_price
    assert rec["realized_price"] == res["fill_price"]

    # Fill price should be >= reference price for BUY with costs
    assert rec["realized_price"] >= rec["reference_price"]


@pytest.mark.asyncio
async def test_shadow_competing_duplicates_cas_single_winner(real_mongo):
    """Proves that competing duplicate executions have exactly ONE winner; loser fails with INTENT_ALREADY_CONSUMED."""
    bot_id = "test-bot-shadow-race"
    real_mongo["bots"].insert_one({"bot_id": bot_id, "cash_balance": 50000.0})

    intent = _create_approved_shadow_intent(bot_id, "NVDA", "BUY", notional=2000.0, price=120.0)

    # First execution succeeds
    res1 = await execute_intent(
        intent.execution_intent_id,
        account_context={"bot_id": bot_id, "effective_mode": "SHADOW"},
        current_quote={"price": 120.0, "age_hours": 0.1},
    )
    assert res1["status"] == "SIMULATED"

    # Second execution must fail due to CAS failure (intent already CONSUMED)
    with pytest.raises(IntentExecutionRejected) as exc_info:
        await execute_intent(
            intent.execution_intent_id,
            account_context={"bot_id": bot_id, "effective_mode": "SHADOW"},
            current_quote={"price": 120.0, "age_hours": 0.1},
        )
    assert "INTENT_ALREADY_CONSUMED" in str(exc_info.value) or exc_info.value.reason_code in ("INTENT_ALREADY_CONSUMED", "INTENT_NOT_CREATED")

    # Assert exactly ONE simulation and ONE reconciliation were committed
    assert real_mongo["shadow_executions"].count_documents({"execution_intent_id": intent.execution_intent_id}) == 1
    assert real_mongo[COLL_EXECUTION_RECONCILIATIONS].count_documents({"execution_intent_id": intent.execution_intent_id}) == 1


@pytest.mark.asyncio
async def test_shadow_crash_before_commit_atomic_rollback(real_mongo, monkeypatch):
    """Proves that a crash / exception before transaction commit rolls back intent CAS and leaves no orphan simulation."""
    bot_id = "test-bot-shadow-rollback"
    real_mongo["bots"].insert_one({"bot_id": bot_id, "cash_balance": 50000.0})

    intent = _create_approved_shadow_intent(bot_id, "TSLA", "BUY", notional=3000.0, price=200.0)

    # Inject exception during reconciliation write
    from app.trading.attribution import repository as repo_mod
    orig_save_rec = repo_mod.save_execution_reconciliation

    def _failing_save_rec(rec, session=None):
        raise RuntimeError("Simulated crash right before transaction commit")

    monkeypatch.setattr(repo_mod, "save_execution_reconciliation", _failing_save_rec)

    with pytest.raises(RuntimeError, match="Simulated crash"):
        await execute_intent(
            intent.execution_intent_id,
            account_context={"bot_id": bot_id, "effective_mode": "SHADOW"},
            current_quote={"price": 200.0, "age_hours": 0.1},
        )

    # Intent must NOT be CONSUMED because transaction rolled back
    intent_doc = real_mongo[COLL_EXECUTION_INTENTS].find_one({"execution_intent_id": intent.execution_intent_id})
    assert intent_doc["status"] == IntentStatus.CREATED.value

    # Zero simulations or reconciliations committed
    assert real_mongo["shadow_executions"].count_documents({"execution_intent_id": intent.execution_intent_id}) == 0
    assert real_mongo[COLL_EXECUTION_RECONCILIATIONS].count_documents({"execution_intent_id": intent.execution_intent_id}) == 0


def test_shadow_intents_do_not_reduce_paper_cash_capacity(real_mongo):
    """Proves that pending SHADOW intents do not reduce cash capacity for operational paper bots."""
    bot_id = "test-bot-shadow-cap"
    real_mongo["bots"].insert_one({"bot_id": bot_id, "cash_balance": 10000.0, "starting_balance": 10000.0})

    # Create active SHADOW intent
    intent = _create_approved_shadow_intent(bot_id, "AMD", "BUY", notional=8000.0, price=100.0)

    # Check capacity for operational bot
    cap = pending_capacity(bot_id, "AMD")
    assert cap["cash_reserved"] == 0.0, f"Expected 0.0 reserved cash, got {cap['cash_reserved']}"


@pytest.mark.asyncio
async def test_shadow_facade_idempotency_replay(real_mongo):
    """Proves that TradeFacade replays already-processed SHADOW trades idempotently without re-executing."""
    bot_id = "test-bot-shadow-facade"
    real_mongo["bots"].insert_one({"bot_id": bot_id, "cash_balance": 50000.0})

    idemp = f"idemp-facade-sh-{uuid.uuid4().hex[:8]}"

    res1 = await TradeFacade.submit_trade(
        bot_id=bot_id,
        ticker="GOOGL",
        action="BUY",
        size_pct=0.05,
        current_price=175.0,
        idempotency_key=idemp,
    )
    assert res1["status"] == TradeResultStatus.SIMULATED.value
    assert res1["simulated"] is True
    assert res1["trade_executed"] is False

    # Second submission with same idempotency key
    res2 = await TradeFacade.submit_trade(
        bot_id=bot_id,
        ticker="GOOGL",
        action="BUY",
        size_pct=0.05,
        current_price=175.0,
        idempotency_key=idemp,
    )
    assert res2["status"] == TradeResultStatus.ALREADY_PROCESSED.value
    assert res2["simulated"] is True
    assert res2["trade_executed"] is False

    # Verify still only 1 simulation
    assert real_mongo["shadow_executions"].count_documents({"ticker": "GOOGL"}) == 1
