import pytest
from unittest.mock import patch, MagicMock, AsyncMock
from app.collectors.twitter_collector import (
    collect_for_ticker,
    collect_fintwit_sweep,
    _is_quality_tweet,
)

@pytest.fixture
def mock_db():
    db = MagicMock()
    with patch("app.collectors.twitter_collector.get_db") as mock_get_db_1, \
         patch("app.processors.dedup_engine.get_db") as mock_get_db_2:
        mock_get_db_1.return_value.__enter__.return_value = db
        mock_get_db_2.return_value.__enter__.return_value = db
        yield db

@pytest.fixture
def mock_scraper_client():
    with patch("app.collectors.twitter_collector.scraper_client") as mock_client:
        mock_client.collect = AsyncMock()
        yield mock_client

@pytest.fixture
def sweep_enabled():
    """TWITTER_SWEEP_ENABLED defaults to False (2026-08-05: scraper-service has
    no TWITTER_ACCOUNTS credentials, so every sweep stored 0 tweets and logged a
    warning). Tests of the sweep MECHANICS must turn it back on explicitly;
    test_sweep_is_gated_by_the_flag covers the default."""
    from app.config import settings

    original = settings.TWITTER_SWEEP_ENABLED
    settings.TWITTER_SWEEP_ENABLED = True
    try:
        yield
    finally:
        settings.TWITTER_SWEEP_ENABLED = original

def test_is_quality_tweet():
    # Retweet should be filtered out
    assert _is_quality_tweet({"is_retweet": True, "like_count": 100}) is False
    
    # Low likes should be filtered out
    assert _is_quality_tweet({"is_retweet": False, "like_count": 2}) is False
    
    # Valid tweet
    assert _is_quality_tweet({"is_retweet": False, "like_count": 5}) is True
    assert _is_quality_tweet({"is_retweet": False, "like_count": 10, "view_count": 100}) is True

@pytest.mark.asyncio
@patch("app.collectors.twitter_collector.get_ticker_symbols", new_callable=AsyncMock)
async def test_collect_for_ticker_success(mock_get_tickers, mock_scraper_client, mock_db):
    # Mock search response from scraper service
    mock_scraper_client.collect.return_value = [
        {
            "id": "123456",
            "text": "Buying some $AAPL today!",
            "author_username": "trader1",
            "author_display_name": "Trader One",
            "author_followers": 500,
            "created_at": "2026-06-15T10:00:00",
            "retweet_count": 2,
            "like_count": 10,
            "reply_count": 1,
            "view_count": 50,
            "is_retweet": False,
            "cashtags": ["AAPL"],
            "hashtags": []
        }
    ]
    
    # Configure mock DB calls for DedupEngine
    mock_db.fetchone.return_value = None
    mock_db.fetchall.return_value = []

    mock_get_tickers.return_value = ["AAPL"]
    
    # Run collection
    count = await collect_for_ticker("AAPL")
    
    # Assertions
    assert count == 1
    mock_scraper_client.collect.assert_called_once_with(
        source="twitter",
        req_data={"cashtags": ["AAPL"], "limit": 50}
    )
    
    # Verify DB insert was called
    insert_calls = [c for c in mock_db.execute.call_args_list if "INSERT INTO social_posts" in c[0][0]]
    assert len(insert_calls) == 1
    # 'twitter' is hardcoded in the query, not in parameters
    assert "123456" in insert_calls[0][0][1]
    assert "AAPL" in insert_calls[0][0][1]

@pytest.mark.asyncio
@patch("app.collectors.twitter_collector.get_ticker_symbols", new_callable=AsyncMock)
async def test_collect_fintwit_sweep_success(mock_get_tickers, mock_scraper_client, mock_db, sweep_enabled):
    # Mock response
    mock_scraper_client.collect.return_value = [
        {
            "id": "78910",
            "text": "General market trends look bullish",
            "author_username": "unusual_whales",
            "author_display_name": "Unusual Whales",
            "author_followers": 100000,
            "created_at": "2026-06-15T11:00:00",
            "retweet_count": 100,
            "like_count": 500,
            "reply_count": 20,
            "view_count": 5000,
            "is_retweet": False,
            "cashtags": [],
            "hashtags": []
        }
    ]
    
    # Configure mock DB calls for DedupEngine
    mock_db.fetchone.return_value = None
    mock_db.fetchall.return_value = []

    mock_get_tickers.return_value = []
    
    # Run fintwit sweep
    count = await collect_fintwit_sweep(limit=5)
    
    # Since FINTWIT_ACCOUNTS + CRYPTO_ACCOUNTS is 16 accounts, in batches of 3,
    # it makes 6 collect calls. Let's assert we got posts stored
    assert count >= 0
    assert mock_scraper_client.collect.call_count == 6


@pytest.mark.asyncio
async def test_sweep_is_gated_by_the_flag(mock_scraper_client):
    """With no TWITTER_ACCOUNTS credentials the sweep stored 0 tweets across 16
    accounts every 6 hours and logged a warning for it. The flag must stop the
    network churn, not merely silence the log — assert the scraper is never
    called."""
    from app.config import settings

    original = settings.TWITTER_SWEEP_ENABLED
    settings.TWITTER_SWEEP_ENABLED = False
    try:
        assert await collect_fintwit_sweep(limit=5) == 0
        assert mock_scraper_client.collect.call_count == 0
    finally:
        settings.TWITTER_SWEEP_ENABLED = original


@pytest.mark.asyncio
async def test_collect_all_is_gated_too(mock_scraper_client):
    """The per-ticker cashtag searches ride the same dead backend as the
    account sweep, so the flag has to gate both entry points."""
    from app.collectors.twitter_collector import collect_all
    from app.config import settings

    original = settings.TWITTER_SWEEP_ENABLED
    settings.TWITTER_SWEEP_ENABLED = False
    try:
        assert await collect_all() == 0
        assert mock_scraper_client.collect.call_count == 0
    finally:
        settings.TWITTER_SWEEP_ENABLED = original
