from app.autoresearch.outcome_recovery import (
    EVAL_CONDITIONAL, EVAL_FLAT_WAIT, EVAL_HOLD_EXISTING, EVAL_IMMEDIATE,
    EVAL_UNRECOVERABLE, classify,
)


def _desk(action="BUY", *, mode="enter_now", held=False, trigger=None):
    return {"cycle_metadata": {"held": held}, "trade_decision": {
        "action": action, "entry_mode": mode, "dynamic_trigger": trigger,
    }}


def test_immediate_entry_keeps_the_existing_resolver_contract():
    out = classify({"id": "a", "action": "BUY"}, _desk())
    assert (out.evaluation_type, out.claim_type, out.evidence_state) == (
        EVAL_IMMEDIATE, EVAL_IMMEDIATE, "pending")


def test_conditional_entry_is_never_relabelled_as_spot_buy():
    out = classify({"id": "a", "action": "BUY"}, _desk(
        mode="enter_on_condition", trigger={"type": "sma_200_rise", "value": 100}))
    assert out.evaluation_type == EVAL_CONDITIONAL
    assert out.claim_type is None
    assert out.evidence_state == "pending"


def test_unheld_hold_is_a_flat_wait_and_held_hold_is_separate():
    flat = classify({"id": "a", "action": "HOLD"}, _desk(action="HOLD", mode="watch_only", held=False))
    kept = classify({"id": "b", "action": "HOLD"}, _desk(action="HOLD", mode="watch_only", held=True))
    assert flat.evaluation_type == EVAL_FLAT_WAIT and flat.claim_type == EVAL_FLAT_WAIT
    assert kept.evaluation_type == EVAL_HOLD_EXISTING and kept.claim_type is None


def test_missing_or_conflicting_source_is_explicitly_unrecoverable():
    assert classify({"id": "a", "action": "BUY"}, {}).evaluation_type == EVAL_UNRECOVERABLE
    assert classify({"id": "a", "action": "BUY"}, _desk(action="HOLD")).evaluation_type == EVAL_UNRECOVERABLE
