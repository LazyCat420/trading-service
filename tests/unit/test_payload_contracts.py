# tests/unit/test_payload_contracts.py

import pytest
from app.utils.payload_gate import gate_check, InsufficientDataError

def test_market_data_valid():
    payload = {"ticker": "AAPL", "price": 150.0, "volume": 1000000}
    assert gate_check(payload, "market_data") == payload

def test_market_data_missing_fields():
    payload = {"ticker": "AAPL", "price": 150.0}  # missing volume
    with pytest.raises(InsufficientDataError) as exc_info:
        gate_check(payload, "market_data")
    assert "missing: ['volume']" in str(exc_info.value)

def test_bull_bear_valid():
    payload = {
        "ticker": "AAPL",
        "thesis": "Bullish growth",
        "confidence": 80,
        "supporting_data": ["RSI is low"]
    }
    assert gate_check(payload, "bull_bear") == payload

def test_bull_bear_missing_fields():
    payload = {"ticker": "AAPL", "thesis": "Bullish growth"}
    with pytest.raises(InsufficientDataError) as exc_info:
        gate_check(payload, "bull_bear")
    assert "confidence" in exc_info.value.missing
    assert "supporting_data" in exc_info.value.missing

def test_debate_valid():
    payload = {"bull_case": "Strong growth", "bear_case": "High debt"}
    assert gate_check(payload, "debate") == payload

def test_synthesis_valid():
    payload = {
        "net_signal": "BUY",
        "confidence": 85,
        "bull_case": "Strong growth",
        "bear_case": "High debt"
    }
    assert gate_check(payload, "synthesis") == payload

def test_synthesis_missing_fields():
    payload = {
        "net_signal": "BUY",
        "confidence": 85,
        "bull_case": ""  # empty string is treated as missing
    }
    with pytest.raises(InsufficientDataError) as exc_info:
        gate_check(payload, "synthesis")
    assert "bull_case" in exc_info.value.missing
    assert "bear_case" in exc_info.value.missing

def test_data_missing_status():
    payload = {"ticker": "AAPL", "price": 150.0, "volume": 1000000, "status": "DATA_MISSING", "missing_fields": ["volume"]}
    with pytest.raises(InsufficientDataError) as exc_info:
        gate_check(payload, "market_data")
    assert exc_info.value.missing == ["volume"]

def test_proceed_false():
    payload = {"ticker": "AAPL", "price": 150.0, "volume": 1000000, "proceed": False}
    with pytest.raises(InsufficientDataError):
        gate_check(payload, "market_data")

def test_data_missing_value_prefix():
    payload = {"ticker": "AAPL", "price": "DATA_MISSING_PRICE", "volume": 1000000}
    with pytest.raises(InsufficientDataError) as exc_info:
        gate_check(payload, "market_data")
    assert "price" in exc_info.value.missing

def test_non_dict_payload():
    with pytest.raises(InsufficientDataError):
        gate_check("not a dict", "market_data")
