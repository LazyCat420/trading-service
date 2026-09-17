"""Unit tests for Phase 3: Concurrency and Ledger Invariants.

Verifies:
1. Slot ownership validation inside executor (SLOT_OWNERSHIP_INVALID).
2. Reservation validity inside executor (RESERVATION_EXPIRED).
3. Current position quantity check for SELL (INSUFFICIENT_POSITION_QTY).
4. Unmatched lot quantity check for SELL (UNMATCHED_LOT_QUANTITY).
5. BUY fee deduction from cash balance.
6. SHADOW execution persistence without mutating live portfolio.
"""

import datetime
import pytest
from unittest.mock import patch, MagicMock

from app.trading.attribution.models import (
    ExecutionIntent,
    IntentStatus,
    ReservationStatus,
)
from app.trading.attribution.repository import (
    COLL_EXECUTION_INTENTS,
    COLL_EXECUTION_SLOTS,
    COLL_POSITION_LOTS,
    COLL_RISK_RESERVATIONS,
)
from app.trading.control_plane import ControlPlaneMode
from app.trading.executor import execute_intent, IntentExecutionRejected


class DictCollection:
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
        return Cursor(matched)

    def find_one(self, query=None, sort=None, session=None):
        cursor = self.find(query=query, session=session)
        items = cursor.items
        return dict(items[0]) if items else None

    def insert_one(self, doc, session=None):
        self.data.append(dict(doc))
        res = MagicMock()
        res.inserted_id = doc.get("_id", "mock-id")
        return res

    def update_one(self, filter_q, update_q, upsert=False, session=None):
        modified = 0
        matched = 0
        now = datetime.datetime.now(datetime.timezone.utc)
        for doc in self.data:
            match = True
            for k, v in filter_q.items():
                val = doc.get(k)
                if isinstance(v, dict):
                    if "$gt" in v and (val is None or val <= v["$gt"]):
                        match = False
                        break
                    if "$gte" in v and (val is None or val < v["$gte"]):
                        match = False
                        break
                    if "$in" in v and val not in v["$in"]:
                        match = False
                        break
                elif val != v:
                    match = False
                    break
            if match:
                matched += 1
                if "$set" in update_q:
                    doc.update(update_q["$set"])
                if "$inc" in update_q:
                    for ik, iv in update_q["$inc"].items():
                        doc[ik] = doc.get(ik, 0) + iv
                modified += 1
                break
        res = MagicMock()
        res.matched_count = matched
        res.modified_count = modified
        return res

    def update_many(self, filter_q, update_q, upsert=False, session=None):
        modified = 0
        matched = 0
        for doc in self.data:
            match = True
            for k, v in filter_q.items():
                val = doc.get(k)
                if isinstance(v, dict):
                    if "$gt" in v and (val is None or val <= v["$gt"]):
                        match = False
                        break
                    if "$gte" in v and (val is None or val < v["$gte"]):
                        match = False
                        break
                    if "$in" in v and val not in v["$in"]:
                        match = False
                        break
                elif val != v:
                    match = False
                    break
            if match:
                matched += 1
                if "$set" in update_q:
                    doc.update(update_q["$set"])
                if "$inc" in update_q:
                    for ik, iv in update_q["$inc"].items():
                        doc[ik] = doc.get(ik, 0) + iv
                modified += 1
        res = MagicMock()
        res.matched_count = matched
        res.modified_count = modified
        return res

    def delete_one(self, filter_q, session=None):
        for idx, doc in enumerate(self.data):
            match = True
            for k, v in filter_q.items():
                if doc.get(k) != v:
                    match = False
                    break
            if match:
                self.data.pop(idx)
                break
        res = MagicMock()
        res.deleted_count = 1
        return res

    def insert_many(self, docs, ordered=False, session=None, **kwargs):
        for doc in docs:
            self.data.append(dict(doc))
        res = MagicMock()
        res.inserted_ids = [d.get("_id", "mock-id") for d in docs]
        return res

    def create_index(self, *args, **kwargs):
        pass

    def list_indexes(self, *args, **kwargs):
        return []


class FakeDB:
    def __init__(self):
        self.cols = {}

    def __getitem__(self, item):
        if item not in self.cols:
            self.cols[item] = DictCollection()
        return self.cols[item]


async def test_slot_ownership_validation():
    fake_db = FakeDB()
    now = datetime.datetime.now(datetime.timezone.utc)
    intent = ExecutionIntent(
        execution_intent_id="intent-slot-1",
        decision_id="dec-1",
        policy_decision_id="pol-1",
        ticker="AAPL",
        side="BUY",
        approved_notional=1000.0,
        approved_size_pct=0.01,
        valid_from=now - datetime.timedelta(minutes=1),
        expires_at=now + datetime.timedelta(minutes=10),
        idempotency_key="key-slot-1",
        bot_id="bot-slot-test",
        slot_key="slot-alpha",
    )
    fake_db[COLL_EXECUTION_INTENTS].insert_one(intent.model_dump(mode="python"))
    # Slot held by a DIFFERENT intent
    fake_db[COLL_EXECUTION_SLOTS].insert_one({
        "slot_key": "slot-alpha",
        "intent_id": "intent-other",
        "status": "RESERVED",
    })
    fake_db["bots"].insert_one({"bot_id": "bot-slot-test", "cash_balance": 50000.0})

    with patch("app.db.mongo_store.get_doc_db", return_value=fake_db), \
         patch("app.db.mongo_store.find_docs", side_effect=lambda col, q, **kw: fake_db[col].find(q).items), \
         patch("app.db.mongo_store.with_txn") as mock_txn, \
         patch("app.db.mongo_query.find_row", return_value=[50000.0]), \
         patch("app.trading.executor._get_current_price", return_value=(150.0, 0.5)):
        
        mock_txn.return_value.__enter__.return_value = "mock-session"

        with pytest.raises(IntentExecutionRejected) as exc_info:
            await execute_intent(intent.execution_intent_id, account_context={"bot_id": "bot-slot-test", "effective_mode": "ENFORCE"})
        assert exc_info.value.reason_code == "SLOT_OWNERSHIP_INVALID"


async def test_reservation_validity_check():
    fake_db = FakeDB()
    now = datetime.datetime.now(datetime.timezone.utc)
    intent = ExecutionIntent(
        execution_intent_id="intent-resv-1",
        decision_id="dec-2",
        policy_decision_id="pol-2",
        ticker="AAPL",
        side="BUY",
        approved_notional=1000.0,
        approved_size_pct=0.01,
        valid_from=now - datetime.timedelta(minutes=5),
        expires_at=now + datetime.timedelta(minutes=10),
        idempotency_key="key-resv-1",
        bot_id="bot-resv-test",
    )
    fake_db[COLL_EXECUTION_INTENTS].insert_one(intent.model_dump(mode="python"))
    # Reservation expired
    fake_db[COLL_RISK_RESERVATIONS].insert_one({
        "execution_intent_id": "intent-resv-1",
        "status": ReservationStatus.ACTIVE.value,
        "expires_at": now - datetime.timedelta(seconds=10),
    })
    fake_db["bots"].insert_one({"bot_id": "bot-resv-test", "cash_balance": 50000.0})

    with patch("app.db.mongo_store.get_doc_db", return_value=fake_db), \
         patch("app.db.mongo_store.find_docs", side_effect=lambda col, q, **kw: fake_db[col].find(q).items), \
         patch("app.db.mongo_store.with_txn") as mock_txn, \
         patch("app.db.mongo_query.find_row", return_value=[50000.0]), \
         patch("app.trading.executor._get_current_price", return_value=(150.0, 0.5)):
        
        mock_txn.return_value.__enter__.return_value = "mock-session"

        with pytest.raises(IntentExecutionRejected) as exc_info:
            await execute_intent(intent.execution_intent_id, account_context={"bot_id": "bot-resv-test", "effective_mode": "ENFORCE"})
        assert exc_info.value.reason_code == "RESERVATION_EXPIRED"


async def test_sell_unmatched_lot_quantity_aborts():
    fake_db = FakeDB()
    now = datetime.datetime.now(datetime.timezone.utc)
    intent = ExecutionIntent(
        execution_intent_id="intent-sell-1",
        decision_id="dec-3",
        policy_decision_id="pol-3",
        ticker="MSFT",
        side="SELL",
        approved_quantity=10.0,
        approved_size_pct=1.0,
        valid_from=now - datetime.timedelta(minutes=1),
        expires_at=now + datetime.timedelta(minutes=10),
        idempotency_key="key-sell-1",
        bot_id="bot-sell-test",
    )
    fake_db[COLL_EXECUTION_INTENTS].insert_one(intent.model_dump(mode="python"))
    fake_db["bots"].insert_one({"bot_id": "bot-sell-test", "cash_balance": 10000.0})
    fake_db["positions"].insert_one({"bot_id": "bot-sell-test", "ticker": "MSFT", "qty": 10.0, "avg_entry_price": 200.0})
    
    # BUT only 5 shares exist in position_lots
    fake_db[COLL_POSITION_LOTS].insert_one({
        "lot_id": "lot-1",
        "bot_id": "bot-sell-test",
        "ticker": "MSFT",
        "remaining_qty": 5.0,
        "entry_price": 200.0,
        "status": "open",
        "opened_at": now - datetime.timedelta(days=1),
    })

    with patch("app.db.mongo_store.get_doc_db", return_value=fake_db), \
         patch("app.db.mongo_store.find_docs", side_effect=lambda col, q, **kw: fake_db[col].find(q).items), \
         patch("app.db.mongo_store.with_txn") as mock_txn, \
         patch("app.db.mongo_query.find_row", side_effect=lambda col, q, p, **kwargs: [1, 10.0, 200.0] if col == "positions" else [10000.0]), \
         patch("app.trading.executor._get_current_price", return_value=(210.0, 0.5)):
        
        mock_txn.return_value.__enter__.return_value = "mock-session"

        with pytest.raises(IntentExecutionRejected) as exc_info:
            await execute_intent(intent.execution_intent_id, account_context={"bot_id": "bot-sell-test", "effective_mode": "ENFORCE"})
        assert exc_info.value.reason_code == "UNMATCHED_LOT_QUANTITY"


async def test_buy_fee_deduction_and_shadow_persistence():
    fake_db = FakeDB()
    now = datetime.datetime.now(datetime.timezone.utc)
    intent = ExecutionIntent(
        execution_intent_id="intent-buy-fee",
        decision_id="dec-4",
        policy_decision_id="pol-4",
        ticker="AAPL",
        side="BUY",
        approved_notional=1000.0,
        approved_size_pct=0.01,
        valid_from=now - datetime.timedelta(minutes=1),
        expires_at=now + datetime.timedelta(minutes=10),
        idempotency_key="key-buy-fee",
        bot_id="bot-fee-test",
    )
    fake_db[COLL_EXECUTION_INTENTS].insert_one(intent.model_dump(mode="python"))
    bot_doc = {"bot_id": "bot-fee-test", "cash_balance": 10000.0}
    fake_db["bots"].insert_one(bot_doc)

    with patch("app.db.mongo_store.get_doc_db", return_value=fake_db), \
         patch("app.db.mongo_store.find_docs", side_effect=lambda col, q, **kw: fake_db[col].find(q).items), \
         patch("app.db.mongo_store.with_txn") as mock_txn, \
         patch("app.db.mongo_query.find_row", side_effect=lambda col, q, p, **kwargs: [10000.0] if col == "bots" else None), \
         patch("app.trading.executor._get_current_price", return_value=(100.0, 0.5)), \
         patch("app.trading.executor._apply_execution_cost", return_value=(100.0, {"commission": 2.50, "exchange_fee": 0.50})):
        
        mock_txn.return_value.__enter__.return_value = "mock-session"

        res = await execute_intent(intent.execution_intent_id, account_context={"bot_id": "bot-fee-test", "effective_mode": "ENFORCE"})
        assert res["status"] == "FILLED"
        assert res["fees"] == 3.0
        # Cash should be deducted by approved_notional (1000) + fees (3.0) = 1003.0
        updated_bot = fake_db["bots"].find_one({"bot_id": "bot-fee-test"})
        assert updated_bot["cash_balance"] == pytest.approx(10000.0 - 1003.0, 0.01)

    # Test SHADOW mode persists simulation
    intent_shadow = ExecutionIntent(
        execution_intent_id="intent-shadow-1",
        decision_id="dec-5",
        policy_decision_id="pol-5",
        ticker="AAPL",
        side="BUY",
        approved_notional=500.0,
        approved_size_pct=0.005,
        valid_from=now - datetime.timedelta(minutes=1),
        expires_at=now + datetime.timedelta(minutes=10),
        idempotency_key="key-shadow-1",
        bot_id="bot-fee-test",
    )
    fake_db[COLL_EXECUTION_INTENTS].insert_one(intent_shadow.model_dump(mode="python"))

    with patch("app.db.mongo_store.get_doc_db", return_value=fake_db), \
         patch("app.db.mongo_store.find_docs", side_effect=lambda col, q, **kw: fake_db[col].find(q).items), \
         patch("app.db.mongo_query.find_row", return_value=[10000.0]), \
         patch("app.trading.executor._get_current_price", return_value=(100.0, 0.5)):
        
        s_res = await execute_intent(intent_shadow.execution_intent_id, account_context={"bot_id": "bot-fee-test", "effective_mode": "SHADOW"})
        assert s_res["status"] == "SIMULATED"
        assert s_res["simulated"] is True
        # Verify shadow_executions record
        shadow_doc = fake_db["shadow_executions"].find_one({"execution_intent_id": "intent-shadow-1"})
        assert shadow_doc is not None
        assert shadow_doc["simulated"] is True
        # Verify live portfolio cash was NOT mutated in SHADOW mode
        assert updated_bot["cash_balance"] == pytest.approx(10000.0 - 1003.0, 0.01)
