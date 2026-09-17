"""Unit tests for Deterministic Policy Translator."""

import datetime
import pytest
from app.trading.attribution.models import DecisionArtifact, PolicyDisposition
from app.trading.policy.policy_translator import PolicyInputSnapshot, PolicyTranslator


@pytest.fixture
def base_artifact():
    return DecisionArtifact(
        decision_id="dec-test-1",
        cycle_id="cycle-100",
        ticker="AAPL",
        producer="v3_decision_synthesizer",
        model="nemotron35",
        requested_action="BUY",
        confidence=80,
        requested_timing={"entry_mode": "enter_now", "trigger_purpose": "none"},
    )


@pytest.fixture
def base_snapshot():
    return PolicyInputSnapshot(
        portfolio_equity=100000.0,
        cash_balance=50000.0,
        held_ticker_value=0.0,
        max_concentration_pct=0.25,
        max_position_size_pct=0.10,
        quote_price=150.0,
        quote_age_hours=1.0,
        is_held=False,
    )


def test_policy_translator_approve_healthy_buy(base_artifact, base_snapshot):
    policy_dec, intent = PolicyTranslator.evaluate(base_artifact, base_snapshot)
    assert policy_dec.disposition == PolicyDisposition.APPROVE
    assert "BUY_APPROVED" in policy_dec.reason_codes
    assert intent is not None
    assert intent.side == "BUY"
    assert intent.approved_size_pct == 0.08  # 0.10 * (80/100)
    assert intent.approved_notional == 8000.0  # 100k * 0.08


def test_policy_translator_stale_quote_rejects(base_artifact, base_snapshot):
    base_snapshot.quote_age_hours = 25.0
    policy_dec, intent = PolicyTranslator.evaluate(base_artifact, base_snapshot)
    assert policy_dec.disposition == PolicyDisposition.REJECT
    assert "STALE_QUOTE" in policy_dec.reason_codes
    assert intent is None


def test_policy_translator_low_confidence_blocks(base_artifact, base_snapshot):
    base_artifact.confidence = 35
    policy_dec, intent = PolicyTranslator.evaluate(base_artifact, base_snapshot)
    assert policy_dec.disposition == PolicyDisposition.BLOCK
    assert "CONFIDENCE_TOO_LOW" in policy_dec.reason_codes
    assert intent is None


def test_policy_translator_drawdown_breaker_blocks(base_artifact, base_snapshot):
    base_snapshot.drawdown_breaker_active = True
    policy_dec, intent = PolicyTranslator.evaluate(base_artifact, base_snapshot)
    assert policy_dec.disposition == PolicyDisposition.BLOCK
    assert "DRAWDOWN_BREAKER_ACTIVE" in policy_dec.reason_codes
    assert intent is None


def test_policy_translator_strategy_health_cut_blocks(base_artifact, base_snapshot):
    base_snapshot.strategy_health_status = "CUT"
    policy_dec, intent = PolicyTranslator.evaluate(base_artifact, base_snapshot)
    assert policy_dec.disposition == PolicyDisposition.BLOCK
    assert "STRATEGY_HEALTH_CUT" in policy_dec.reason_codes
    assert intent is None


def test_policy_translator_strategy_health_reduce_halves_size(base_artifact, base_snapshot):
    base_snapshot.strategy_health_status = "REDUCE"
    policy_dec, intent = PolicyTranslator.evaluate(base_artifact, base_snapshot)
    assert policy_dec.disposition == PolicyDisposition.APPROVE
    assert "STRATEGY_HEALTH_HALVED" in policy_dec.reason_codes
    assert intent is not None
    assert intent.approved_size_pct == 0.04  # 0.08 / 2


def test_policy_translator_concentration_cap_enforced(base_artifact, base_snapshot):
    base_snapshot.held_ticker_value = 22000.0  # Max is 25k (25% of 100k)
    # Requested size is 8k (which would take total to 30k > 25k)
    policy_dec, intent = PolicyTranslator.evaluate(base_artifact, base_snapshot)
    assert policy_dec.disposition == PolicyDisposition.APPROVE_WITH_CAP
    assert "CONCENTRATION_CAPPED" in policy_dec.reason_codes
    assert intent is not None
    assert intent.approved_notional == 3000.0  # 25k - 22k


def test_policy_translator_conditional_entry_queues_research(base_artifact, base_snapshot):
    base_artifact.requested_timing = {
        "entry_mode": "enter_on_condition",
        "trigger_purpose": "entry",
        "dynamic_trigger": {"type": "price_below", "value": 140.0},
    }
    policy_dec, intent = PolicyTranslator.evaluate(base_artifact, base_snapshot)
    assert policy_dec.disposition == PolicyDisposition.QUEUE_RESEARCH
    assert "CONDITIONAL_ENTRY_ARMED" in policy_dec.reason_codes
    assert intent is None  # Guiding Invariant: conditional entry cannot create paper order


def test_policy_translator_hold_converts_to_watch(base_artifact, base_snapshot):
    base_artifact.requested_action = "HOLD"
    policy_dec, intent = PolicyTranslator.evaluate(base_artifact, base_snapshot)
    assert policy_dec.disposition == PolicyDisposition.CONVERT_TO_WATCH
    assert intent is None


def test_policy_translator_sell_unheld_blocks(base_artifact, base_snapshot):
    base_artifact.requested_action = "SELL"
    base_snapshot.is_held = False
    policy_dec, intent = PolicyTranslator.evaluate(base_artifact, base_snapshot)
    assert policy_dec.disposition == PolicyDisposition.BLOCK
    assert "NO_OPEN_POSITION" in policy_dec.reason_codes
    assert intent is None


def test_policy_translator_sell_held_approves(base_artifact, base_snapshot):
    base_artifact.requested_action = "SELL"
    base_snapshot.is_held = True
    policy_dec, intent = PolicyTranslator.evaluate(base_artifact, base_snapshot)
    assert policy_dec.disposition == PolicyDisposition.APPROVE
    assert "SELL_APPROVED" in policy_dec.reason_codes
    assert intent is not None
    assert intent.side == "SELL"


def test_policy_translator_deterministic_replay(base_artifact, base_snapshot):
    t_fixed = datetime.datetime(2026, 9, 16, 12, 0, 0, tzinfo=datetime.timezone.utc)
    res1, int1 = PolicyTranslator.evaluate(
        base_artifact, base_snapshot, policy_decision_id="pol-fixed-1", now=t_fixed
    )
    res2, int2 = PolicyTranslator.evaluate(
        base_artifact, base_snapshot, policy_decision_id="pol-fixed-1", now=t_fixed
    )
    assert res1.config_hash == res2.config_hash
    assert res1.approved_values == res2.approved_values
    assert int1.idempotency_key == int2.idempotency_key
