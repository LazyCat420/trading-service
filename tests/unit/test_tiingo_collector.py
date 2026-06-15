import pytest
from unittest.mock import patch, MagicMock
import datetime

from app.collectors.tiingo_collector import (
    collect_eod_prices,
    collect_all,
)

@pytest.fixture
def mock_db():
    with patch("app.collectors.tiingo_collector.get_db") as mock_get_db:
        db = MagicMock()
        db.fetchone.return_value = None
        db.fetchall.return_value = []
        mock_get_db.return_value.__enter__.return_value = db
        yield db

@pytest.mark.asyncio
@patch("httpx.AsyncClient.get")
async def test_collect_eod_prices_success(mock_get, mock_db):
    mock_resp = MagicMock()
    mock_resp.json.return_value = [
        {"date": "2026-05-15T00:00:00.000Z", "open": 180.0, "high": 182.0, "low": 179.0, "close": 181.0, "volume": 1000000},
    ]
    mock_resp.raise_for_status = MagicMock()
    mock_get.return_value = mock_resp

    with patch("app.config.settings.TIINGO_API_KEY", "test_key"):
        count = await collect_eod_prices("AAPL")
        assert count == 1
        mock_db.executemany.assert_called_once()

@pytest.mark.asyncio
async def test_collect_eod_prices_missing_key(mock_db):
    with patch("app.config.settings.TIINGO_API_KEY", ""):
        count = await collect_eod_prices("AAPL")
        assert count == 0
        mock_db.executemany.assert_not_called()
