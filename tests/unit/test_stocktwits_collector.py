"""StockTwits collector tests, ported off the inert `get_db` mock.

The old fixture patched `get_db` on BOTH `stocktwits_collector` and
`processors.dedup_engine`. Only the first target still exists — this collector
does still write its batch through `get_db().executemany(...)` — but
`DedupEngine` was migrated to Mongo, so `dedup_engine.get_db` no longer exists
and every duplicate check ran against the LIVE Mongo database. The dedup half
patches `dedup_engine.mongo_store` now (count_docs for the content-hash tier,
find_docs for the Jaccard/title tiers), while the collector's own Postgres
write keeps its `get_db` mock, and the row assertion checks the actual column
values rather than only that a call happened.
"""
import pytest
from unittest.mock import patch, MagicMock

from app.collectors.stocktwits_collector import (
    collect_for_ticker,
)


@pytest.fixture
def mock_db():
    """Postgres write surface of the collector + Mongo read surface of dedup."""
    db = MagicMock()
    with patch("app.collectors.stocktwits_collector.get_db") as mock_get_db, \
         patch("app.processors.dedup_engine.mongo_store") as dedup_store:
        mock_get_db.return_value.__enter__.return_value = db

        db.fetchone.return_value = None
        db.fetchall.return_value = []

        cursor = MagicMock()
        cursor.fetchone.return_value = None
        cursor.fetchall.return_value = []
        db.execute.return_value = cursor

        # Nothing on record => nothing is a duplicate.
        dedup_store.count_docs.return_value = 0
        dedup_store.find_docs.return_value = []

        yield db


@pytest.mark.asyncio
@patch("app.services.scraper_client.scraper_client.collect")
async def test_collect_for_ticker_success(mock_collect, mock_db):
    mock_collect.return_value = [
        {
            "id": "12345",
            "body": "AAPL looks great today!",
            "username": "trader1",
            "display_name": "Trader One",
            "followers": 150,
            "sentiment": "Bullish",
            "created_at": "2026-06-15T18:00:00Z"
        }
    ]

    count = await collect_for_ticker("AAPL")
    assert count == 1
    mock_db.executemany.assert_called_once()

    sql, rows = mock_db.executemany.call_args[0]
    assert "INSERT INTO social_posts" in sql
    assert len(rows) == 1
    row = rows[0]
    # Column order is pinned by the INSERT above: id, platform, platform_post_id,
    # ticker, author_username, author_display_name, author_followers, content, ...
    assert row[1] == "stocktwits"
    assert row[2] == "12345"
    assert row[3] == "AAPL"
    assert row[4] == "trader1"
    assert row[5] == "Trader One"
    assert row[6] == 150
    assert row[7] == "AAPL looks great today!"
