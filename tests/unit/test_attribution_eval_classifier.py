"""Unit tests for Evaluator and AttributionClassifier."""

import pytest
from app.trading.attribution.classifier import AttributionClassifier
from app.trading.attribution.evaluator import DecisionEvaluator, ExecutionEvaluator
from app.trading.attribution.models import AttributionClass, PolicyDisposition, ReconciliationVerdict


def test_decision_evaluator_long_win():
    res = DecisionEvaluator.evaluate(
        entry_price=100.0,
        horizon_price=110.0,
        benchmark_entry=500.0,
        benchmark_horizon=510.0,
        action="BUY",
    )
    # Decision return: +10%
    # Benchmark return: +2%
    # Decision alpha: +8%
    assert res.decision_return == 10.0
    assert res.benchmark_return == 2.0
    assert res.decision_alpha == 8.0


def test_decision_evaluator_long_loss():
    res = DecisionEvaluator.evaluate(
        entry_price=100.0,
        horizon_price=90.0,
        benchmark_entry=500.0,
        benchmark_horizon=510.0,
        action="BUY",
    )
    # Decision return: -10%
    # Benchmark return: +2%
    # Decision alpha: -12%
    assert res.decision_return == -10.0
    assert res.decision_alpha == -12.0


def test_execution_evaluator_drag():
    res = ExecutionEvaluator.evaluate(
        fill_price=102.0,  # Filled worse than ref (100.0)
        exit_realized_price=110.0,
        reference_price=100.0,
        benchmark_entry=500.0,
        benchmark_horizon=510.0,
        fees=10.0,
        fill_value=1020.0,
        action="BUY",
    )
    # Comp decision return: (110 - 100)/100 = 10%
    # Raw exec return: (110 - 102)/102 = 7.8431%
    # Fees pct: 10 / 1020 = ~0.98%
    assert res.decision_return == 10.0
    assert res.execution_drag < 0  # Drag is negative (adverse)


def test_attribution_classifier_decision_failure():
    report = AttributionClassifier.classify(
        lineage={"decision_id": "dec-1"},
        decision_alpha=-5.0,
        net_alpha=-5.5,
    )
    assert report.classification == AttributionClass.DECISION_FAILURE
    assert report.primary_reason_code == "NEGATIVE_DECISION_ALPHA"
    assert report.owner_subsystem == "LLM_AGENT"


def test_attribution_classifier_execution_failure_drag():
    report = AttributionClassifier.classify(
        lineage={"decision_id": "dec-2"},
        decision_alpha=4.0,  # Good thesis
        net_alpha=-1.5,      # Ruined by execution drag
        execution_drag=-5.5,
    )
    assert report.classification == AttributionClass.EXECUTION_FAILURE
    assert report.primary_reason_code == "ADVERSE_EXECUTION_DRAG"
    assert report.owner_subsystem == "PAPER_TRADER"


def test_attribution_classifier_policy_intervention():
    report = AttributionClassifier.classify(
        lineage={"decision_id": "dec-3"},
        disposition=PolicyDisposition.BLOCK.value,
    )
    assert report.classification == AttributionClass.POLICY_INTERVENTION
    assert report.primary_reason_code == "POLICY_BLOCK"
    assert report.owner_subsystem == "POLICY_GATE"


def test_attribution_classifier_market_data_failure():
    report = AttributionClassifier.classify(
        lineage={"decision_id": "dec-4"},
        data_error="STALE_QUOTE_36H",
    )
    assert report.classification == AttributionClass.MARKET_DATA_FAILURE
    assert report.primary_reason_code == "STALE_QUOTE_36H"
    assert report.owner_subsystem == "DATA_FEED"


def test_attribution_classifier_unresolved_pending():
    report = AttributionClassifier.classify(
        lineage={"decision_id": "dec-5"},
        decision_alpha=None,
        net_alpha=None,
    )
    assert report.classification == AttributionClass.UNRESOLVED
    assert report.primary_reason_code == "OUTCOME_PENDING_MATURITY"


def test_attribution_classifier_no_failure():
    report = AttributionClassifier.classify(
        lineage={"decision_id": "dec-6"},
        decision_alpha=5.0,
        net_alpha=4.5,
    )
    assert report.classification == AttributionClass.NO_FAILURE
    assert report.primary_reason_code == "POSITIVE_NET_ALPHA"
