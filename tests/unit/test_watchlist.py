"""Watchlist tests against the Mongo read/write layer.

These used to patch `watchlist.get_db` and stub `db.execute(...).fetchone()`.
That mock is inert now: the module calls `mongo_query`/`mongo_store`, so a
patched `get_db` intercepts nothing and every read went to the LIVE database —
`add_ticker("AAPL")` found the real row, took the reactivate branch, and
returned False. The tests failed for the right reason.

They patch the Mongo functions now. `find_row` returns a TUPLE in the column
order the caller asked for, exactly as `cursor.fetchone()` did, so the stub
values below are unchanged from the Postgres versions — that shape-compatibility
is the whole point of `app/db/mongo_query.py`.
"""
import pytest
from unittest.mock import ANY, MagicMock, patch

from app.trading.watchlist import (
    add_ticker,
    remove_ticker,
    auto_purge_ticker,
    pause_ticker,
    resume_ticker,
    ban_ticker,
    is_banned,
    check_ban_patterns,
    get_paused,
)


@pytest.fixture
def mq():
    """Patch the module's Mongo read + write surface together.

    Both are needed: a test that stubs only the read lets the write reach the
    real database, which is how a unit test quietly becomes an integration
    test that mutates production data.
    """
    with patch("app.trading.watchlist.mongo_query") as q, \
         patch("app.trading.watchlist.mongo_store") as s:
        yield q, s


@patch("app.trading.watchlist.is_banned", return_value=False)
def test_add_ticker_success(mock_is_banned, mq):
    q, s = mq
    q.find_row.return_value = None          # not already on the watchlist
    assert add_ticker("AAPL", source="test") is True
    s.insert_docs.assert_called_once_with(
        "watchlist",
        [{"ticker": "AAPL", "source": "test", "notes": "",
          "added_at": ANY, "status": "active"}],
    )


@patch("app.trading.watchlist.is_banned", return_value=False)
def test_add_ticker_reactivates_existing(mock_is_banned, mq):
    q, s = mq
    q.find_row.return_value = ("AAPL", "removed")
    assert add_ticker("AAPL") is False       # already known -> reactivated
    s.update_docs.assert_called_once()
    s.insert_docs.assert_not_called()


@patch("app.trading.watchlist.is_banned", return_value=True)
def test_add_ticker_banned(mock_is_banned):
    assert add_ticker("AAPL") is False


def test_remove_ticker(mq):
    q, s = mq
    q.find_row.return_value = ("AAPL",)
    assert remove_ticker("AAPL") is True

    q.find_row.return_value = None
    assert remove_ticker("INVALID") is False


def test_auto_purge_ticker(mq):
    q, s = mq
    q.find_row.return_value = ("AAPL",)
    assert auto_purge_ticker("AAPL", "Low confidence") is True


def test_pause_resume_ticker(mq):
    q, s = mq
    q.find_row.return_value = ("AAPL",)
    assert pause_ticker("AAPL") is True
    assert resume_ticker("AAPL") is True


def test_get_paused(mq):
    q, s = mq
    q.find_rows.return_value = [
        ("AAPL", "manual", "Notes", None, "user paused")
    ]
    paused = get_paused()
    assert len(paused) == 1
    assert paused[0]["ticker"] == "AAPL"
    assert paused[0]["status_reason"] == "user paused"


@patch("app.trading.watchlist._snapshot_market_data", return_value=(None, 0.5, None))
def test_check_ban_patterns(mock_snapshot, mq):
    q, s = mq
    q.find_rows.return_value = [("penny_stock", '{"price_lt": 1.0}')]
    assert check_ban_patterns("PENN") == "penny_stock"
