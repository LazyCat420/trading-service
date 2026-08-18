"""
Snapshot Writer Smoke Tests (plan item D3).

Two independent "snapshot" writers persist state each cycle. Both are dormant
in an all-HOLD regime only in the sense that snapshots follow executed trades —
but the writers themselves have NO real-trade gate, so they can be exercised
directly here:

  1. save_desk (app/v3/desk_persistence.py) — upserts the whole SharedDesk as
     a JSON string into `shared_desk`, keyed on (cycle_id, ticker)
     (idempotent by design).
  2. take_snapshot (app/trading/portfolio.py) — appends a document to
     `portfolio_snapshots` (fresh uuid PK each call; NOT idempotent by design —
     it builds the peak history the drawdown breaker reads).

These used to patch `get_db` and assert on SQL text ("INSERT INTO shared_desk",
"ON CONFLICT (desk_id) DO UPDATE") plus positional params. Both modules write
through `mongo_store` now — `desk_persistence` imports no `get_db` at all — so
the patched cursor intercepted nothing, `_find_call` searched an empty call
list, and the writes went to the live store while the assertions scored a mock.

They patch `mongo_store` (and `mongo_query`, so the reads cannot escape either)
and assert on the STRUCTURE of the write: collection name, the upsert FILTER
that carries the idempotency key, and the document fields. That is stronger
than the SQL substring — it pins which key the upsert actually collapses on.

NOTE the contract moved with the port: the upsert key is now the
(cycle_id, ticker) pair rather than desk_id. Same idempotency guarantee, a
different key, and the test says so explicitly.
"""
import os
import sys
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))


def _upserts(store, collection):
    """Every upsert_doc call against `collection`."""
    return [c for c in store.upsert_doc.call_args_list
            if c.args and c.args[0] == collection]


def _inserts(store, collection):
    """Every insert_docs call against `collection`."""
    return [c for c in store.insert_docs.call_args_list
            if c.args and c.args[0] == collection]


class TestSaveDesk:
    """save_desk should upsert the desk keyed on (cycle_id, ticker) (idempotent)."""

    def _make_desk(self):
        from app.v3.shared_desk import SharedDesk
        return SharedDesk(cycle_id="cycle-test-1", ticker="AAPL")

    def _patch(self):
        store = MagicMock()
        query = MagicMock()
        query.find_row.return_value = None
        query.find_rows.return_value = []
        return (
            patch("app.v3.desk_persistence.mongo_store", store),
            patch("app.v3.desk_persistence.mongo_query", query),
            store,
        )

    def test_writes_upsert_with_expected_params(self):
        from app.v3.desk_persistence import save_desk
        ps, pq, store = self._patch()
        desk = self._make_desk()
        with ps, pq:
            save_desk(desk)

        calls = _upserts(store, "shared_desk")
        assert calls, "save_desk must upsert into shared_desk"
        collection, key_filter, doc = calls[0].args[:3]
        # The upsert filter IS the idempotency key.
        assert key_filter == {"cycle_id": "cycle-test-1", "ticker": "AAPL"}, \
            "must be an idempotent upsert keyed on cycle_id + ticker"
        assert doc["desk_id"] == desk.desk_id
        assert doc["cycle_id"] == "cycle-test-1"
        assert doc["ticker"] == "AAPL"
        assert doc["phase"] == desk.phase.value  # "INIT"
        # desk_data is JSON — must be a serialized string, not a dict.
        assert isinstance(doc["desk_data"], str)
        assert "AAPL" in doc["desk_data"]

    def test_idempotent_same_desk_id_across_calls(self):
        from app.v3.desk_persistence import save_desk
        ps, pq, store = self._patch()
        desk = self._make_desk()
        with ps, pq:
            save_desk(desk)
            save_desk(desk)

        calls = _upserts(store, "shared_desk")
        assert len(calls) == 2, "both calls hit the writer"
        # Idempotency: the upsert key is identical across the two calls, so
        # they collapse to one logical document.
        assert calls[0].args[1] == calls[1].args[1]
        # ...and the desk_id carried in the document is stable too.
        assert calls[0].args[2]["desk_id"] == calls[1].args[2]["desk_id"] == desk.desk_id


class TestTakeSnapshot:
    """take_snapshot appends a portfolio_snapshots document (peak-history feeder)."""

    def _patch_portfolio_mongo(self):
        store = MagicMock()
        store.dec128.side_effect = lambda v: v
        query = MagicMock()
        query.find_row.return_value = None
        query.find_rows.return_value = []
        return (
            patch("app.trading.portfolio.mongo_store", store),
            patch("app.trading.portfolio.mongo_query", query),
            store,
        )

    def test_inserts_snapshot_row(self):
        import app.trading.portfolio as portfolio
        ps, pq, store = self._patch_portfolio_mongo()
        state = {"cash": 20_000.0, "total_value": 105_000.0}

        with ps, pq, \
             patch.object(portfolio, "get_current_state", return_value=state), \
             patch.object(portfolio, "_get_default_bot_id", return_value="bot-1"):
            result = portfolio.take_snapshot("bot-1")

        calls = _inserts(store, "portfolio_snapshots")
        assert calls, "take_snapshot must insert into portfolio_snapshots"
        doc = calls[0].args[1][0]
        assert doc["bot_id"] == "bot-1"
        assert doc["cash_balance"] == 20_000.0
        assert doc["total_value"] == 105_000.0
        assert result == state

    def test_not_idempotent_fresh_pk_each_call(self):
        import app.trading.portfolio as portfolio
        ps, pq, store = self._patch_portfolio_mongo()
        state = {"cash": 20_000.0, "total_value": 105_000.0}

        with ps, pq, \
             patch.object(portfolio, "get_current_state", return_value=state), \
             patch.object(portfolio, "_get_default_bot_id", return_value="bot-1"):
            portfolio.take_snapshot("bot-1")
            portfolio.take_snapshot("bot-1")

        calls = _inserts(store, "portfolio_snapshots")
        assert len(calls) == 2
        # By design each call mints a fresh uuid PK → two distinct documents
        # (this is what builds the peak history the drawdown breaker reads).
        assert calls[0].args[1][0]["id"] != calls[1].args[1][0]["id"]

    def test_swallows_db_error(self):
        # take_snapshot logs and swallows DB errors (non-fatal), returning state.
        import app.trading.portfolio as portfolio
        ps, pq, store = self._patch_portfolio_mongo()
        store.insert_docs.side_effect = RuntimeError("db down")

        state = {"cash": 20_000.0, "total_value": 105_000.0}
        with ps, pq, \
             patch.object(portfolio, "get_current_state", return_value=state), \
             patch.object(portfolio, "_get_default_bot_id", return_value="bot-1"):
            result = portfolio.take_snapshot("bot-1")

        assert result == state, "snapshot failure must not break the caller"
