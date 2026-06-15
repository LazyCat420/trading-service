import pytest
from unittest.mock import patch, MagicMock
import datetime

from app.collectors.stocktwits_collector import (
    collect_for_ticker,
)

@pytest.fixture
def mock_db():
    db = MagicMock()
    with patch("app.collectors.stocktwits_collector.get_db") as mock_get_db_1, \
         patch("app.processors.dedup_engine.get_db") as mock_get_db_2:
        mock_get_db_1.return_value.__enter__.return_value = db
        mock_get_db_2.return_value.__enter__.return_value = db
        
        db.fetchone.return_value = None
        db.fetchall.return_value = []
        
        cursor = MagicMock()
        cursor.fetchone.return_value = None
        cursor.fetchall.return_value = []
        db.execute.return_value = cursor
        
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

