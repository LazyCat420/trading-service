"""Portfolio state, snapshots, equity curve and performance summary.

These used to patch `portfolio.get_db` and dispatch a `db.execute` mock on the
SQL text (`"FROM bots" in query`). `app/trading/portfolio.py` calls
`mongo_query`/`mongo_store` now, so a patched `get_db` intercepts nothing: the
mock was inert, the module talked to the LIVE database, and the assertions were
scored against whatever production happened to hold.

They patch the Mongo helpers now, dispatching on the COLLECTION name the module
asks for. `find_row` returns a TUPLE in the column order the caller listed and
`find_rows` a list of them — that positional contract is the whole point of
`app/db/mongo_query.py`, so the fixtures return tuples, not documents.
"""
from unittest.mock import patch, MagicMock

from app.trading.portfolio import (
    get_current_state,
    take_snapshot,
    get_equity_curve,
    get_performance_summary
)
from app.config import settings


def _mongo(find_row=None, find_rows=None):
    """Patch portfolio's Mongo layer, dispatching on collection name.

    Both halves are patched together. Stubbing only the read leaves writes
    pointed at the real store, which is how a "unit" test comes to insert a
    row into production.
    """
    q = MagicMock()
    q.find_row.side_effect = lambda coll, *a, **k: (find_row or {}).get(coll)
    q.find_rows.side_effect = lambda coll, *a, **k: (find_rows or {}).get(coll, [])
    return patch("app.trading.portfolio.mongo_query", q), q


@patch("app.trading.portfolio._get_default_bot_id", return_value="bot1")
def test_get_current_state_empty(mock_default_bot):
    ctx, q = _mongo()
    with ctx:
        state = get_current_state()

    assert state["bot_id"] == "bot1"
    assert state["cash"] == settings.STARTING_CASH
    assert state["total_value"] == settings.STARTING_CASH
    assert state["position_count"] == 0


@patch("app.trading.portfolio._get_default_bot_id", return_value="bot1")
def test_get_current_state_with_positions(mock_default_bot):
    ctx, q = _mongo(
        find_row={
            "bots": (5000.0, 100.0, 5),                 # cash, pnl, trades
            "portfolio_snapshots": ("2023-10-01T00:00:00Z",),
            "price_history": (160.0,),                  # current price
            "ticker_metadata": ("Tech", "Mega", 2000000.0),
            "fundamentals": (15.0, 0.1),
            "technicals": (50.0,),
        },
        find_rows={
            # AAPL: 10 shares @ $150
            "positions": [("AAPL", 10.0, 150.0, 0.05)],
        },
    )
    with ctx:
        state = get_current_state()

    assert state["cash"] == 5000.0
    assert state["total_value"] == 5000.0 + (10.0 * 160.0)  # 5000 + 1600 = 6600
    assert state["position_count"] == 1
    assert state["positions"][0]["ticker"] == "AAPL"
    assert state["positions"][0]["current_price"] == 160.0


@patch("app.trading.portfolio._get_default_bot_id", return_value="bot1")
def test_get_current_state_price_sanity(mock_default_bot):
    ctx, q = _mongo(
        find_row={
            "bots": (5000.0, 0, 0),
            "portfolio_snapshots": None,
            # Phantom price! 2000 vs 150 > 10x
            "price_history": (2000.0,),
            "ticker_metadata": None,
            "fundamentals": None,
            "technicals": None,
        },
        find_rows={"positions": [("AAPL", 10.0, 150.0, 0.05)]},
    )
    with ctx:
        state = get_current_state()

    # Should fallback to entry price of 150
    assert state["positions"][0]["current_price"] == 150.0
    assert state["total_value"] == 5000.0 + 1500.0


@patch("app.trading.portfolio.get_current_state")
def test_take_snapshot(mock_get_current_state):
    mock_get_current_state.return_value = {"cash": 1000.0, "total_value": 2000.0}

    with patch("app.trading.portfolio.mongo_store") as store:
        res = take_snapshot("bot1")

    assert res["total_value"] == 2000.0
    store.insert_docs.assert_called_once()
    collection, docs = store.insert_docs.call_args[0][:2]
    assert collection == "portfolio_snapshots"
    assert docs[0]["bot_id"] == "bot1"


def test_get_equity_curve():
    now = "2023-10-01T00:00:00Z"
    # (total_value, cash_balance, snapshot_ts, realized_pnl, unrealized_pnl).
    # The P&L columns joined the SELECT on 2026-07-26 — they were in the schema
    # and never written, so the equity curve could not be decomposed into
    # "trades we closed" vs "marks that moved".
    ctx, q = _mongo(find_rows={"portfolio_snapshots": [(1000.0, 500.0, now, 42.0, -7.5)]})
    with ctx:
        curve = get_equity_curve("bot1")

    assert len(curve) == 1
    assert curve[0]["total_value"] == 1000.0
    assert curve[0]["realized_pnl"] == 42.0
    assert curve[0]["unrealized_pnl"] == -7.5


def test_get_equity_curve_reports_missing_pnl_as_none():
    """Rows written before 2026-07-26 carry NULL. Surfacing that as 0.0 would
    claim the book made nothing, when the truth is nobody recorded it."""
    ctx, q = _mongo(
        find_rows={
            "portfolio_snapshots": [(1000.0, 500.0, "2023-10-01T00:00:00Z", None, None)]
        }
    )
    with ctx:
        curve = get_equity_curve("bot1")

    assert curve[0]["realized_pnl"] is None
    assert curve[0]["unrealized_pnl"] is None


@patch("app.trading.portfolio.get_current_state")
@patch("app.services.bot_manager.get_bot_starting_cash", return_value=10000.0)
def test_get_performance_summary(mock_starting_cash, mock_get_current_state):
    mock_get_current_state.return_value = {
        "cash": 5000.0, "total_value": 15000.0, "position_count": 2
    }
    # trades, realized, win rate
    ctx, q = _mongo(find_row={"bots": (10, 5000.0, 0.6)})
    with ctx:
        summary = get_performance_summary("bot1")

    assert summary["pnl"] == 5000.0  # 15000 - 10000
    assert summary["pnl_pct"] == 50.0
    assert summary["total_trades"] == 10
    assert summary["win_rate"] == 0.6
