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

    # Session injection was removed in d788c27 — newer yfinance manages its own
    # (curl_cffi) session and breaks when handed a requests.Session
    mock_ticker.assert_called_once_with("AAPL")

@patch("app.db.connection.get_db")
def test_tool_registry_log_usage_explicit_called_at(mock_get_db):
    """Verify that _log_usage passes called_at to the database execute statement."""
    mock_db = MagicMock()
    mock_get_db.return_value.__enter__.return_value = mock_db
    
    from app.tools.registry import registry
    from app.tools.tool_context import clear_tool_context, set_tool_context

    # Attribution reaches the row through the CONTEXT, not the call signature.
    # The SDK's _log_usage accepts ticker/cycle_id but invokes the telemetry
    # callback with only five positional args (lazycat/tool_registry.py:848),
    # dropping both on the way — so context is the only channel that survives,
    # and app/routers/agent_tools_router.py populates it per request. Setting it
    # here mirrors production rather than testing a path that cannot occur.
    clear_tool_context()
    set_tool_context(agent_name="test_agent", cycle_id="cycle-123", ticker="AAPL")
    try:
        registry._log_usage(
            tool_name="test_tool",
            agent_name="test_agent",
            ticker="AAPL",
            cycle_id="cycle-123",
            success=True,
            execution_ms=100,
            error_message=None,
        )
    finally:
        clear_tool_context()
    
    mock_db.execute.assert_called_once()
    args, kwargs = mock_db.execute.call_args
    sql = args[0]
    params = args[1]
    
    # Check that called_at is in the columns
    assert "called_at" in sql.lower()
    # Placeholders must match parameters. Asserted against each other rather
    # than a literal count: the literal was 7, and it failed the moment the
    # INSERT legitimately grew to carry ticker and cycle_id (2026-07-29), even
    # though the property this test exists to protect — an explicit called_at —
    # was never at risk. Self-consistency still catches the real bug class (a
    # column added without its parameter, or vice versa).
    assert sql.count("%s") == len(params)

    # Attribution columns must be WRITTEN, not just present in the schema.
    # Measured before the fix: all 2,066 rows over 7 days had
    # agent_name='unknown' with ticker and cycle_id NULL, because the INSERT
    # omitted both columns entirely — so "which agent researches?" could not
    # be answered at all.
    for col in ("agent_name", "ticker", "cycle_id"):
        assert col in sql.lower(), f"{col} missing from the INSERT"
    assert "AAPL" in params, "ticker must reach the row"
    assert "cycle-123" in params, "cycle_id must reach the row"

    # Check that the last parameter is a datetime object
    assert isinstance(params[-1], datetime)

@patch("app.processors.data_sanity.get_db")
def test_data_sanity_checks_returns_correct_warnings(mock_get_db):
    """Verify that run_sanity_checks returns warnings when DB queries yield invalid data."""
    mock_db = MagicMock()
    mock_get_db.return_value.__enter__.return_value = mock_db
    
    # Configure mock db queries
    # First: sec_13f_holdings max usd > 1e12 (e.g. 2e12)
    # Second: holdings with negative value_usd count (e.g. 5)
    # Third: holdings with 0 shares but positive value count (e.g. 3)
    # Fourth: Berkshire AAPL value_usd (e.g. 10e9 -> expected > 30B)
    # Fifth: fundamentals AAPL market cap (e.g. 500e9 -> expected > 1T)
    # Sixth: negative market caps count (e.g. 0)
    # Seventh: extreme P/E ratios (e.g. [])
    # Eighth: close <= 0 count (e.g. 0)
    # Ninth: absurd single-day moves (e.g. [])
    # Tenth: congress trades chamber distinct query (e.g. [("House",)]) -> missing Senate
    # Eleventh: technicals RSI outside 0-100 count (e.g. 2)
    
    mock_db.execute.return_value.fetchone.side_effect = [
        (2e12,),       # 1. 13F position > $1T
        (5,),          # 2. negative value count
        (3,),          # 3. 0 shares positive value count
        (10e9,),       # 4. Berkshire AAPL under $30B
        (500e9,),      # 5. AAPL market cap under $1T
        (0,),          # 6. negative market caps count
        (0,),          # 8. close <= 0 count
        (2,),          # 11. RSI outside 0-100 count
    ]
    
    mock_db.execute.return_value.fetchall.side_effect = [
        [],            # 7. PE ratios (fundamentals)
        [],            # 9. Absurd moves (prices)
        [("House",)],  # 10. Congress trades chambers
    ]
    
    from app.processors.data_sanity import run_sanity_checks
    
    failures = run_sanity_checks()
    
    assert len(failures) > 0
    # Confirm specific failures are present
    assert any("13F: Max position value" in f for f in failures)
    assert any("Berkshire AAPL" in f for f in failures)
    assert any("AAPL market cap" in f for f in failures)
    assert any("missing Senate data" in f for f in failures)
    assert any("RSI outside 0-100" in f for f in failures)

