"""Tests for the 2026-07-24 no-trade-available wave.

There is no shorting. On a ticker the bot does not hold, the only executable
action is BUY — yet 167 of 176 SELL decisions (95%) were on unheld tickers,
each blocked by the policy gate only AFTER the desk had spent ~1,243s on it.
Those were simultaneously the most expensive and least actionable decisions in
the pipeline, and they are what made the board look like it destroyed value.
"""

import pytest

from app.v3.artifact_validators import coerce_unshortable_sell


class _Desk:
    """Minimal stand-in carrying just the artifacts the gate inspects."""

    def __init__(self, **artifacts):
        self.fundamental_report = artifacts.get("fundamental_report", {})
        self.quant_report = artifacts.get("quant_report", {})
        self.desk_note = artifacts.get("desk_note", {})


class TestUnshortableSellCoercion:
    def test_unheld_sell_becomes_a_flat_hold(self):
        art = coerce_unshortable_sell(
            {"action": "SELL", "confidence": 72, "position_size_pct": 4.0},
            held=False,
        )
        assert art["action"] == "HOLD"
        assert art["position_size_pct"] == 0
        assert art["_coerced_from"] == "SELL"

    def test_held_sell_is_a_real_exit_and_survives(self):
        art = coerce_unshortable_sell({"action": "SELL", "confidence": 72}, held=True)
        assert art["action"] == "SELL"
        assert "_coerced_from" not in art

    @pytest.mark.parametrize("action", ["BUY", "HOLD"])
    def test_other_actions_are_untouched(self, action):
        art = coerce_unshortable_sell({"action": action}, held=False)
        assert art["action"] == action

    def test_the_bearish_view_is_retained_not_erased(self):
        """Coercion must not silently rewrite the desk's opinion — the note is
        what tells a later reader the agent wanted out and could not act."""
        art = coerce_unshortable_sell(
            {"action": "SELL", "reasoning": "margins collapsing"}, held=False
        )
        assert art["reasoning"] == "margins collapsing"
        notes = " ".join(art.get("_validator_notes", []))
        assert "no shorting" in notes.lower()

    def test_lowercase_action_is_still_caught(self):
        art = coerce_unshortable_sell({"action": "sell"}, held=False)
        assert art["action"] == "HOLD"


class TestResearchUnanimity:
    """The gate skips the most expensive stage in the pipeline, so it must
    under-fire rather than over-fire."""

    def _gate(self, desk):
        from app.v3.orchestrator import _research_unanimously_bearish
        return _research_unanimously_bearish(desk)

    def test_all_bearish_fires(self):
        assert self._gate(_Desk(
            fundamental_report={"thesis_direction": "BEARISH"},
            quant_report={"thesis_direction": "BEARISH"},
        )) is True

    def test_one_dissenting_bull_blocks_the_gate(self):
        assert self._gate(_Desk(
            fundamental_report={"thesis_direction": "BEARISH"},
            quant_report={"thesis_direction": "BULLISH"},
        )) is False

    def test_neutral_is_not_bearish(self):
        assert self._gate(_Desk(
            fundamental_report={"thesis_direction": "BEARISH"},
            quant_report={"thesis_direction": "NEUTRAL"},
        )) is False

    def test_a_single_opinion_can_never_be_unanimous(self):
        """A failed analyst must not manufacture unanimity by being absent."""
        assert self._gate(_Desk(
            fundamental_report={"thesis_direction": "BEARISH"},
        )) is False

    def test_empty_desk_does_not_fire(self):
        assert self._gate(_Desk()) is False

    def test_junior_catalyst_call_counts_as_a_stance(self):
        """desk_note has no thesis_direction; its catalyst_call carries the
        equivalent claim (added in the Phase 2 audit)."""
        assert self._gate(_Desk(
            fundamental_report={"thesis_direction": "BEARISH"},
            desk_note={"catalyst_call": {"direction": "BEARISH"}},
        )) is True

    def test_case_and_whitespace_are_normalized(self):
        assert self._gate(_Desk(
            fundamental_report={"thesis_direction": " bearish "},
            quant_report={"thesis_direction": "Bearish"},
        )) is True
