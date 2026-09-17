"""Unit tests for Step 04: Verify and Strengthen the Runtime Execution Boundary.

Verifies:
1. Direct, dynamic, and aliased calls to paper_trader.buy/sell are rejected with DirectTraderCallRestricted.
2. Forged called_via_facade=True boolean flag cannot bypass the execution boundary without FacadeExecutionAuthority.
3. Absent parent policy decision strictly fails closed (PARENT_POLICY_MISSING) unless explicitly marked emergency exit.
4. Malformed parent policy decision strictly fails closed (MALFORMED_PARENT_POLICY).
5. Account mismatch between intent and execution context strictly fails closed (ACCOUNT_MISMATCH).
6. Missing or unknown quote age (None) strictly fails closed (UNKNOWN_QUOTE_AGE).
7. Mode downgrade attempts (e.g. account in ENFORCE, caller requests SHADOW/OBSERVE) strictly fail closed (MODE_DOWNGRADE_REJECTED).
8. Missing required risk reservation for BUY in ENFORCE mode strictly fails closed (MISSING_REQUIRED_RESERVATION).
9. Missing required execution slot in ENFORCE mode strictly fails closed (MISSING_REQUIRED_SLOT).
10. Legitimate TradeFacade paths and protective exits continue to succeed with valid authority.
"""

import datetime
import uuid
import pytest
from unittest.mock import AsyncMock, patch, MagicMock

from app.trading.attribution.models import (
    DecisionArtifact,
    ExecutionIntent,
    IntentStatus,
    PolicyDecision,
    PolicyDisposition,
    ReservationStatus,
)
from app.trading.authority import (
    DirectTraderCallRestricted,
    FacadeExecutionAuthority,
    mint_facade_authority,
)
from app.trading.control_plane import ControlPlaneMode
from app.trading.executor import IntentExecutionRejected, execute_intent
from app.trading.facade import TradeFacade, TradeResultStatus
from app.trading.paper_trader import buy, sell
from app.trading.policy.policy_translator import PolicyInputSnapshot


def _make_test_intent(**kwargs) -> ExecutionIntent:
    now = datetime.datetime.now(datetime.timezone.utc)
    defaults = {
        "execution_intent_id": f"intent-{uuid.uuid4().hex[:8]}",
        "decision_id": "dec-1",
        "policy_decision_id": "policy-1",
        "ticker": "AAPL",
        "side": "BUY",
        "approved_notional": 1000.0,
        "approved_size_pct": 0.05,
        "bot_id": "bot-test",
        "effective_mode": "SHADOW",
        "valid_from": now,
        "expires_at": now + datetime.timedelta(hours=1),
        "idempotency_key": f"idem-{uuid.uuid4().hex[:8]}",
    }
    defaults.update(kwargs)
    return ExecutionIntent(**defaults)


def _make_test_policy(**kwargs) -> PolicyDecision:
    defaults = {
        "policy_decision_id": "policy-1",
        "decision_id": "dec-1",
        "config_hash": "h-1",
        "requested_values": {},
        "normalized_values": {},
        "approved_values": {"target_allocation_pct": 0.05, "approved_notional": 1000.0},
        "disposition": PolicyDisposition.APPROVE,
    }
    defaults.update(kwargs)
    return PolicyDecision(**defaults)


@pytest.mark.asyncio
async def test_direct_and_forged_calls_to_paper_trader_are_rejected():
    """Calling buy or sell directly or with forged boolean flags must be rejected."""
    # 1. Plain direct call
    with pytest.raises(DirectTraderCallRestricted):
        await buy(bot_id="bot-test", ticker="AAPL", size_pct=0.10)

    with pytest.raises(DirectTraderCallRestricted):
        await sell(bot_id="bot-test", ticker="AAPL")

    # 2. Forged called_via_facade=True without capability object
    with pytest.raises(DirectTraderCallRestricted):
        await buy(bot_id="bot-test", ticker="AAPL", size_pct=0.10, called_via_facade=True)

    with pytest.raises(DirectTraderCallRestricted):
        await sell(bot_id="bot-test", ticker="AAPL", called_via_facade=True)

    # 3. Dynamic getattr call
    import app.trading.paper_trader as pt
    dynamic_buy = getattr(pt, "buy")
    with pytest.raises(DirectTraderCallRestricted):
        await dynamic_buy(bot_id="bot-test", ticker="AAPL", size_pct=0.10, called_via_facade=True)

    # 4. Capability for different action / ticker rejected
    mismatched_auth = mint_facade_authority(bot_id="bot-test", ticker="MSFT", action="BUY")
    with pytest.raises(DirectTraderCallRestricted):
        await buy(bot_id="bot-test", ticker="AAPL", size_pct=0.10, execution_authority=mismatched_auth)


@pytest.mark.asyncio
async def test_absent_parent_policy_decision_fails_closed(monkeypatch):
    """Missing parent policy decision fails closed unless explicitly marked emergency exit."""
    fake_intent = _make_test_intent(
        execution_intent_id="intent-no-parent-1",
        policy_decision_id="non-existent-policy-id",
    )

    monkeypatch.setattr(
        "app.db.mongo_store.find_docs",
        lambda coll, q, **kw: [fake_intent.model_dump(mode="python")] if coll == "execution_intents" else [],
    )
    monkeypatch.setattr("app.trading.paper_trader._get_current_price", lambda t: (150.0, 0.5))

    with pytest.raises(IntentExecutionRejected) as exc_info:
        await execute_intent(
            intent_id="intent-no-parent-1",
            account_context={"bot_id": "bot-test", "effective_mode": "SHADOW"},
            current_quote={"price": 150.0, "age_hours": 0.5},
        )
    assert "PARENT_POLICY_MISSING" in exc_info.value.reason_code


@pytest.mark.asyncio
async def test_malformed_parent_policy_decision_fails_closed(monkeypatch):
    """Malformed parent policy decision document fails closed."""
    fake_intent = _make_test_intent(
        execution_intent_id="intent-malformed-parent-1",
        policy_decision_id="policy-malformed-1",
    )

    malformed_policy_doc = {
        "policy_decision_id": "policy-malformed-1",
        "disposition": "CORRUPTED_DISPOSITION_STRING",  # Invalid enum
    }

    def mock_find_docs(coll, q, **kw):
        if coll == "execution_intents":
            return [fake_intent.model_dump(mode="python")]
        if coll == "policy_decisions":
            return [malformed_policy_doc]
        return []

    monkeypatch.setattr("app.db.mongo_store.find_docs", mock_find_docs)

    with pytest.raises(IntentExecutionRejected) as exc_info:
        await execute_intent(
            intent_id="intent-malformed-parent-1",
            account_context={"bot_id": "bot-test", "effective_mode": "SHADOW"},
            current_quote={"price": 150.0, "age_hours": 0.5},
        )
    assert "MALFORMED_PARENT_POLICY" in exc_info.value.reason_code


@pytest.mark.asyncio
async def test_account_mismatch_fails_closed(monkeypatch):
    """Execution context bot_id mismatching intent bot_id fails closed."""
    fake_intent = _make_test_intent(
        execution_intent_id="intent-acct-mismatch-1",
        bot_id="bot-legitimate-owner",
    )

    monkeypatch.setattr(
        "app.db.mongo_store.find_docs",
        lambda coll, q, **kw: [fake_intent.model_dump(mode="python")] if coll == "execution_intents" else [],
    )

    with pytest.raises(IntentExecutionRejected) as exc_info:
        await execute_intent(
            intent_id="intent-acct-mismatch-1",
            account_context={"bot_id": "bot-attacker", "effective_mode": "SHADOW"},
            current_quote={"price": 150.0, "age_hours": 0.5},
        )
    assert "ACCOUNT_MISMATCH" in exc_info.value.reason_code


@pytest.mark.asyncio
async def test_unknown_quote_age_fails_closed(monkeypatch):
    """Quote with age_hours=None must fail closed instead of defaulting to fresh."""
    fake_intent = _make_test_intent(execution_intent_id="intent-unknown-age-1")
    pol_dec = _make_test_policy(policy_decision_id="policy-1")

    def mock_find_docs(coll, q, **kw):
        if coll == "execution_intents":
            return [fake_intent.model_dump(mode="python")]
        if coll == "policy_decisions":
            return [pol_dec.model_dump(mode="python")]
        return []

    monkeypatch.setattr("app.db.mongo_store.find_docs", mock_find_docs)

    with pytest.raises(IntentExecutionRejected) as exc_info:
        await execute_intent(
            intent_id="intent-unknown-age-1",
            account_context={"bot_id": "bot-test", "effective_mode": "SHADOW"},
            current_quote={"price": 150.0, "age_hours": None},  # Unknown quote age
        )
    assert "UNKNOWN_QUOTE_AGE" in exc_info.value.reason_code


@pytest.mark.asyncio
async def test_mode_downgrade_attempt_fails_closed(monkeypatch):
    """Attempting to execute in SHADOW or OBSERVE when account is configured for ENFORCE is rejected."""
    fake_intent = _make_test_intent(
        execution_intent_id="intent-downgrade-1",
        effective_mode="ENFORCE",
    )
    pol_dec = _make_test_policy(policy_decision_id="policy-1")

    def mock_find_docs(coll, q, **kw):
        if coll == "execution_intents":
            return [fake_intent.model_dump(mode="python")]
        if coll == "policy_decisions":
            return [pol_dec.model_dump(mode="python")]
        return []

    monkeypatch.setattr("app.db.mongo_store.find_docs", mock_find_docs)
    # Account mode is ENFORCE
    monkeypatch.setattr("app.trading.executor.resolve_control_plane_mode", lambda b: ControlPlaneMode.ENFORCE)

    # Caller attempts to downgrade to SHADOW
    with pytest.raises(IntentExecutionRejected) as exc_info:
        await execute_intent(
            intent_id="intent-downgrade-1",
            account_context={"bot_id": "bot-test", "effective_mode": "SHADOW"},
            current_quote={"price": 150.0, "age_hours": 0.5},
        )
    assert "MODE_DOWNGRADE_REJECTED" in exc_info.value.reason_code


@pytest.mark.asyncio
async def test_enforce_missing_required_risk_reservation_fails_closed(monkeypatch):
    """BUY execution under ENFORCE without active risk reservation fails closed."""
    fake_intent = _make_test_intent(
        execution_intent_id="intent-no-resv-1",
        effective_mode="ENFORCE",
        slot_key="slot:bot-test:AAPL",
    )
    pol_dec = _make_test_policy(policy_decision_id="policy-1")

    colls = {
        "risk_reservations": MagicMock(),
        "execution_slots": MagicMock(),
    }
    colls["risk_reservations"].find_one.return_value = None
    colls["execution_slots"].find_one.return_value = {
        "slot_key": "slot:bot-test:AAPL",
        "intent_id": "intent-no-resv-1",
        "status": "ACTIVE",
    }
    fake_db = MagicMock()
    fake_db.__getitem__.side_effect = lambda k: colls.get(k, MagicMock())

    def mock_find_docs(coll, q, **kw):
        if coll == "execution_intents":
            return [fake_intent.model_dump(mode="python")]
        if coll == "policy_decisions":
            return [pol_dec.model_dump(mode="python")]
        return []

    monkeypatch.setattr("app.db.mongo_store.find_docs", mock_find_docs)
    monkeypatch.setattr("app.db.mongo_store.get_doc_db", lambda: fake_db)
    monkeypatch.setattr("app.db.mongo_query.find_row", lambda *a, **k: [100000.0])
    monkeypatch.setattr("app.trading.executor.resolve_control_plane_mode", lambda b: ControlPlaneMode.ENFORCE)

    class FakeTxn:
        def __enter__(self):
            return None
        def __exit__(self, *args):
            pass

    monkeypatch.setattr("app.db.mongo_store.with_txn", lambda: FakeTxn())
    monkeypatch.setattr("app.trading.attribution.repository.consume_execution_intent", lambda *a, **k: True)

    with pytest.raises(IntentExecutionRejected) as exc_info:
        await execute_intent(
            intent_id="intent-no-resv-1",
            account_context={"bot_id": "bot-test", "effective_mode": "ENFORCE"},
            current_quote={"price": 150.0, "age_hours": 0.5},
        )
    assert "MISSING_REQUIRED_RESERVATION" in exc_info.value.reason_code


@pytest.mark.asyncio
async def test_enforce_missing_required_slot_fails_closed(monkeypatch):
    """BUY execution under ENFORCE without execution slot key fails closed."""
    fake_intent = _make_test_intent(
        execution_intent_id="intent-no-slot-1",
        effective_mode="ENFORCE",
        slot_key=None,  # Missing slot key!
    )
    pol_dec = _make_test_policy(policy_decision_id="policy-1")

    colls = {
        "risk_reservations": MagicMock(),
        "execution_slots": MagicMock(),
    }
    colls["risk_reservations"].find_one.return_value = {
        "execution_intent_id": "intent-no-slot-1",
        "status": "ACTIVE",
        "expires_at": datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=1),
    }
    fake_db = MagicMock()
    fake_db.__getitem__.side_effect = lambda k: colls.get(k, MagicMock())

    def mock_find_docs(coll, q, **kw):
        if coll == "execution_intents":
            return [fake_intent.model_dump(mode="python")]
        if coll == "policy_decisions":
            return [pol_dec.model_dump(mode="python")]
        return []

    monkeypatch.setattr("app.db.mongo_store.find_docs", mock_find_docs)
    monkeypatch.setattr("app.db.mongo_store.get_doc_db", lambda: fake_db)
    monkeypatch.setattr("app.db.mongo_query.find_row", lambda *a, **k: [100000.0])
    monkeypatch.setattr("app.trading.executor.resolve_control_plane_mode", lambda b: ControlPlaneMode.ENFORCE)

    class FakeTxn:
        def __enter__(self):
            return None
        def __exit__(self, *args):
            pass

    monkeypatch.setattr("app.db.mongo_store.with_txn", lambda: FakeTxn())
    monkeypatch.setattr("app.trading.attribution.repository.consume_execution_intent", lambda *a, **k: True)

    with pytest.raises(IntentExecutionRejected) as exc_info:
        await execute_intent(
            intent_id="intent-no-slot-1",
            account_context={"bot_id": "bot-test", "effective_mode": "ENFORCE"},
            current_quote={"price": 150.0, "age_hours": 0.5},
        )
    assert "MISSING_REQUIRED_SLOT" in exc_info.value.reason_code


@pytest.mark.asyncio
async def test_facade_legacy_execution_with_authority_succeeds(monkeypatch):
    """TradeFacade._execute_legacy mints valid FacadeExecutionAuthority and successfully executes legacy buy/sell."""
    monkeypatch.setattr("app.trading.paper_trader._ensure_bot", lambda b: None)
    monkeypatch.setattr("app.trading.paper_trader._get_current_price", lambda t: (150.0, 0.5))
    monkeypatch.setattr("app.db.mongo_query.find_row", lambda table, *a, **kw: [100000.0] if table == "bots" else ["pos-1", 10.0, 100.0])
    monkeypatch.setattr("app.db.mongo_query.find_rows", lambda *a, **kw: [])
    monkeypatch.setattr("app.db.mongo_store.insert_docs", lambda *a, **kw: None)

    fake_db = MagicMock()
    monkeypatch.setattr("app.db.mongo_store.get_doc_db", lambda: fake_db)

    class FakeTxn:
        def __enter__(self):
            return None
        def __exit__(self, *args):
            pass

    monkeypatch.setattr("app.db.mongo_store.with_txn", lambda: FakeTxn())

    # Executing via TradeFacade._execute_legacy (the only supported adapter)
    buy_res = await TradeFacade._execute_legacy(
        bot_id="bot-test",
        ticker="AAPL",
        action="BUY",
        size_pct=0.05,
        current_price=150.0,
    )
    assert "error" not in buy_res
    assert buy_res.get("ticker") == "AAPL"

    sell_res = await TradeFacade._execute_legacy(
        bot_id="bot-test",
        ticker="AAPL",
        action="SELL",
        size_pct=1.0,
        current_price=150.0,
    )
    assert "error" not in sell_res
    assert sell_res.get("ticker") == "AAPL"
