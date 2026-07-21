"""
Tests for the enforced policy gates and jury vote normalization.

2026-07-15 audit findings pinned here:
  - policy_action used to be decorative (no consumer) — it is now binding in
    pipeline_service, so the gate logic itself must be correct.
  - A jury-majority veto is binding; a solo veto is a risk flag the board can
    only trade through with explicit mitigation (stop-loss + trigger + size).
  - validate_jury_score normalizes the new "winner" side vote.
"""
from app.cognition.debate.format_validator import validate_jury_score
from app.v3.orchestrator import _apply_policy_gates
from app.v3.shared_desk import SharedDesk


def _desk(**overrides) -> SharedDesk:
    desk = SharedDesk(ticker="TEST", cycle_id="cycle-test")
    desk.regime_classification = {"summary": "regime ok"}
    desk.final_decision = {
        "action": "BUY",
        "confidence": 80,
        "stop_loss": 100.0,
        "dynamic_trigger": {"type": "trailing_drop", "value": 0.1},
        "position_size_pct": 4.0,
    }
    for key, value in overrides.items():
        setattr(desk, key, value)
    return desk


# ── Jury winner-vote normalization ──────────────────────────────────

def test_jury_winner_normalized():
    ok, parsed, _ = validate_jury_score(
        '{"winner": "Thesis A", "score": 7, "reasoning": "solid"}'
    )
    assert ok and parsed["winner"] == "A"


def test_jury_winner_missing_means_abstain():
    ok, parsed, _ = validate_jury_score('{"score": 6, "reasoning": "meh"}')
    assert ok and "winner" not in parsed


def test_jury_winner_ambiguous_dropped():
    ok, parsed, _ = validate_jury_score(
        '{"winner": "A and B", "score": 6, "reasoning": "torn"}'
    )
    assert ok and "winner" not in parsed


# ── Policy gates ─────────────────────────────────────────────────────

def test_gate_executes_clean_buy():
    assert _apply_policy_gates(_desk()) == "EXECUTE_BUY"


def test_gate_holds_without_signal():
    desk = _desk()
    desk.final_decision = {"action": "HOLD", "confidence": 0}
    assert _apply_policy_gates(desk) == "HOLD_NO_SIGNAL"


# ── SELL-on-unheld: the bug that survived 5+ downstream fixes ────────
# The gate must express "can't sell, not held" ITSELF — every prior fix
# patched a downstream layer (agent prompt, executor block, event emit)
# while the gate kept emitting EXECUTE_SELL with no holdings check.

def _sell_desk(held, confidence=80):
    desk = _desk()
    desk.final_decision = {
        "action": "SELL", "confidence": confidence,
        "stop_loss": 100.0,
        "dynamic_trigger": {"type": "trailing_drop", "value": 0.1},
        "position_size_pct": 4.0,
    }
    if held is not None:
        desk.cycle_metadata["held"] = held
    return desk


def test_gate_blocks_sell_when_not_held():
    # High confidence must NOT rescue a sell of a position we don't hold.
    assert _apply_policy_gates(_sell_desk(held=False)) == "HOLD_NO_POSITION"


def test_gate_executes_sell_when_held():
    assert _apply_policy_gates(_sell_desk(held=True)) == "EXECUTE_SELL"


def test_gate_falls_through_when_holdings_unknown():
    # held missing (context fetch raised at desk build) → don't hard-block on a
    # guess; let the executor's own position check + paper_trader be the backstop.
    assert _apply_policy_gates(_sell_desk(held=None)) == "EXECUTE_SELL"


def test_not_held_beats_low_confidence_label():
    # not-held is the dominant, most-informative reason.
    assert _apply_policy_gates(_sell_desk(held=False, confidence=50)) == "HOLD_NO_POSITION"


def test_gate_blocks_low_confidence():
    desk = _desk()
    desk.final_decision["confidence"] = 50
    assert _apply_policy_gates(desk) == "HOLD_POLICY_BLOCKED_LOW_CONFIDENCE"


def test_gate_blocks_missing_regime():
    desk = _desk(regime_classification=None)
    assert _apply_policy_gates(desk) == "HOLD_POLICY_BLOCKED_MISSING_REGIME"


def test_jury_majority_veto_is_binding():
    desk = _desk(tournament_result={"vetoed": True, "risk_flags": []})
    assert _apply_policy_gates(desk) == "HOLD_POLICY_BLOCKED_JURY_VETO"


def test_solo_veto_blocks_unmitigated_trade():
    desk = _desk(
        tournament_result={
            "vetoed": False,
            "risk_flags": [{"juror": "Risk_Manager", "reasoning": "no stop"}],
        }
    )
    desk.final_decision.pop("stop_loss")
    assert _apply_policy_gates(desk) == "HOLD_POLICY_BLOCKED_UNMITIGATED_RISK"


def test_solo_veto_allows_fully_mitigated_trade():
    desk = _desk(
        tournament_result={
            "vetoed": False,
            "risk_flags": [{"juror": "Risk_Manager", "reasoning": "risky"}],
        }
    )
    assert _apply_policy_gates(desk) == "EXECUTE_BUY"


def test_synthesizer_overrides_board_for_mitigation():
    desk = _desk(
        tournament_result={
            "vetoed": False,
            "risk_flags": [{"juror": "Risk_Manager", "reasoning": "risky"}],
        }
    )
    desk.final_decision.pop("position_size_pct")
    desk.trade_decision = {
        "action": "BUY",
        "confidence": 75,
        "position_size_pct": 3.0,
    }
    assert _apply_policy_gates(desk) == "EXECUTE_BUY"


# ── Dynamic gates (plan 3.1-3.3, 2026-07-15) ─────────────────────────

def test_board_confidence_floor_raises_the_bar():
    desk = _desk()
    desk.final_decision["confidence"] = 80
    desk.final_decision["confidence_floor"] = 85
    assert _apply_policy_gates(desk) == "HOLD_POLICY_BLOCKED_LOW_CONFIDENCE"


def test_board_confidence_floor_cannot_lower_the_bar():
    desk = _desk()
    desk.final_decision["confidence"] = 50  # below the firm-wide 65
    desk.final_decision["confidence_floor"] = 10
    assert _apply_policy_gates(desk) == "HOLD_POLICY_BLOCKED_LOW_CONFIDENCE"


def test_low_data_quality_blocks_despite_confidence():
    desk = _desk()
    desk.final_decision["conviction_vector"] = {"data_quality": 30, "consensus_strength": 90}
    assert _apply_policy_gates(desk) == "HOLD_POLICY_BLOCKED_DATA_QUALITY"


def test_healthy_conviction_vector_passes():
    desk = _desk()
    desk.final_decision["conviction_vector"] = {"data_quality": 75, "consensus_strength": 60}
    assert _apply_policy_gates(desk) == "EXECUTE_BUY"


def test_board_may_override_jury_veto_with_justification_and_mitigation():
    desk = _desk(tournament_result={"vetoed": True, "risk_flags": []})
    desk.final_decision["overrides_veto"] = True
    desk.final_decision["override_justification"] = (
        "Veto was driven by a stale filing; fresh 8-K contradicts it. "
        "Position halved, hard stop at support."
    )
    # _desk() already carries stop_loss + dynamic_trigger + position_size_pct
    assert _apply_policy_gates(desk) == "EXECUTE_BUY"


def test_veto_override_without_justification_still_blocks():
    desk = _desk(tournament_result={"vetoed": True, "risk_flags": []})
    desk.final_decision["overrides_veto"] = True
    desk.final_decision["override_justification"] = "   "
    assert _apply_policy_gates(desk) == "HOLD_POLICY_BLOCKED_JURY_VETO"


def test_veto_override_without_mitigation_blocks_as_unmitigated():
    desk = _desk(tournament_result={"vetoed": True, "risk_flags": []})
    desk.final_decision["overrides_veto"] = True
    desk.final_decision["override_justification"] = "Stale filing, fresh 8-K contradicts."
    desk.final_decision.pop("stop_loss")
    assert _apply_policy_gates(desk) == "HOLD_POLICY_BLOCKED_UNMITIGATED_RISK"
