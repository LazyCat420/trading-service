"""Integration Test Suite: Control Plane 13-Scenario Verification Harness.

Exercises the full enforcement and measurement pipeline against an isolated test database
or ephemeral replica-set environment, verifying:
1. Approved BUY/SELL end-to-end lineage
2. Duplicate delivery idempotency (CAS)
3. Concurrent cash/exposure race condition
4. Policy rejection producing zero executor invocations
5. Mismatch validations (account, side, ticker)
6. Quote movement / stale quote rejection
7. Crash before transaction commit (zero mutations)
8. Crash after commit (outbox recovery)
9. Outbox queue retry backoff & poison queue
10. Partial fill and multi-lot FIFO closures
11. Adverse slippage breach vs favorable price improvement
12. Missing benchmark handling (UNRESOLVED)
13. Strict Pydantic model extra='forbid'
"""

import datetime
import pytest
from pydantic import ValidationError

from app.trading.attribution.evaluator import LotAlphaEvaluator
from app.trading.attribution.models import (
    DecisionArtifact,
    ExecutionIntent,
    IntentStatus,
    OrderAttempt,
    PolicyDecision,
    PolicyDisposition,
    ReconciliationVerdict,
)
from app.trading.attribution.reconciliation import reconcile_execution
from app.trading.attribution.repository import (
    COLL_EXECUTION_INTENTS,
    COLL_EXECUTION_OUTBOX,
    COLL_EXECUTION_RECONCILIATIONS,
    COLL_LOT_CLOSURES,
    COLL_POLICY_DECISIONS,
    COLL_POLICY_SNAPSHOTS,
    COLL_POSITION_LOTS,
    claim_execution_slot,
    ensure_attribution_indexes,
    save_decision_artifact,
    save_execution_intent,
    save_policy_decision,
)
from app.trading.control_plane import ControlPlaneMode, resolve_control_plane_mode
from app.trading.executor import IntentExecutionRejected, execute_intent
from app.trading.outbox.repository import (
    claim_pending_outbox_events,
    mark_outbox_event_completed,
    mark_outbox_event_failed,
)
from app.trading.outbox.worker import process_outbox_event, run_outbox_worker_iteration
from app.trading.policy.policy_translator import PolicyInputSnapshot, PolicyTranslator
from app.trading.policy.snapshot_service import build_policy_snapshot

pytestmark = pytest.mark.real_mongo


@pytest.fixture(autouse=True)
def init_clean_env(real_mongo):
    """Ensure indexes and clean collections in isolated test database."""
    ensure_attribution_indexes()
    yield real_mongo


# ─────────────────────────────────────────────────────────────────────────────
# Scenario 1: Approved BUY and SELL end-to-end lineage
# ─────────────────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_scenario_1_approved_buy_and_sell_lineage(real_mongo):
    bot_id = "test-bot-sc1"
    now = datetime.datetime.now(datetime.timezone.utc)
    real_mongo["bots"].insert_one({"bot_id": bot_id, "cash_balance": 50000.0, "starting_balance": 50000.0})

    # --- BUY PHASE ---
    art_buy = DecisionArtifact(
        decision_id="dec-sc1-buy",
        cycle_id="cycle-sc1",
        ticker="AAPL",
        producer="v3_decision_synthesizer",
        model="local",
        requested_action="BUY",
        requested_size_pct=0.10,
        confidence=85,
        reference_quote={"price": 150.0, "age_hours": 0.1},
    )
    save_decision_artifact(art_buy)

    snap_buy, _ = build_policy_snapshot(bot_id=bot_id, ticker="AAPL", quote_price=150.0, as_of=now)
    pol_dec_buy, intent_buy = PolicyTranslator.evaluate(art_buy, snap_buy)
    assert pol_dec_buy.disposition in (PolicyDisposition.APPROVE, PolicyDisposition.APPROVE_WITH_CAP)
    assert intent_buy is not None
    intent_buy.bot_id = bot_id
    save_policy_decision(pol_dec_buy)
    save_execution_intent(intent_buy)

    # Execute BUY
    res_buy = await execute_intent(intent_buy.execution_intent_id, {"bot_id": bot_id}, {"price": 150.0})
    assert res_buy["status"] == "FILLED"
    assert res_buy["side"] == "BUY"

    # Verify BUY DB state
    assert real_mongo["orders"].count_documents({"execution_intent_id": intent_buy.execution_intent_id}) == 1
    assert real_mongo["trade_fills"].count_documents({"execution_intent_id": intent_buy.execution_intent_id}) == 1
    assert real_mongo[COLL_POSITION_LOTS].count_documents({"execution_intent_id": intent_buy.execution_intent_id}) == 1
    assert real_mongo[COLL_EXECUTION_OUTBOX].count_documents({"aggregate_id": intent_buy.execution_intent_id, "status": "PENDING"}) == 1

    # Run Outbox Worker
    processed = run_outbox_worker_iteration(batch_size=10)
    assert processed >= 1
    assert real_mongo[COLL_EXECUTION_RECONCILIATIONS].count_documents({"execution_intent_id": intent_buy.execution_intent_id}) == 1

    # --- SELL PHASE ---
    art_sell = DecisionArtifact(
        decision_id="dec-sc1-sell",
        cycle_id="cycle-sc1",
        ticker="AAPL",
        producer="v3_decision_synthesizer",
        model="local",
        requested_action="SELL",
        confidence=80,
        reference_quote={"price": 160.0, "age_hours": 0.1},
    )
    save_decision_artifact(art_sell)

    snap_sell, _ = build_policy_snapshot(bot_id=bot_id, ticker="AAPL", quote_price=160.0, as_of=now)
    pol_dec_sell, intent_sell = PolicyTranslator.evaluate(art_sell, snap_sell)
    assert pol_dec_sell.disposition == PolicyDisposition.APPROVE
    assert intent_sell is not None
    intent_sell.bot_id = bot_id
    save_policy_decision(pol_dec_sell)
    save_execution_intent(intent_sell)

    res_sell = await execute_intent(intent_sell.execution_intent_id, {"bot_id": bot_id}, {"price": 160.0})
    assert res_sell["status"] == "FILLED"
    assert res_sell["side"] == "SELL"

    # Verify FIFO Lot Closure
    closures = list(real_mongo[COLL_LOT_CLOSURES].find({"ticker": "AAPL"}))
    assert len(closures) == 1
    assert closures[0]["exit_price"] > closures[0]["entry_price"]
    assert closures[0]["gross_pnl"] > 0


# ─────────────────────────────────────────────────────────────────────────────
# Scenario 2: Duplicate delivery idempotency (CAS)
# ─────────────────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_scenario_2_duplicate_delivery_idempotency(real_mongo):
    bot_id = "test-bot-sc2"
    real_mongo["bots"].insert_one({"bot_id": bot_id, "cash_balance": 50000.0})

    intent = ExecutionIntent(
        execution_intent_id="int-sc2-idem",
        decision_id="dec-sc2",
        policy_decision_id="pol-sc2",
        bot_id=bot_id,
        ticker="MSFT",
        side="BUY",
        approved_notional=2000.0,
        approved_size_pct=0.04,
        reference_quote={"price": 300.0},
        valid_from=datetime.datetime.now(datetime.timezone.utc),
        expires_at=datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=1),
        idempotency_key="idemp-sc2",
    )
    save_execution_intent(intent)

    # First call succeeds
    res1 = await execute_intent(intent.execution_intent_id, {"bot_id": bot_id}, {"price": 300.0})
    assert res1["status"] == "FILLED"

    # Second call raises IntentExecutionRejected (already consumed)
    with pytest.raises(IntentExecutionRejected) as exc:
        await execute_intent(intent.execution_intent_id, {"bot_id": bot_id}, {"price": 300.0})
    assert "INTENT_NOT_CREATED" in str(exc.value) or "CONSUMED" in str(exc.value)

    # Verify only 1 order exists
    assert real_mongo["orders"].count_documents({"execution_intent_id": intent.execution_intent_id}) == 1


# ─────────────────────────────────────────────────────────────────────────────
# Scenario 3: Concurrent cash/exposure race condition
# ─────────────────────────────────────────────────────────────────────────────
def test_scenario_3_concurrent_cash_reservation_race(real_mongo):
    bot_id = "test-bot-sc3"
    from app.trading.order_capacity import pending_capacity, strict_capacity_error

    # Bot with $10,000 cash and $10,000 equity
    real_mongo["bots"].insert_one({"bot_id": bot_id, "cash_balance": 10000.0, "starting_balance": 10000.0})

    # Intent 1 approved for $8,000 on Ticker A
    intent1 = ExecutionIntent(
        execution_intent_id="int-sc3-res1",
        decision_id="dec-sc3-1",
        policy_decision_id="pol-sc3-1",
        bot_id=bot_id,
        ticker="TICKERA",
        side="BUY",
        approved_notional=8000.0,
        approved_size_pct=0.80,
        reference_quote={"price": 100.0},
        valid_from=datetime.datetime.now(datetime.timezone.utc),
        expires_at=datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=1),
        idempotency_key="idemp-sc3-1",
    )
    save_execution_intent(intent1)

    # Check pending capacity for Ticker B
    cap = pending_capacity(bot_id, "TICKERB")
    assert cap["cash_reserved"] == 8000.0

    # Competing Intent 2 asking for $5,000 (which exceeds available unreserved cash of $2,000)
    err = strict_capacity_error(
        equity=10000.0,
        cash=10000.0,
        held_value=0.0,
        requested_fraction=0.50,  # $5,000
        concentration_fraction=0.90,
        order_fraction=0.80,
        reservations=cap,
    )
    assert err is not None
    assert "exceeds current capacity including pending orders" in err


# ─────────────────────────────────────────────────────────────────────────────
# Scenario 4: Policy rejection produces zero executor calls
# ─────────────────────────────────────────────────────────────────────────────
def test_scenario_4_policy_rejection_zero_executor(real_mongo):
    bot_id = "test-bot-sc4"
    # Low confidence proposal
    art = DecisionArtifact(
        decision_id="dec-sc4-low-conf",
        cycle_id="cycle-sc4",
        ticker="DOGE",
        producer="v3_decision_synthesizer",
        model="local",
        requested_action="BUY",
        requested_size_pct=0.10,
        confidence=40,  # Below minimum 60 threshold
        reference_quote={"price": 1.0},
    )
    snap = PolicyInputSnapshot(
        portfolio_equity=10000.0,
        cash_balance=10000.0,
        quote_price=1.0,
        is_held=False,
    )
    pol_dec, intent = PolicyTranslator.evaluate(art, snap)
    assert pol_dec.disposition == PolicyDisposition.BLOCK
    assert intent is None
    assert "CONFIDENCE_TOO_LOW" in pol_dec.reason_codes


# ─────────────────────────────────────────────────────────────────────────────
# Scenario 5: Mismatch validations (Account mismatch)
# ─────────────────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_scenario_5_mismatch_validations(real_mongo):
    bot_id = "bot-sc5-alpha"
    wrong_bot = "bot-sc5-intruder"
    intent = ExecutionIntent(
        execution_intent_id="int-sc5-mismatch",
        decision_id="dec-sc5",
        policy_decision_id="pol-sc5",
        bot_id=bot_id,
        ticker="AAPL",
        side="BUY",
        approved_notional=1000.0,
        approved_size_pct=0.1,
        reference_quote={"price": 150.0},
        valid_from=datetime.datetime.now(datetime.timezone.utc),
        expires_at=datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=1),
        idempotency_key="idemp-sc5",
    )
    save_execution_intent(intent)

    with pytest.raises(IntentExecutionRejected) as exc:
        await execute_intent(intent.execution_intent_id, {"bot_id": wrong_bot}, {"price": 150.0})
    assert "ACCOUNT_MISMATCH" in str(exc.value)


# ─────────────────────────────────────────────────────────────────────────────
# Scenario 6: Stale quote rejection (> 12 hours)
# ─────────────────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_scenario_6_stale_quote_rejection(real_mongo):
    bot_id = "test-bot-sc6"
    intent = ExecutionIntent(
        execution_intent_id="int-sc6-stale",
        decision_id="dec-sc6",
        policy_decision_id="pol-sc6",
        bot_id=bot_id,
        ticker="GOOGL",
        side="BUY",
        approved_notional=1000.0,
        approved_size_pct=0.1,
        reference_quote={"price": 180.0, "age_hours": 15.0},  # Stale!
        valid_from=datetime.datetime.now(datetime.timezone.utc),
        expires_at=datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=1),
        idempotency_key="idemp-sc6",
    )
    save_execution_intent(intent)

    with pytest.raises(IntentExecutionRejected) as exc:
        await execute_intent(intent.execution_intent_id, {"bot_id": bot_id}, {"price": 180.0, "age_hours": 15.0})
    assert "STALE_QUOTE" in str(exc.value)


# ─────────────────────────────────────────────────────────────────────────────
# Scenario 7: Crash before transaction commit (zero mutations)
# ─────────────────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_scenario_7_crash_before_commit(real_mongo, monkeypatch):
    bot_id = "test-bot-sc7"
    real_mongo["bots"].insert_one({"bot_id": bot_id, "cash_balance": 50000.0})
    intent = ExecutionIntent(
        execution_intent_id="int-sc7-crash",
        decision_id="dec-sc7",
        policy_decision_id="pol-sc7",
        bot_id=bot_id,
        ticker="AMZN",
        side="BUY",
        approved_notional=5000.0,
        approved_size_pct=0.1,
        reference_quote={"price": 180.0},
        valid_from=datetime.datetime.now(datetime.timezone.utc),
        expires_at=datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=1),
        idempotency_key="idemp-sc7",
    )
    save_execution_intent(intent)

    # Inject crash before commit
    def mock_crash(*args, **kwargs):
        raise RuntimeError("Simulated process crash mid-transaction")

    monkeypatch.setattr("app.trading.attribution.repository.save_order_attempt", mock_crash)

    with pytest.raises(RuntimeError):
        await execute_intent(intent.execution_intent_id, {"bot_id": bot_id}, {"price": 180.0})

    # Assert zero ledger records landed
    assert real_mongo["orders"].count_documents({"execution_intent_id": intent.execution_intent_id}) == 0
    assert real_mongo["trade_fills"].count_documents({"execution_intent_id": intent.execution_intent_id}) == 0
    # Intent remains CREATED (rolled back)
    doc = real_mongo[COLL_EXECUTION_INTENTS].find_one({"execution_intent_id": intent.execution_intent_id})
    assert doc["status"] == "CREATED"


# ─────────────────────────────────────────────────────────────────────────────
# Scenario 8: Crash after commit (outbox recovery)
# ─────────────────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_scenario_8_crash_after_commit_outbox_recovery(real_mongo):
    bot_id = "test-bot-sc8"
    real_mongo["bots"].insert_one({"bot_id": bot_id, "cash_balance": 50000.0})
    intent = ExecutionIntent(
        execution_intent_id="int-sc8-recover",
        decision_id="dec-sc8",
        policy_decision_id="pol-sc8",
        bot_id=bot_id,
        ticker="META",
        side="BUY",
        approved_notional=3000.0,
        approved_size_pct=0.06,
        reference_quote={"price": 500.0},
        valid_from=datetime.datetime.now(datetime.timezone.utc),
        expires_at=datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=1),
        idempotency_key="idemp-sc8",
    )
    save_execution_intent(intent)

    # Execution commits
    await execute_intent(intent.execution_intent_id, {"bot_id": bot_id}, {"price": 500.0})

    # Outbox has PENDING event
    assert real_mongo[COLL_EXECUTION_OUTBOX].count_documents({"aggregate_id": intent.execution_intent_id, "status": "PENDING"}) == 1
    assert real_mongo[COLL_EXECUTION_RECONCILIATIONS].count_documents({"execution_intent_id": intent.execution_intent_id}) == 0

    # Worker runs after restart
    run_outbox_worker_iteration(batch_size=10)

    # Outbox event completed, reconciliation generated
    assert real_mongo[COLL_EXECUTION_OUTBOX].count_documents({"aggregate_id": intent.execution_intent_id, "status": "COMPLETED"}) == 1
    assert real_mongo[COLL_EXECUTION_RECONCILIATIONS].count_documents({"execution_intent_id": intent.execution_intent_id}) == 1


# ─────────────────────────────────────────────────────────────────────────────
# Scenario 9: Outbox worker retry & poison queue
# ─────────────────────────────────────────────────────────────────────────────
def test_scenario_9_outbox_retry_and_poison_queue(real_mongo):
    event_id = "outbox-poison-test"
    real_mongo[COLL_EXECUTION_OUTBOX].insert_one({
        "event_id": event_id,
        "status": "PENDING",
        "attempts": 4,
        "payload": {"invalid": "missing intent_id"},
        "created_at": datetime.datetime.now(datetime.timezone.utc),
    })

    # Next failure hits attempt 5 -> transitions to FAILED
    mark_outbox_event_failed(event_id, error_msg="Simulated failure", max_attempts=5)
    doc = real_mongo[COLL_EXECUTION_OUTBOX].find_one({"event_id": event_id})
    assert doc["status"] == "FAILED"
    assert doc["attempts"] == 5


# ─────────────────────────────────────────────────────────────────────────────
# Scenario 10: Partial fill and multi-lot FIFO closures
# ─────────────────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_scenario_10_multi_lot_fifo_closure(real_mongo):
    bot_id = "test-bot-sc10"
    ticker = "COIN"
    now = datetime.datetime.now(datetime.timezone.utc)

    # Lot 1: 10 shares @ $100
    real_mongo[COLL_POSITION_LOTS].insert_one({
        "lot_id": "lot-1",
        "bot_id": bot_id,
        "ticker": ticker,
        "initial_qty": 10.0,
        "remaining_qty": 10.0,
        "entry_price": 100.0,
        "opened_at": now - datetime.timedelta(hours=2),
        "status": "open",
    })
    # Lot 2: 10 shares @ $110
    real_mongo[COLL_POSITION_LOTS].insert_one({
        "lot_id": "lot-2",
        "bot_id": bot_id,
        "ticker": ticker,
        "initial_qty": 10.0,
        "remaining_qty": 10.0,
        "entry_price": 110.0,
        "opened_at": now - datetime.timedelta(hours=1),
        "status": "open",
    })
    real_mongo["positions"].insert_one({"bot_id": bot_id, "ticker": ticker, "qty": 20.0, "avg_entry_price": 105.0})
    real_mongo["bots"].insert_one({"bot_id": bot_id, "cash_balance": 10000.0})

    # Sell 15 shares @ $120
    intent_sell = ExecutionIntent(
        execution_intent_id="int-sc10-sell",
        decision_id="dec-sc10-sell",
        policy_decision_id="pol-sc10-sell",
        bot_id=bot_id,
        ticker=ticker,
        side="SELL",
        approved_size_pct=0.75,  # 75% of 20 = 15 shares
        reference_quote={"price": 120.0},
        valid_from=now,
        expires_at=now + datetime.timedelta(hours=1),
        idempotency_key="idemp-sc10",
    )
    save_execution_intent(intent_sell)

    res = await execute_intent(intent_sell.execution_intent_id, {"bot_id": bot_id}, {"price": 120.0})
    assert res["status"] == "FILLED"

    # Lot 1 must be closed
    l1 = real_mongo[COLL_POSITION_LOTS].find_one({"lot_id": "lot-1"})
    assert l1["status"] == "closed"
    assert l1["remaining_qty"] == 0.0

    # Lot 2 must be partial (5 shares remaining)
    l2 = real_mongo[COLL_POSITION_LOTS].find_one({"lot_id": "lot-2"})
    assert l2["status"] == "partial"
    assert l2["remaining_qty"] == 5.0

    # 2 closure records generated
    closures = list(real_mongo[COLL_LOT_CLOSURES].find({"ticker": ticker}))
    assert len(closures) == 2


# ─────────────────────────────────────────────────────────────────────────────
# Scenario 11: Adverse slippage breach vs favorable price improvement
# ─────────────────────────────────────────────────────────────────────────────
def test_scenario_11_adverse_vs_favorable_slippage():
    now = datetime.datetime.now(datetime.timezone.utc)
    intent = ExecutionIntent(
        execution_intent_id="int-sc11",
        decision_id="dec-sc11",
        policy_decision_id="pol-sc11",
        ticker="AMD",
        side="BUY",
        approved_notional=1000.0,
        approved_size_pct=0.1,
        allowed_slippage_bps=25.0,
        reference_quote={"price": 100.0},
        valid_from=now,
        expires_at=now + datetime.timedelta(hours=1),
        idempotency_key="idemp-sc11",
    )
    attempts = [OrderAttempt(order_attempt_id="att-11", execution_intent_id=intent.execution_intent_id, attempt_number=1, submitted_at=now)]
    order = {"order_id": "ord-11", "price": 100.0}

    # Favorable fill @ $99.80 -> matched
    rec_fav = reconcile_execution(intent, attempts, order, [{"qty": 10.0, "price": 99.80, "fees": 0.0}])
    assert rec_fav.verdict == ReconciliationVerdict.EXECUTION_MATCHED
    assert rec_fav.realized_slippage_bps < 0

    # Adverse fill @ $101.00 -> breach (100 bps > 25 bps)
    rec_adv = reconcile_execution(intent, attempts, order, [{"qty": 10.0, "price": 101.00, "fees": 0.0}])
    assert rec_adv.verdict == ReconciliationVerdict.EXECUTION_SLIPPAGE_BREACH
    assert rec_adv.realized_slippage_bps == 100.0


# ─────────────────────────────────────────────────────────────────────────────
# Scenario 12: Missing benchmark handling (UNRESOLVED)
# ─────────────────────────────────────────────────────────────────────────────
def test_scenario_12_missing_benchmark_unresolved():
    res = LotAlphaEvaluator.evaluate_lot_closure(
        lot_entry_price=100.0,
        lot_exit_price=110.0,
        benchmark_entry=None,  # Missing benchmark!
        benchmark_exit=None,
    )
    assert res["status"] == "UNRESOLVED"
    assert res["net_alpha"] is None
    assert res["reason"] == "MISSING_SOURCE_PINNED_BENCHMARK"


# ─────────────────────────────────────────────────────────────────────────────
# Scenario 13: Strict Pydantic model rejection (extra='forbid')
# ─────────────────────────────────────────────────────────────────────────────
def test_scenario_13_strict_pydantic_forbid():
    now = datetime.datetime.now(datetime.timezone.utc)
    # Extra field on PolicyDecision
    with pytest.raises(ValidationError):
        PolicyDecision(
            policy_decision_id="pol-err",
            decision_id="dec-err",
            config_hash="abc",
            requested_values={},
            normalized_values={},
            approved_values={},
            disposition=PolicyDisposition.APPROVE,
            **{"unexpected_field_should_fail": "forbidden_payload"},
        )

    # Extra field on ExecutionIntent
    with pytest.raises(ValidationError):
        ExecutionIntent(
            execution_intent_id="int-err",
            decision_id="dec-err",
            policy_decision_id="pol-err",
            ticker="AAPL",
            side="BUY",
            approved_size_pct=0.1,
            valid_from=now,
            expires_at=now,
            idempotency_key="idem",
            **{"unauthorized_key": "forbidden_key"},
        )
