"""StockTwits collector tests, ported off the inert `get_db` mock.

The old fixture patched `get_db` on BOTH `stocktwits_collector` and
`processors.dedup_engine`. Only the first target still exists — this collector
`DedupEngine` was migrated to Mongo, so `dedup_engine.get_db` no longer exists
and every duplicate check ran against the LIVE Mongo database. The dedup half
patches `dedup_engine.mongo_store` (count_docs for the content-hash tier,
find_docs for the Jaccard/title tiers); the collector's own batch write moved
off `get_db().executemany(...)` onto
`mongo_store.insert_docs('social_posts', [...])`, so that is patched too and
the assertion checks the actual document field values rather than only that a
call happened.
"""
import pytest
from unittest.mock import patch, MagicMock

from app.collectors.stocktwits_collector import (
    collect_for_ticker,
)


@pytest.fixture
def mock_db():
    """Mongo write surface of the collector + Mongo read surface of dedup."""
    db = MagicMock()
    with patch("app.collectors.stocktwits_collector.mongo_store") as store, \
         patch("app.processors.dedup_engine.mongo_store") as dedup_store:
        db.mongo_store = store

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
    mock_db.mongo_store.insert_docs.assert_called_once()

    collection, docs = mock_db.mongo_store.insert_docs.call_args[0]
    assert collection == "social_posts"
    assert len(docs) == 1
    doc = docs[0]
    assert doc["platform"] == "stocktwits"
    assert doc["platform_post_id"] == "12345"
    assert doc["ticker"] == "AAPL"
    assert doc["author_username"] == "trader1"
    assert doc["author_display_name"] == "Trader One"
    assert doc["author_followers"] == 150
    assert doc["content"] == "AAPL looks great today!"
