"""Unit tests for Attribution and Control-Plane Canonical Models."""

import datetime
import pytest
from app.trading.attribution.models import (
    AttributionClass,
    AttributionReport,
    DecisionArtifact,
    ExecutionIntent,
    ExecutionReconciliation,
    IntentStatus,
    OrderAttempt,
    OrderAttemptStatus,
    PolicyDecision,
    PolicyDisposition,
    ReconciliationVerdict,
)


def test_decision_artifact_validation():
    art = DecisionArtifact(
        decision_id="dec-123",
        cycle_id="cycle-1",
        ticker="AAPL",
        producer="v3_decision_synthesizer",
        model="nemotron35",
        requested_action="BUY",
        confidence=85,
    )
    assert art.decision_id == "dec-123"
    assert art.asset_type == "stock"
    assert art.currency == "USD"
    assert art.benchmark_symbol == "SPY"
    assert art.created_at.tzinfo == datetime.timezone.utc


def test_policy_decision_dispositions():
    pol = PolicyDecision(
        policy_decision_id="pol-123",
        decision_id="dec-123",
        config_hash="abc12345",
        requested_values={"action": "BUY"},
        normalized_values={"action": "BUY"},
        approved_values={"action": "BUY", "size_pct": 0.05},
        disposition=PolicyDisposition.APPROVE_WITH_CAP,
        reason_codes=["CONCENTRATION_CAPPED"],
    )
    assert pol.disposition == PolicyDisposition.APPROVE_WITH_CAP
    assert "CONCENTRATION_CAPPED" in pol.reason_codes
    assert pol.evaluated_at.tzinfo == datetime.timezone.utc


def test_execution_intent_timestamps():
    now = datetime.datetime.now(datetime.timezone.utc)
    expires = now + datetime.timedelta(minutes=15)
    intent = ExecutionIntent(
        execution_intent_id="int-123",
        decision_id="dec-123",
        policy_decision_id="pol-123",
        ticker="AAPL",
        side="BUY",
        approved_size_pct=0.05,
        valid_from=now,
        expires_at=expires,
        idempotency_key="idemp:cycle-1:AAPL:BUY:pol-123",
    )
    assert intent.status == IntentStatus.CREATED
    assert intent.valid_from.tzinfo == datetime.timezone.utc
    assert intent.expires_at.tzinfo == datetime.timezone.utc


def test_order_attempt_status():
    attempt = OrderAttempt(
        order_attempt_id="att-1",
        execution_intent_id="int-123",
        request_hash="hash123",
        status=OrderAttemptStatus.ACCEPTED,
        order_id="ord-999",
    )
    assert attempt.status == OrderAttemptStatus.ACCEPTED
    assert attempt.order_id == "ord-999"


def test_reconciliation_verdict():
    rec = ExecutionReconciliation(
        reconciliation_id="rec-1",
        execution_intent_id="int-123",
        order_id="ord-999",
        fill_ids=["fill-1"],
        intended_qty=10.0,
        filled_qty=10.0,
        reference_price=150.0,
        expected_price=150.05,
        realized_price=150.02,
        verdict=ReconciliationVerdict.EXECUTION_MATCHED,
    )
    assert rec.verdict == ReconciliationVerdict.EXECUTION_MATCHED


def test_attribution_report_classification():
    rep = AttributionReport(
        attribution_id="att-rep-1",
        classification=AttributionClass.DECISION_FAILURE,
        primary_reason_code="NEGATIVE_DECISION_ALPHA",
        owner_subsystem="LLM_AGENT",
    )
    assert rep.classification == AttributionClass.DECISION_FAILURE
    assert rep.owner_subsystem == "LLM_AGENT"
