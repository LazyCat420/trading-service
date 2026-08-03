"""Regression tests for five self-contradicting behaviours in the V3 cycle.

Each test names the contradiction it pins: a comment that claimed one thing
while the code did another, or two code paths answering the same question
differently. Audited and fixed 2026-08-03.
"""

import pytest
from unittest.mock import patch, MagicMock

from app.v3.orchestrator import (
    _apply_policy_gates,
    _drop_implausible_levels,
    _judge_confidence,
    _persist_trade_verdict,
)
from app.v3.contradiction_shadow import (
    build_dissent_block,
    resolution_is_substantive,
)
from app.v3.shared_desk import SharedDesk


def _desk(ticker="AAPL", cycle="cycle-test"):
    d = SharedDesk(cycle_id=cycle, ticker=ticker)
    d.cycle_metadata = {"ticker": ticker, "held": True, "triage_tier": "v3_deep"}
    return d


def _tradeable(**over):
    """A decision that clears every gate, so a test can fail exactly one."""
    base = {
        "action": "BUY",
        "confidence": 85,
        "reasoning": "clean",
        "stop_loss": 90.0,
        "take_profit": 130.0,
        "dynamic_trigger": {"type": "sma_50_drop", "value": 95.0},
        "position_size_pct": 2.5,
    }
    base.update(over)
    return base


# ── no DB in unit tests: the gates probe price_history and strategy health ──
@pytest.fixture(autouse=True)
def _stub_gate_io():
    with patch("app.quant.technical_baseline.has_price_history", return_value=True), \
         patch("app.quant.strategy_health.get_pipeline_health", return_value={"status": "OK"}), \
         patch("app.v3.telemetry.record_guardrail_firing", return_value=None), \
         patch("app.services.parameter_store.get_param",
               side_effect=lambda k: {"ANALYSIS_CONFIDENCE_THRESHOLD": 70,
                                      "DATA_QUALITY_FLOOR": 40}.get(k, 0)):
        yield


# ═══════════════════════════════════════════════════════════════════════════
# 1. Contradiction gate: was a silent cap to 60 under a floor of 70, i.e. a
#    guaranteed HOLD wearing a "not a downgrade to HOLD" comment.
# ═══════════════════════════════════════════════════════════════════════════

def test_dissent_block_is_empty_when_the_desks_agree():
    """Silence is the correct output for consensus.

    Announcing "no disagreement found" on every desk would teach the agent to
    read one absent conflict as confirmation.
    """
    assert build_dissent_block({
        "has_directional_conflict": False,
        "sentiment_by_source": {"quant_report": "BULLISH"},
    }) == ""
    assert build_dissent_block({"error": "boom"}) == ""


def test_dissent_block_names_the_disagreeing_desks_and_the_required_field():
    block = build_dissent_block({
        "has_directional_conflict": True,
        "sentiment_by_source": {
            "quant_report": "BEARISH",
            "fundamental_report": "BULLISH",
        },
        "contradictions": [{"description": "sentiment conflict", "severity": "high"}],
    })
    assert "quant_report" in block and "BEARISH" in block
    assert "fundamental_report" in block and "BULLISH" in block
    assert "dissent_resolution" in block


def test_dissent_block_never_quotes_the_agents_own_verdict_back_at_it():
    """final_decision/trade_decision are the decider's own view.

    Listing them as dissenting "sources" would read as independent
    corroboration of a claim the agent is about to make itself.
    """
    block = build_dissent_block({
        "has_directional_conflict": True,
        "sentiment_by_source": {
            "quant_report": "BEARISH",
            "final_decision": "BULLISH",
            "trade_decision": "BULLISH",
        },
    })
    # Only one genuine external source survives the filter → nothing to argue.
    assert block == ""


def test_a_resolved_dissent_no_longer_costs_the_desk_its_confidence():
    """The old cap rewrote confidence 85 → 60, which the 70 floor then blocked.

    A desk that answers the dissent now keeps its number AND trades.
    """
    desk = _desk()
    desk.cycle_metadata["dissent_detected"] = {
        "sentiment_by_source": {"quant_report": "BEARISH"},
    }
    desk.append_artifact("regime_classification", {"regime": "CONTRADICTORY"})
    desk.append_artifact("final_decision", _tradeable(
        dissent_resolution=(
            "The quant desk is bearish on a 14-day RSI cross, but its own report "
            "flags the sample as post-earnings and unrepresentative; the "
            "fundamental re-rating outweighs a two-week oscillator."
        ),
    ))

    assert _apply_policy_gates(desk) == "EXECUTE_BUY"
    assert desk.final_decision["confidence"] == 85  # untouched


def test_an_unanswered_dissent_blocks_under_its_own_honest_label():
    """Fail-closed, and NOT as LOW_CONFIDENCE.

    The old path blamed the desk ("confidence below threshold") for a 60 that
    we had written over its 85. The block is the same; the diagnosis is true.
    """
    desk = _desk()
    desk.cycle_metadata["dissent_detected"] = {
        "sentiment_by_source": {"quant_report": "BEARISH"},
    }
    desk.append_artifact("regime_classification", {"regime": "CONTRADICTORY"})
    desk.append_artifact("final_decision", _tradeable())  # no resolution

    assert _apply_policy_gates(desk) == "HOLD_POLICY_BLOCKED_UNRESOLVED_DISSENT"


def test_a_token_acknowledgement_is_not_a_resolution():
    assert not resolution_is_substantive({"dissent_resolution": "noted"})
    assert not resolution_is_substantive({"dissent_resolution": "   "})
    assert not resolution_is_substantive({})
    assert resolution_is_substantive({"dissent_resolution": "x" * 80})
    # A full veto-override rationale plainly engages with the disagreement —
    # failing it on field naming would block a trade for a formatting reason.
    assert resolution_is_substantive({"override_justification": "y" * 90})


def test_dissent_never_blocks_a_hold_or_an_undetected_conflict():
    desk = _desk()
    desk.append_artifact("regime_classification", {"regime": "CONTRADICTORY"})
    desk.append_artifact("final_decision", _tradeable(action="HOLD"))
    assert _apply_policy_gates(desk) == "HOLD_NO_SIGNAL"

    clean = _desk()
    clean.append_artifact("regime_classification", {"regime": "CONTRADICTORY"})
    clean.append_artifact("final_decision", _tradeable())
    assert _apply_policy_gates(clean) == "EXECUTE_BUY"


# ═══════════════════════════════════════════════════════════════════════════
# 2. Delta tier wrote no trade_results row — 40 of 40 analyses, 5 of them
#    holding real filled orders.
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_delta_decision_is_persisted_to_trade_results():
    """The old gate was `has_artifact("trade_decision")`; delta only ever
    publishes `final_decision`, so this whole route persisted nothing."""
    desk = _desk()
    delta = _tradeable(persona_used="Delta Analyst", regime="delta_relook")

    with patch("app.services.trade_result_saver.save_trade_result") as saver, \
         patch("app.trading.strategy_tracker.record_strategy") as tracker, \
         patch("app.services.rlm_audit.log_rlm_audit_trail"), \
         patch("app.v3.orchestrator._drop_implausible_levels", return_value=[]):
        await _persist_trade_verdict(
            desk, delta, cycle_id="cycle-test", bot_id="bot-1",
            ticker="AAPL", regime="delta_relook", source="v3_delta",
        )

    saver.assert_called_once()
    assert saver.call_args[0][0] == "AAPL"
    assert saver.call_args[0][2]["action"] == "BUY"
    tracker.assert_called_once()  # delta BUYs now reach P&L attribution


@pytest.mark.asyncio
async def test_the_challenger_stays_off_the_delta_path():
    """It re-decides from research/debate artifacts a delta desk never built."""
    desk = _desk()
    with patch("app.services.trade_result_saver.save_trade_result"), \
         patch("app.trading.strategy_tracker.record_strategy"), \
         patch("app.services.rlm_audit.log_rlm_audit_trail"), \
         patch("app.v3.challenger.get_challenger_spec", return_value="spec-a") as spec, \
         patch("app.v3.orchestrator._drop_implausible_levels", return_value=[]):
        await _persist_trade_verdict(
            desk, _tradeable(), cycle_id="c", bot_id="b",
            ticker="AAPL", regime="r", source="v3_delta",
        )
        spec.assert_not_called()


# ═══════════════════════════════════════════════════════════════════════════
# 3. The implausible-level sanitizer ran at different points on the two paths.
# ═══════════════════════════════════════════════════════════════════════════

def _close(price):
    """Stub the last-close lookup the sanitizer does."""
    db = MagicMock()
    db.execute.return_value.fetchone.return_value = (price,)
    ctx = MagicMock()
    ctx.__enter__.return_value = db
    return patch("app.db.connection.get_db", return_value=ctx)


def test_a_decimal_error_stop_is_dropped_not_clamped():
    """LMT shipped a $0.92 stop against a $581 close. Dropping is honest;
    clamping would present OUR arithmetic as the desk's thesis."""
    desk = _desk()
    desk.append_artifact("final_decision", _tradeable(stop_loss=0.92, take_profit=1.25))
    with _close(581.0):
        dropped = _drop_implausible_levels(desk)
    assert set(dropped) == {"stop_loss", "take_profit"}
    assert desk.final_decision["stop_loss"] is None
    assert desk.final_decision["take_profit"] is None


def test_the_sanitizer_is_idempotent():
    """It now runs on both the persist path and before the gates; running
    twice must not raise or re-record."""
    desk = _desk()
    # take_profit kept plausible for a $581 close so only the stop is dropped —
    # the point here is the second pass, not the band.
    desk.append_artifact("final_decision", _tradeable(stop_loss=0.92, take_profit=700.0))
    with _close(581.0):
        first = _drop_implausible_levels(desk)
        second = _drop_implausible_levels(desk)
    assert first == ["stop_loss"]
    assert second == []


def test_a_wide_but_legitimate_stop_survives():
    """The band is a decimal-error detector, not a strategy opinion."""
    desk = _desk()
    desk.append_artifact("final_decision", _tradeable(stop_loss=70.0, take_profit=250.0))
    with _close(100.0):
        assert _drop_implausible_levels(desk) == []
    assert desk.final_decision["stop_loss"] == 70.0


def test_the_sanitizer_is_no_longer_a_policy_gate():
    """It mutates and returns no label, so it must not sit in the gate chain —
    that is what made it run after the result was built on the delta path."""
    desk = _desk()
    desk.append_artifact("regime_classification", {"regime": "CONTRADICTORY"})
    desk.append_artifact("final_decision", _tradeable(stop_loss=0.92))
    # The gate no longer touches levels; the sanitizer is called separately.
    assert _apply_policy_gates(desk) == "EXECUTE_BUY"
    assert desk.final_decision["stop_loss"] == 0.92


# ═══════════════════════════════════════════════════════════════════════════
# 4. Policy-gate telemetry stamped a triage_tier nothing ever wrote.
# ═══════════════════════════════════════════════════════════════════════════

def test_policy_gate_firings_carry_the_triage_tier():
    """All 30 firings over 21 days recorded `triage_tier: null`, so the
    question the field exists to answer stayed unanswerable."""
    desk = _desk()
    desk.cycle_metadata["triage_tier"] = "v3_delta"
    desk.append_artifact("regime_classification", {"regime": "CONTRADICTORY"})
    desk.append_artifact("final_decision", _tradeable(confidence=40))

    with patch("app.v3.telemetry.record_guardrail_firing") as rec:
        assert _apply_policy_gates(desk) == "HOLD_POLICY_BLOCKED_LOW_CONFIDENCE"

    assert rec.call_args.kwargs["detail"]["triage_tier"] == "v3_delta"


# ═══════════════════════════════════════════════════════════════════════════
# 5. debate_judge has two writers with incompatible key names; two readers in
#    the orchestrator each knew only one.
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("verdict,expected", [
    ({"winner": "bull", "final_confidence": 75}, 75),   # the judge agent
    ({"winning_side": "bull", "confidence": 75}, 75),   # tournament / skip marker
    ({"winning_side": "skipped", "confidence": 0}, 0),
    ({"action": "HOLD", "reasoning": "x"}, 0),          # validator-coerced
    ({"final_confidence": "80"}, 80),                   # LLM string
    ({}, 0),
    (None, 0),
])
def test_judge_confidence_reads_both_artifact_shapes(verdict, expected):
    assert _judge_confidence(verdict) == expected


def test_a_high_confidence_judge_verdict_is_not_read_as_zero():
    """With DEBATE_ENGINE=3 the real judge (final_confidence) is the LIVE path.
    Reading only `confidence` scored every one of its verdicts 0, so the
    synthesizer's "only when low-confidence" deep-retrieval hook always fired.
    """
    assert _judge_confidence({"winner": "bull", "final_confidence": 82}) >= 60
