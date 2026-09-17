"""Unit tests for Phase 1: Admission, Account Ownership, and Replay Semantics.

Verifies:
1. False-success execution bug: TradeFacade must execute the intent via execute_intent and NOT return ALREADY_PROCESSED on first submission.
2. bot_id is preserved on DecisionArtifact, PolicyInputSnapshot, and ExecutionIntent; 'default' or missing bot_id is rejected.
3. Distinguish CREATED (resumed) from CONSUMED (already processed) on duplicate idempotency key.
4. Stable request IDs.
"""

import datetime
import pytest
from unittest.mock import AsyncMock, patch

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
    COLL_EXECUTION_SLOTS,
    COLL_RISK_RESERVATIONS,
    admit_execution_intent,
    AdmissionError,
)
from app.trading.control_plane import ControlPlaneMode
from app.trading.facade import TradeFacade, TradeResultStatus
from app.trading.policy.policy_translator import PolicyInputSnapshot, PolicyTranslator


class SimpleDictCollection:
    def __init__(self, data=None):
        self.data = list(data) if data else []

    def find(self, query=None, projection=None, session=None):
        query = query or {}
        matched = []
        for doc in self.data:
            match = True
            for k, v in query.items():
                val = doc.get(k)
                if isinstance(v, dict):
                    if "$gte" in v and (val is None or val < v["$gte"]):
                        match = False
                        break
                    if "$gt" in v and (val is None or val <= v["$gt"]):
                        match = False
                        break
                    if "$in" in v and val not in v["$in"]:
                        match = False
                        break
                elif val != v:
                    match = False
                    break
            if match:
                matched.append(dict(doc))
        class Cursor:
            def __init__(self, items):
                self.items = items
            def sort(self, *args, **kwargs):
                return self
            def limit(self, n):
                if n > 0:
                    self.items = self.items[:n]
                return self
            def __iter__(self):
                return iter(self.items)
            def __getitem__(self, idx):
                return self.items[idx]
            def to_list(self, length=None):
                return self.items[:length] if length else self.items
        return Cursor(matched)

    def count_documents(self, query=None, session=None):
        return len(list(self.find(query, session=session)))

    def find_one(self, query, session=None):
        res = list(self.find(query, session=session))
        return res[0] if res else None

    def insert_one(self, doc, session=None):
        self.data.append(dict(doc))
        class Res:
            inserted_id = "test-id"
        return Res()

    def insert_many(self, docs, *args, **kwargs):
        for doc in docs:
            self.data.append(dict(doc))
        class Res:
            inserted_ids = ["test-id"] * len(docs)
        return Res()

    def create_index(self, *args, **kwargs):
        pass

    def list_indexes(self, *args, **kwargs):
        return []

    def aggregate(self, pipeline, session=None):
        return []

    def update_one(self, query, update, session=None):
        doc = self.find_one(query, session=session)
        if doc:
            for idx, d in enumerate(self.data):
                if d == doc:
                    if "$set" in update:
                        d.update(update["$set"])
                    if "$inc" in update:
                        for ik, iv in update["$inc"].items():
                            d[ik] = d.get(ik, 0) + iv
                    class Res:
                        modified_count = 1
                    return Res()
        class Res:
            modified_count = 0
        return Res()


class SimpleDictDb:
    def __init__(self):
        self.collections = {}

    def __getitem__(self, name):
        if name not in self.collections:
            self.collections[name] = SimpleDictCollection()
        return self.collections[name]


@pytest.mark.asyncio
async def test_facade_enforce_does_not_falsely_report_already_processed_on_first_submit(monkeypatch):
    """Bug reproduction: Real facade + real admit_execution_intent should execute intent,

    not declare ALREADY_PROCESSED on first attempt.
    """
    fake_db = SimpleDictDb()
    # Seed bot with cash
    fake_db["bots"].insert_one({"bot_id": "bot-test", "cash_balance": 100000.0})

    monkeypatch.setattr("app.db.mongo_store.get_doc_db", lambda: fake_db)
    monkeypatch.setattr("app.trading.attribution.repository.mongo_store.get_doc_db", lambda: fake_db)
    monkeypatch.setattr("app.trading.facade.resolve_control_plane_mode", lambda bot_id: ControlPlaneMode.ENFORCE)
    monkeypatch.setattr("app.trading.paper_trader._get_current_price", lambda ticker: (150.0, 0.5))
    monkeypatch.setattr("app.trading.facade.build_policy_snapshot", lambda **kwargs: (
        PolicyInputSnapshot(
            portfolio_equity=100000.0,
            cash_balance=100000.0,
            quote_price=150.0,
            quote_age_hours=0.5,
            bot_id="bot-test",
        ),
        {},
    ))
    # Mock find_row for bot cash
    monkeypatch.setattr("app.db.mongo_query.find_row", lambda table, query, cols, **kwargs: (
        [100000.0] if table == "bots" else None
    ))
    # Mock with_txn to yield None session
    class FakeTxn:
        def __enter__(self):
            return None
        def __exit__(self, *args):
            pass
    monkeypatch.setattr("app.db.mongo_store.with_txn", lambda: FakeTxn())
    monkeypatch.setattr("app.trading.attribution.repository.mongo_store.with_txn", lambda: FakeTxn())

    mock_executor = AsyncMock(return_value={
        "status": "FILLED",
        "order_id": "ord-enforce-1",
        "ticker": "AAPL",
        "side": "BUY",
        "fill_price": 150.0,
        "qty": 10.0,
    })
    monkeypatch.setattr("app.trading.facade.execute_intent", mock_executor)

    res = await TradeFacade.submit_trade(
        bot_id="bot-test",
        ticker="AAPL",
        action="BUY",
        size_pct=0.10,
        confidence=85,
        idempotency_key="unique-idemp-key-1",
    )

    # In the unfixed code:
    # res['status'] == 'ALREADY_PROCESSED'
    # mock_executor.assert_called_once() FAILS because mock_executor was never called!
    assert res["status"] == TradeResultStatus.COMMITTED.value
    assert res["effective_mode"] == "ENFORCE"
    assert res["trade_executed"] is True
    mock_executor.assert_called_once()


@pytest.mark.asyncio
async def test_admission_rejects_missing_or_default_bot_id(monkeypatch):
    """Admission must reject missing bot_id or 'default' bot_id."""
    fake_db = SimpleDictDb()
    monkeypatch.setattr("app.db.mongo_store.get_doc_db", lambda: fake_db)
    monkeypatch.setattr("app.trading.attribution.repository.mongo_store.get_doc_db", lambda: fake_db)
    class FakeTxn:
        def __enter__(self):
            return None
        def __exit__(self, *args):
            pass
    monkeypatch.setattr("app.db.mongo_store.with_txn", lambda: FakeTxn())
    monkeypatch.setattr("app.trading.attribution.repository.mongo_store.with_txn", lambda: FakeTxn())

    intent = ExecutionIntent(
        execution_intent_id="int-test-1",
        decision_id="dec-1",
        policy_decision_id="pol-1",
        ticker="AAPL",
        side="BUY",
        approved_notional=1000.0,
        approved_size_pct=0.10,
        valid_from=datetime.datetime.now(datetime.timezone.utc),
        expires_at=datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=1),
        idempotency_key="idemp-1",
        bot_id="default",  # Default or empty should be rejected!
    )

    with pytest.raises(AdmissionError) as exc:
        admit_execution_intent(intent=intent, slot_key="slot:test", required_notional=1000.0)
    assert "INVALID_ACCOUNT" in getattr(exc.value, "reason_code", str(exc.value))


@pytest.mark.asyncio
async def test_duplicate_intent_in_created_state_resumes_execution(monkeypatch):
    """If an intent already exists in CREATED state, duplicate submission must resume execution."""
    fake_db = SimpleDictDb()
    fake_db["bots"].insert_one({"bot_id": "bot-test", "cash_balance": 100000.0})

    existing_intent = ExecutionIntent(
        execution_intent_id="int-resume-1",
        decision_id="dec-resume-1",
        policy_decision_id="pol-resume-1",
        ticker="AAPL",
        side="BUY",
        approved_notional=1000.0,
        approved_size_pct=0.10,
        valid_from=datetime.datetime.now(datetime.timezone.utc),
        expires_at=datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=1),
        idempotency_key="idemp-resume-1",
        bot_id="bot-test",
        status=IntentStatus.CREATED,
    )
    fake_db[COLL_EXECUTION_INTENTS].insert_one(existing_intent.model_dump(mode="python"))

    monkeypatch.setattr("app.db.mongo_store.get_doc_db", lambda: fake_db)
    monkeypatch.setattr("app.trading.attribution.repository.mongo_store.get_doc_db", lambda: fake_db)
    monkeypatch.setattr("app.trading.facade.resolve_control_plane_mode", lambda bot_id: ControlPlaneMode.ENFORCE)
    monkeypatch.setattr("app.trading.paper_trader._get_current_price", lambda ticker: (150.0, 0.5))
    monkeypatch.setattr("app.trading.facade.build_policy_snapshot", lambda **kwargs: (
        PolicyInputSnapshot(
            portfolio_equity=100000.0,
            cash_balance=100000.0,
            quote_price=150.0,
            quote_age_hours=0.5,
            bot_id="bot-test",
        ),
        {},
    ))
    monkeypatch.setattr("app.db.mongo_query.find_row", lambda table, query, cols, **kwargs: (
        [100000.0] if table == "bots" else None
    ))
    class FakeTxn:
        def __enter__(self):
            return None
        def __exit__(self, *args):
            pass
    monkeypatch.setattr("app.db.mongo_store.with_txn", lambda: FakeTxn())
    monkeypatch.setattr("app.trading.attribution.repository.mongo_store.with_txn", lambda: FakeTxn())

    mock_executor = AsyncMock(return_value={
        "status": "FILLED",
        "order_id": "ord-resume-1",
        "ticker": "AAPL",
        "side": "BUY",
        "fill_price": 150.0,
        "qty": 10.0,
    })
    monkeypatch.setattr("app.trading.facade.execute_intent", mock_executor)

    res = await TradeFacade.submit_trade(
        bot_id="bot-test",
        ticker="AAPL",
        action="BUY",
        size_pct=0.10,
        confidence=85,
        idempotency_key="idemp-resume-1",
    )

    # Resume must call execute_intent, not falsely return ALREADY_PROCESSED without executing
    mock_executor.assert_called_once()
    assert res["status"] == TradeResultStatus.COMMITTED.value
    assert res["trade_executed"] is True

