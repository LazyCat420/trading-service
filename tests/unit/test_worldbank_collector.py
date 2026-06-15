import pytest
from unittest.mock import patch, MagicMock
import datetime

from app.collectors.worldbank_collector import (
    collect_indicators,
    collect_all,
)

@pytest.fixture
def mock_db():
    with patch("app.collectors.worldbank_collector.get_db") as mock_get_db:
        db = MagicMock()
        db.fetchone.return_value = None
        db.fetchall.return_value = []
        mock_get_db.return_value.__enter__.return_value = db
        yield db

@pytest.mark.asyncio
@patch("httpx.AsyncClient.get")
async def test_collect_indicators_success(mock_get, mock_db):
    mock_resp = MagicMock()
    mock_resp.json.return_value = [
        {"page": 1, "pages": 1, "per_page": "100", "total": 1},
        [
            {"date": "2025", "value": 2.5},
            {"date": "2024", "value": 2.1},
        ]
    ]
    mock_resp.raise_for_status = MagicMock()
    mock_get.return_value = mock_resp

    count = await collect_indicators(country="US", years=3)
    # 2 rows for GDP_GROWTH, 2 for INFLATION, 2 for UNEMPLOYMENT, 2 for CURRENT_ACCT = 8 total inserts expected
    assert count == 8
    assert mock_db.executemany.call_count == 4
