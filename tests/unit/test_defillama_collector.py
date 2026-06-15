import pytest
from unittest.mock import patch, MagicMock, AsyncMock
import datetime

from app.collectors.defillama_collector import (
    collect_protocol_tvl,
    collect_stablecoin_supply,
    collect_yields,
    collect_all,
)

@pytest.fixture
def mock_db():
    with patch("app.collectors.defillama_collector.get_db") as mock_get_db:
        db = MagicMock()
        db.fetchone.return_value = None
        db.fetchall.return_value = []
        mock_get_db.return_value.__enter__.return_value = db
        yield db

@pytest.mark.asyncio
@patch("httpx.AsyncClient.get")
async def test_collect_protocol_tvl_success(mock_get, mock_db):
    mock_resp = MagicMock()
    mock_resp.json.return_value = [
        {"name": "MakerDAO", "tvl": 5000000000.0},
        {"name": "Lido", "tvl": 10000000000.0},
    ]
    mock_resp.raise_for_status = MagicMock()
    mock_get.return_value = mock_resp

    count = await collect_protocol_tvl(limit=2)
    assert count == 2
    mock_db.executemany.assert_called_once()

@pytest.mark.asyncio
@patch("httpx.AsyncClient.get")
async def test_collect_stablecoin_supply_success(mock_get, mock_db):
    mock_resp = MagicMock()
    mock_resp.json.return_value = {
        "peggedAssets": [
            {"symbol": "USDT", "circulating": {"peggedUSD": 1000000000.0}},
            {"symbol": "USDC", "circulating": {"peggedUSD": 500000000.0}},
        ]
    }
    mock_resp.raise_for_status = MagicMock()
    mock_get.return_value = mock_resp

    count = await collect_stablecoin_supply()
    assert count == 2
    mock_db.executemany.assert_called_once()

@pytest.mark.asyncio
@patch("httpx.AsyncClient.get")
async def test_collect_yields_success(mock_get, mock_db):
    mock_resp = MagicMock()
    mock_resp.json.return_value = {
        "data": [
            {"project": "aave", "symbol": "USDT", "tvlUsd": 100000.0, "apy": 5.5},
            {"project": "compound", "symbol": "USDC", "tvlUsd": 200000.0, "apy": 4.2},
        ]
    }
    mock_resp.raise_for_status = MagicMock()
    mock_get.return_value = mock_resp

    count = await collect_yields(limit=2)
    assert count == 2
    mock_db.executemany.assert_called_once()
