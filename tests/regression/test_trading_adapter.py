import pytest
from app.telemetry.trading_adapter import (
    TradingLineageTracker,
    CandidateTradingObservation,
    _sanitize_dict,
    TradingSpan,
)


def test_derive_trace_id_deterministic():
    cycle_id = "cycle-v3-1788770000"
    trace_id_1 = TradingLineageTracker.derive_trace_id(cycle_id)
    trace_id_2 = TradingLineageTracker.derive_trace_id(cycle_id)

    assert len(trace_id_1) == 32
    assert trace_id_1 == trace_id_2


def test_lineage_spans_propagation():
    cycle_id = "cycle-test-lineage"
    ticker = "NVDA"

    snap_span = TradingLineageTracker.record_market_snapshot(
        cycle_id=cycle_id, ticker=ticker, bar_price=125.50, source="alpaca"
    )
    assert snap_span.trace_id == TradingLineageTracker.derive_trace_id(cycle_id)
    assert snap_span.stage == "market_snapshot"
    assert snap_span.attributes["price"] == 125.50

    dec_span = TradingLineageTracker.record_decision(
        cycle_id=cycle_id, ticker=ticker, action="BUY", confidence=85, parent_span_id=snap_span.span_id
    )
    assert dec_span.trace_id == snap_span.trace_id
    assert dec_span.parent_span_id == snap_span.span_id
    assert dec_span.attributes["action"] == "BUY"

    policy_span = TradingLineageTracker.record_policy_eval(
        cycle_id=cycle_id, ticker=ticker, verdict="APPROVED", approved=True, parent_span_id=dec_span.span_id
    )
    assert policy_span.status == "OK"

    intent_span = TradingLineageTracker.record_execution_intent(
        cycle_id=cycle_id, ticker=ticker, action="BUY", shares=100, price=125.50
    )
    assert "intent_id" in intent_span.attributes

    fill_span = TradingLineageTracker.record_order_fill(
        cycle_id=cycle_id, ticker=ticker, fill_id="fill_abc123", shares=100, fill_price=125.48
    )
    assert fill_span.attributes["executed_shares"] == 100

    recon_span = TradingLineageTracker.record_reconciliation(
        cycle_id=cycle_id, ticker=ticker, status="MATCH", diff=0.0
    )
    assert recon_span.status == "OK"

    outcome_span = TradingLineageTracker.record_outcome(
        cycle_id=cycle_id, ticker=ticker, outcome="WIN", pnl_pct=3.8
    )
    assert outcome_span.status == "OK"
    assert outcome_span.attributes["pnl_pct"] == 3.8


def test_candidate_observation_creation():
    cycle_id = "cycle-test-observation"
    ticker = "MSFT"
    evidence_ref = "sec_10k_filing_evidence"
    lesson_text = "Watch out for sudden cloud revenue deceleration."

    candidate = CandidateTradingObservation.create_candidate(
        cycle_id=cycle_id,
        ticker=ticker,
        lesson_text=lesson_text,
        evidence_ref=evidence_ref,
    )

    assert candidate["lifecycle_state"] == "CANDIDATE"
    assert candidate["ticker"] == "MSFT"
    assert candidate["evidence_ref"] == evidence_ref
    assert candidate["trace_id"] == TradingLineageTracker.derive_trace_id(cycle_id)
    assert candidate["verified_at"] is None


def test_sanitize_dict_redaction():
    raw_data = {
        "safe_field": "market_data",
        "api_key": "super_secret_key_123",
        "nested": {
            "password": "my_password_xyz",
            "normal": 42,
        },
    }

    clean = _sanitize_dict(raw_data)
    assert clean["safe_field"] == "market_data"
    assert clean["api_key"] == "[REDACTED]"
    assert clean["nested"]["password"] == "[REDACTED]"
    assert clean["nested"]["normal"] == 42
