"""
Unit tests for deterministic data-readiness evaluator.
"""
from app.v3.data_readiness import evaluate_ticker_readiness


def test_clean_ticker_is_ready():
    res = evaluate_ticker_readiness(
        ticker="NVDA",
        data_report="### PRICE & PROFILE\nSpot: $120.50\n### TECHNICAL INDICATORS\nRSI: 55.4",
        technical_context="RSI: 55.4, SMA20: $118.0",
        valuation_context="EV/EBITDA: 35.2",
        price_age_trading_days=0,
    )
    assert res.is_ready is True
    assert res.disposition == "PROCEED"
    assert res.quality_score == 1.0
    assert len(res.missing_reasons) == 0


def test_missing_data_report_flags_data_gap():
    res = evaluate_ticker_readiness(
        ticker="FAILTICKER",
        data_report="Failed to pre-collect stock data: 404 not found",
        technical_context="NONE ON FILE: No technical indicators",
        price_age_trading_days=10,
    )
    assert res.is_ready is False
    assert res.disposition == "DATA_GAP"
    assert res.quality_score < 0.5
    assert "data_report_collection_failed" in res.missing_reasons
    assert "price_history_stale_10_days" in res.missing_reasons
    assert "missing_technical_baseline" in res.missing_reasons
