import pytest
from unittest.mock import patch, MagicMock
import requests
from datetime import datetime

from app.collectors.yfinance_collector import _yf_session, fetch_ohlcv_dataframe

def test_yfinance_session_configuration():
    """Verify that _yf_session is a valid requests.Session with a TimeoutHTTPAdapter."""
    assert isinstance(_yf_session, requests.Session)
    
    # Check that HTTPAdapter is mounted and is a timeout adapter
    https_adapter = _yf_session.adapters.get("https://")
    http_adapter = _yf_session.adapters.get("http://")
    
    assert https_adapter is not None
    assert http_adapter is not None
    assert hasattr(https_adapter, "timeout")
    assert https_adapter.timeout == 15.0
    assert http_adapter.timeout == 15.0

@pytest.mark.asyncio
@patch("app.collectors.yfinance_collector.yf.Ticker")
async def test_yfinance_collector_uses_session(mock_ticker):
    """Verify that yf.Ticker is instantiated with the custom session."""
    mock_ticker_inst = MagicMock()
    mock_ticker_inst.history.return_value = None
    mock_ticker.return_value = mock_ticker_inst
    
    await fetch_ohlcv_dataframe("AAPL")
    
    mock_ticker.assert_called_once_with("AAPL", session=_yf_session)

@patch("app.db.connection.get_db")
def test_tool_registry_log_usage_explicit_called_at(mock_get_db):
    """Verify that _log_usage passes called_at to the database execute statement."""
    mock_db = MagicMock()
    mock_get_db.return_value.__enter__.return_value = mock_db
    
    from app.tools.registry import registry
    
    registry._log_usage(
        tool_name="test_tool",
        agent_name="test_agent",
        ticker="AAPL",
        cycle_id="cycle-123",
        success=True,
        execution_ms=100,
        error_message=None,
    )
    
    mock_db.execute.assert_called_once()
    args, kwargs = mock_db.execute.call_args
    sql = args[0]
    params = args[1]
    
    # Check that called_at is in the columns
    assert "called_at" in sql.lower()
    # Check that there are 9 placeholders/parameters (including called_at)
    assert sql.count("%s") == 9
    assert len(params) == 9
    
    # Check that the last parameter is a datetime object
    assert isinstance(params[-1], datetime)
