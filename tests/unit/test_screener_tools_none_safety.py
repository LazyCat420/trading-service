"""Regression tests for screener_tools NoneType default safety."""
from unittest.mock import AsyncMock, patch
import pytest

from app.tools.screener_tools import screener_query


@pytest.mark.asyncio
async def test_screener_query_handles_none_total_matches():
    """Verify screener_query does not raise TypeError when payload['total'] is None."""
    mock_payload = {
        "total": None,
        "rows": [{"ticker": "AAPL", "market_cap": 3000000000}],
    }
    with patch("app.services.screener_client.screener_client.query", new=AsyncMock(return_value=mock_payload)):
        result = await screener_query(filters=["market_cap:gt:1000000"])
        assert result["total_matches"] is None
        assert result["returned"] == 1
        assert "rows" in result
