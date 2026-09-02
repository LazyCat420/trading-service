"""Regression tests for options_collector NaN safety."""
from unittest.mock import MagicMock, patch
import pandas as pd
import pytest

from app.collectors.options_collector import _fetch_options


def test_options_collector_handles_all_nan_volume():
    """Verify options collector does not crash with ValueError when volume has NaNs."""
    mock_ticker = MagicMock()
    mock_ticker.options = ("2026-09-18",)
    
    mock_chain = MagicMock()
    mock_chain.calls = pd.DataFrame({
        "strike": [100.0, 105.0],
        "volume": [float("nan"), float("nan")],
        "openInterest": [10, 20],
    })
    mock_chain.puts = pd.DataFrame({
        "strike": [95.0, 90.0],
        "volume": [float("nan"), 5.0],
        "openInterest": [5, float("nan")],
    })
    mock_ticker.option_chain.return_value = mock_chain

    with patch("yfinance.Ticker", return_value=mock_ticker):
        result = _fetch_options("AAPL")
        assert result is not None
        assert result["total_call_volume"] == 0
        assert result["total_put_volume"] == 5
        assert result["total_call_oi"] == 30
        assert result["total_put_oi"] == 5
