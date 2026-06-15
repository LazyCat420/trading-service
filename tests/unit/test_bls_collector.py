import pytest
from unittest.mock import patch, MagicMock
import datetime

from app.collectors.bls_collector import (
    collect_bls_series,
    collect_all,
    parse_bls_date,
)

@pytest.fixture
def mock_db():
    with patch("app.collectors.bls_collector.get_db") as mock_get_db:
        db = MagicMock()
        db.fetchone.return_value = None
        db.fetchall.return_value = []
        mock_get_db.return_value.__enter__.return_value = db
        yield db

def test_parse_bls_date():
    assert parse_bls_date("2026", "M05") == datetime.date(2026, 5, 1)
    assert parse_bls_date("2026", "Q03") == datetime.date(2026, 7, 1)
    assert parse_bls_date("2026", "A01") == datetime.date(2026, 12, 31)

@pytest.mark.asyncio
@patch("httpx.AsyncClient.post")
async def test_collect_bls_series_success(mock_post, mock_db):
    mock_resp = MagicMock()
    mock_resp.json.return_value = {
        "status": "REQUEST_SUCCEEDED",
        "Results": {
            "series": [
                {
                    "seriesID": "CUUR0000SA0",
                    "data": [
                        {"year": "2026", "period": "M05", "value": "314.123"}
                    ]
                }
            ]
        }
    }
    mock_resp.raise_for_status = MagicMock()
    mock_post.return_value = mock_resp

    count = await collect_bls_series()
    assert count == 1
    mock_db.executemany.assert_called_once()
