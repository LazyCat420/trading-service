"""The bearish-override shadow flag.

Measured 2026-07-25: the board turning bearish over a fundamental desk that
reported NO near-term view is the one handoff that survives a permutation test
(-2.81%/decision n=68; -2.38% executable-only, p=0.0015). This flag marks that
population so promotion to a real coercion can be decided on accumulated live
evidence rather than one 30-day window.

The tests below pin the two things that make it safe: it fires on exactly the
measured quadrant and nothing else, and it NEVER alters a decision.
"""

from __future__ import annotations

from unittest.mock import patch

from app.v3.artifact_validators import flag_bearish_override_of_fundamental as flag

FLAG = "bearish_override_of_neutral_fundamental"


def _neutral_desk():
    return {"near_term_read": {"direction": "NEUTRAL", "matters_this_week": False}}


def _fire(board, fundamental):
    with patch("app.v3.telemetry.record_guardrail_firing"):
        return flag(dict(board), fundamental_report=fundamental,
                    ticker="TEST", cycle_id="c1")


def test_fires_on_the_measured_quadrant():
    """desk NEUTRAL -> board BEARISH: n=68, mean -2.81%."""
    out = _fire({"action": "SELL", "confidence": 70}, _neutral_desk())
    assert FLAG in out["_shadow_flags"]


def test_never_alters_the_decision():
    """THE safety property. n=35 executable overrides is enough to detect the
    effect, not to rewire the board — the standing rule is shadow-first."""
    board = {"action": "SELL", "confidence": 70, "position_size_pct": 4}
    out = _fire(board, _neutral_desk())
    assert out["action"] == "SELL"
    assert out["confidence"] == 70
    assert out["position_size_pct"] == 4
    assert "_coerced_from" not in out
    assert out.get("decision_provenance") != "coerced_unshortable"


def test_does_not_fire_on_bullish_overrides():
    """Every bullish override measured POSITIVE (desk NEUT -> board BULL +1.60%,
    desk BEAR -> board BULL +2.50%). Flagging them would suppress value."""
    out = _fire({"action": "BUY"}, _neutral_desk())
    assert not out.get("_shadow_flags")


def test_does_not_fire_when_the_desk_agrees():
    """Agreement earns +0.06% — it is the override that costs."""
    bearish_desk = {"near_term_read": {"direction": "BEARISH"}}
    out = _fire({"action": "SELL"}, bearish_desk)
    assert not out.get("_shadow_flags")


def test_does_not_fire_on_bullish_desk_overridden_bearish():
    """desk BULL -> board BEAR is a DIFFERENT quadrant (n=13, -0.87%) and is
    not what was measured as costly. Keep the flag narrow."""
    bullish_desk = {"near_term_read": {"direction": "BULLISH"}}
    out = _fire({"action": "SELL"}, bullish_desk)
    assert not out.get("_shadow_flags")


def test_prefers_near_term_read_over_thesis_direction():
    """Horizon discipline: thesis_direction is an explicitly multi-quarter
    business view. A YEARS-horizon BEARISH thesis with a NEUTRAL near-term read
    is a NEUTRAL desk for a 7-day trade."""
    desk = {"thesis_direction": "BEARISH", "horizon": "YEARS",
            "near_term_read": {"direction": "NEUTRAL"}}
    out = _fire({"action": "SELL"}, desk)
    assert FLAG in out["_shadow_flags"]


def test_no_fundamental_report_is_a_no_op():
    out = _fire({"action": "SELL"}, None)
    assert not out.get("_shadow_flags")


def test_unreadable_stance_is_a_no_op():
    out = _fire({"action": "SELL"}, {"near_term_read": {"direction": "???"}})
    assert not out.get("_shadow_flags")


def test_flag_is_idempotent():
    """The board artifact is validated on final_decision AND trade_decision;
    a duplicated flag would double-count the population."""
    board = {"action": "SELL"}
    once = _fire(board, _neutral_desk())
    twice = flag(once, fundamental_report=_neutral_desk(), ticker="T", cycle_id="c")
    assert twice["_shadow_flags"].count(FLAG) == 1


def test_telemetry_failure_cannot_break_the_artifact():
    with patch("app.v3.telemetry.record_guardrail_firing", side_effect=RuntimeError("db down")):
        out = flag({"action": "SELL"}, fundamental_report=_neutral_desk(),
                   ticker="T", cycle_id="c")
    assert out["action"] == "SELL"
    assert FLAG in out["_shadow_flags"]
