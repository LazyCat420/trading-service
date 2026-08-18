"""
Snapshot Writer Smoke Tests (plan item D3).

Two independent "snapshot" writers persist state each cycle. Both are dormant
in an all-HOLD regime only in the sense that snapshots follow executed trades —
but the writers themselves have NO real-trade gate, so they can be exercised
directly here:

  1. save_desk (app/v3/desk_persistence.py) — upserts the whole SharedDesk as
     JSONB into `shared_desk`, keyed on desk_id (idempotent by design).
  2. take_snapshot (app/trading/portfolio.py) — appends a row to
     `portfolio_snapshots` (fresh uuid PK each call; NOT idempotent by design —
     it builds the peak history the drawdown breaker reads).

These assert the SQL shape + params and the (non-)idempotency contract, so a
regression that silently changes the write path is caught without a live trade.
"""
import os
import sys
from contextlib import contextmanager
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))


def _executed_sql(mock_db):
    """Return the list of SQL strings passed to db.execute()."""
    return [call.args[0] for call in mock_db.execute.call_args_list if call.args]


def _find_call(mock_db, needle):
    """Return the first execute() call whose SQL contains `needle`, or None."""
    for call in mock_db.execute.call_args_list:
        if call.args and needle in call.args[0]:
            return call
    return None


class TestSaveDesk:
    """save_desk should upsert the desk keyed on desk_id (idempotent)."""

    def _make_desk(self):
        from app.v3.shared_desk import SharedDesk
        return SharedDesk(cycle_id="cycle-test-1", ticker="AAPL")

    def test_writes_upsert_with_expected_params(self, patch_get_db):
        # save_desk late-imports get_db, so the autouse patch_get_db (which
        # patches app.db.connection.get_db) covers it.
        import app.v3.desk_persistence as dp
        dp._TABLE_ENSURED = True  # skip the CREATE TABLE DDL noise

        from app.v3.desk_persistence import save_desk
        desk = self._make_desk()
        save_desk(desk)

        call = _find_call(patch_get_db, "INSERT INTO shared_desk")
        assert call is not None, "save_desk must INSERT into shared_desk"
        sql, params = call.args[0], call.args[1]
        assert "ON CONFLICT (desk_id) DO UPDATE" in sql, "must be an idempotent upsert"
        # params: [desk_id, cycle_id, ticker, phase, desk_data]
        assert params[0] == desk.desk_id
        assert params[1] == "cycle-test-1"
        assert params[2] == "AAPL"
        assert params[3] == desk.phase.value  # "INIT"
        # desk_data is JSON — must be a serialized string, not a dict.
        assert isinstance(params[4], str)
        assert "AAPL" in params[4]

    def test_idempotent_same_desk_id_across_calls(self, patch_get_db):
        import app.v3.desk_persistence as dp
        dp._TABLE_ENSURED = True

        from app.v3.desk_persistence import save_desk
        desk = self._make_desk()
        save_desk(desk)
        save_desk(desk)

        inserts = [
            c for c in patch_get_db.execute.call_args_list
            if c.args and "INSERT INTO shared_desk" in c.args[0]
        ]
        assert len(inserts) == 2, "both calls hit the writer"
        # Idempotency: the ON CONFLICT key (desk_id) is identical, so two calls
        # collapse to one logical row in Postgres.
        assert inserts[0].args[1][0] == inserts[1].args[1][0] == desk.desk_id


class TestTakeSnapshot:
    """take_snapshot appends a portfolio_snapshots row (peak-history feeder)."""

    def _patch_portfolio_db(self):
        cursor = MagicMock()
        cursor.execute.return_value = cursor
        cursor.fetchone.return_value = None
        cursor.fetchall.return_value = []
        cursor.__enter__ = MagicMock(return_value=cursor)
        cursor.__exit__ = MagicMock(return_value=False)

        @contextmanager
        def _get_db():
            yield cursor

        # portfolio.py imports get_db at module level → patch it there.
        return patch("app.trading.portfolio.get_db", _get_db), cursor

    def test_inserts_snapshot_row(self):
        import app.trading.portfolio as portfolio
        db_patch, cursor = self._patch_portfolio_db()
        state = {"cash": 20_000.0, "total_value": 105_000.0}

        with db_patch, \
             patch.object(portfolio, "get_current_state", return_value=state), \
             patch.object(portfolio, "_get_default_bot_id", return_value="bot-1"):
            result = portfolio.take_snapshot("bot-1")

        call = _find_call(cursor, "INSERT INTO portfolio_snapshots")
        assert call is not None, "take_snapshot must INSERT into portfolio_snapshots"
        params = call.args[1]
        # params: [id, bot_id, snapshot_ts, cash_balance, total_value]
        assert params[1] == "bot-1"
        assert params[3] == 20_000.0
        assert params[4] == 105_000.0
        assert result == state

    def test_not_idempotent_fresh_pk_each_call(self):
        import app.trading.portfolio as portfolio
        db_patch, cursor = self._patch_portfolio_db()
        state = {"cash": 20_000.0, "total_value": 105_000.0}

        with db_patch, \
             patch.object(portfolio, "get_current_state", return_value=state), \
             patch.object(portfolio, "_get_default_bot_id", return_value="bot-1"):
            portfolio.take_snapshot("bot-1")
            portfolio.take_snapshot("bot-1")

        inserts = [
            c for c in cursor.execute.call_args_list
            if c.args and "INSERT INTO portfolio_snapshots" in c.args[0]
        ]
        assert len(inserts) == 2
        # By design each call mints a fresh uuid PK → two distinct rows (this is
        # what builds the peak history the drawdown breaker reads).
        assert inserts[0].args[1][0] != inserts[1].args[1][0]

    def test_swallows_db_error(self):
        # take_snapshot logs and swallows DB errors (non-fatal), returning state.
        import app.trading.portfolio as portfolio

        def _boom():
            raise RuntimeError("db down")

        state = {"cash": 20_000.0, "total_value": 105_000.0}
        with patch("app.trading.portfolio.get_db", _boom), \
             patch.object(portfolio, "get_current_state", return_value=state), \
             patch.object(portfolio, "_get_default_bot_id", return_value="bot-1"):
            result = portfolio.take_snapshot("bot-1")

        assert result == state, "snapshot failure must not break the caller"
