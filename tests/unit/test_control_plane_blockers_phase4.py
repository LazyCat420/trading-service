"""Unit tests for Phase 4: Lot Migration Replay Reconstruction and Integrity Enforcement.

Verifies:
1. Historical replay in FIFO order: BUY-SELL-BUY correctly matches remaining position to latest BUY fill.
2. Existing lots and repeated runs: Idempotent and zero duplicate lots created.
3. Orphan open lots detection for zero-position and non-existent position symbols.
4. Cost basis mismatch detection.
5. promote_bot_to_enforce: blocks promotion when violations exist, promotes when clean using bot_id.
"""

import datetime
import pytest
from unittest.mock import patch, MagicMock

from app.trading.attribution.repository import COLL_POSITION_LOTS
from app.trading.migration.lot_migrator import (
    reconcile_and_migrate_bot_positions,
    verify_bot_position_lot_integrity,
    promote_bot_to_enforce,
)


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

    def insert_many(self, docs, ordered=False, session=None, **kwargs):
        for doc in docs:
            self.data.append(dict(doc))
        res = MagicMock()
        res.inserted_ids = [d.get("_id", "mock-id") for d in docs]
        return res

    def update_one(self, filter_q, update_q, upsert=False, session=None):
        matched = 0
        modified = 0
        for doc in self.data:
            match = True
            for k, v in filter_q.items():
                if doc.get(k) != v:
                    match = False
                    break
            if match:
                matched += 1
                if "$set" in update_q:
                    doc.update(update_q["$set"])
                modified += 1
                break
        if matched == 0 and upsert:
            new_doc = dict(filter_q)
            if "$set" in update_q:
                new_doc.update(update_q["$set"])
            if "$setOnInsert" in update_q:
                new_doc.update(update_q["$setOnInsert"])
            self.data.append(new_doc)
            matched = 1
            modified = 1
        res = MagicMock()
        res.matched_count = matched
        res.modified_count = modified
        return res

    def create_index(self, *args, **kwargs):
        pass


class FakeDB:
    def __init__(self):
        self.cols = {}

    def __getitem__(self, item):
        if item not in self.cols:
            self.cols[item] = DictCollection()
        return self.cols[item]


def test_buy_sell_buy_fifo_replay_reconstruction():
    fake_db = FakeDB()
    now = datetime.datetime.now(datetime.timezone.utc)
    bot_id = "bot-recon-test"

    # Fills:
    # 1. Buy 10 @ 100
    # 2. Sell 10 @ 105
    # 3. Buy 5 @ 110
    fills = [
        ("fill-1", "BUY", 100.0, 10.0, now - datetime.timedelta(days=3), "ord-1"),
        ("fill-2", "SELL", 105.0, 10.0, now - datetime.timedelta(days=2), "ord-2"),
        ("fill-3", "BUY", 110.0, 5.0, now - datetime.timedelta(days=1), "ord-3"),
    ]
    # Current held position: 5 @ 110.0
    positions = [
        (1, "AAPL", 5.0, 110.0, now - datetime.timedelta(days=1))
    ]

    def mock_find_rows(col, q, proj, **kwargs):
        if col == "positions":
            return positions
        if col == "trade_fills":
            return fills
        return []

    with patch("app.db.mongo_store.get_doc_db", return_value=fake_db), \
         patch("app.db.mongo_store.with_txn") as mock_txn, \
         patch("app.db.mongo_query.find_rows", side_effect=mock_find_rows):
        
        mock_txn.return_value.__enter__.return_value = "mock-session"

        report = reconcile_and_migrate_bot_positions(bot_id)
        assert report["status"] == "PASS"
        assert report["lots_created"] == 1

        # Check the created lot
        created_lots = fake_db[COLL_POSITION_LOTS].data
        assert len(created_lots) == 1
        lot = created_lots[0]
        assert lot["ticker"] == "AAPL"
        assert lot["remaining_qty"] == 5.0
        # CRITICAL: Must be reconstructed from fill-3, NOT fill-1!
        assert lot["historical_fill_id"] == "fill-3"
        assert lot["entry_price"] == 110.0
        assert lot["provenance_complete"] is True
        assert lot["origin"] == "HISTORICAL_RECONSTRUCTION"


def test_repeated_migration_is_idempotent():
    fake_db = FakeDB()
    now = datetime.datetime.now(datetime.timezone.utc)
    bot_id = "bot-idempotent-test"

    fills = [
        ("fill-1", "BUY", 100.0, 10.0, now - datetime.timedelta(days=1), "ord-1")
    ]
    positions = [
        (1, "MSFT", 10.0, 100.0, now - datetime.timedelta(days=1))
    ]

    def mock_find_rows(col, q, proj, **kwargs):
        if col == "positions":
            return positions
        if col == "trade_fills":
            return fills
        return []

    with patch("app.db.mongo_store.get_doc_db", return_value=fake_db), \
         patch("app.db.mongo_store.with_txn") as mock_txn, \
         patch("app.db.mongo_query.find_rows", side_effect=mock_find_rows):
        
        mock_txn.return_value.__enter__.return_value = "mock-session"

        # First run creates lot
        rep1 = reconcile_and_migrate_bot_positions(bot_id)
        assert rep1["lots_created"] == 1

        # Second run should find existing lot and create 0 new lots
        rep2 = reconcile_and_migrate_bot_positions(bot_id)
        assert rep2["lots_created"] == 0
        assert len(fake_db[COLL_POSITION_LOTS].data) == 1


def test_orphan_lots_and_cost_basis_mismatch_detection():
    fake_db = FakeDB()
    now = datetime.datetime.now(datetime.timezone.utc)
    bot_id = "bot-orphan-test"

    # Positions: AAPL 10 shares @ $100
    positions = [
        (1, "AAPL", 10.0, 100.0)
    ]
    # But existing lots in db has:
    # 1. AAPL lot with entry_price 150.0 (Cost basis mismatch!)
    # 2. TSLA lot with 5 shares (Orphan lot! No TSLA in positions!)
    fake_db[COLL_POSITION_LOTS].insert_one({
        "lot_id": "lot-aapl-1",
        "bot_id": bot_id,
        "ticker": "AAPL",
        "remaining_qty": 10.0,
        "entry_price": 150.0,
        "status": "open",
    })
    fake_db[COLL_POSITION_LOTS].insert_one({
        "lot_id": "lot-tsla-orphan",
        "bot_id": bot_id,
        "ticker": "TSLA",
        "remaining_qty": 5.0,
        "entry_price": 200.0,
        "status": "open",
    })

    with patch("app.db.mongo_store.get_doc_db", return_value=fake_db), \
         patch("app.db.mongo_query.find_rows", return_value=positions):

        is_valid, errors = verify_bot_position_lot_integrity(bot_id)
        assert is_valid is False
        assert len(errors) == 2
        error_str = " ".join(errors)
        assert "cost basis mismatch" in error_str
        assert "orphan open lot" in error_str


def test_promote_bot_to_enforce_boundaries():
    fake_db = FakeDB()
    bot_id = "bot-promo-test"
    fake_db["bots"].insert_one({"bot_id": bot_id, "control_plane_mode": "OBSERVE"})

    # Clean positions and lots
    positions = [(1, "NVDA", 10.0, 120.0)]
    fake_db[COLL_POSITION_LOTS].insert_one({
        "lot_id": "lot-nvda-1",
        "bot_id": bot_id,
        "ticker": "NVDA",
        "remaining_qty": 10.0,
        "entry_price": 120.0,
        "status": "open",
    })

    with patch("app.db.mongo_store.get_doc_db", return_value=fake_db), \
         patch("app.db.mongo_query.find_rows", return_value=positions):

        # Promotion succeeds when integrity passes
        promo_res = promote_bot_to_enforce(bot_id)
        assert promo_res["status"] == "PROMOTED"
        assert promo_res["control_plane_mode"] == "ENFORCE"
        # Verify db update
        bot_doc = fake_db["bots"].find_one({"bot_id": bot_id})
        assert bot_doc["control_plane_mode"] == "ENFORCE"

    # Dirty state: position has 15 shares, lots only have 10 shares
    dirty_positions = [(1, "NVDA", 15.0, 120.0)]
    with patch("app.db.mongo_store.get_doc_db", return_value=fake_db), \
         patch("app.db.mongo_query.find_rows", return_value=dirty_positions):

        # Promotion must fail closed
        with pytest.raises(ValueError) as exc_info:
            promote_bot_to_enforce(bot_id)
        assert "integrity violations present" in str(exc_info.value)
