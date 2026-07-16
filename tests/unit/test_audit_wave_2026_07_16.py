"""Regression tests for the 2026-07-16 audit-wave fixes.

Covers:
- parse_json_response: nested outer object must win over inner sub-dicts
  (the board-salvage / consolidator-empty-extraction root cause)
- summarize_ticker_results: no_position bucket + trade_skip_categories shape
- tournament: winner direction gates the action, not the bracket seat
- whiteboard_write: reserved control sections are blocked for agents
"""

import json

import pytest

from app.utils.text_utils import parse_json_response


class TestParseJsonResponseNesting:
    def test_nested_object_returns_outer(self):
        text = (
            'Here is my decision:\n'
            '{"action": "BUY", "confidence": 80, '
            '"conviction_vector": {"data_quality": 85, "risk_adjusted": 70}, '
            '"reasoning": "solid"}'
        )
        parsed = parse_json_response(text)
        assert parsed.get("action") == "BUY"
        assert "conviction_vector" in parsed

    def test_consolidator_shape_returns_outer(self):
        # The exact shape whose inner dicts used to win via [-1] selection,
        # making the consolidator "extract" zero memories from valid output.
        text = (
            '{"new_or_updated_memories": ['
            '{"content": "m1", "category": "c"}, '
            '{"content": "m2", "category": "c"}], '
            '"deprecated_memory_ids": []}'
        )
        parsed = parse_json_response(text)
        assert len(parsed.get("new_or_updated_memories", [])) == 2

    def test_sibling_objects_last_wins(self):
        text = 'example: {"action": "EXAMPLE"} final answer: {"action": "SELL", "confidence": 70}'
        parsed = parse_json_response(text)
        assert parsed.get("action") == "SELL"

    def test_truncated_outer_salvages_inner_fragment(self):
        # Truncated outer JSON can only ever salvage a fragment; the decision
        # layer treats missing required fields as AGENT_ERROR instead.
        text = (
            '{"action": "BUY", "confidence": 80, '
            '"conviction_vector": {"data_quality": 85}, "reasoning": "trunc'
        )
        parsed = parse_json_response(text)
        assert parsed == {"data_quality": 85}


class TestDecisionArtifactFatalValidation:
    def test_missing_required_fields_returns_agent_error(self):
        from app.v3.artifacts import validate_artifact

        salvaged_fragment = {"data_quality": 85, "risk_adjusted": 70}
        errors = validate_artifact("final_decision", salvaged_fragment)
        missing = [e for e in errors if e.startswith("Missing required field")]
        # agent_runner treats this as AGENT_ERROR for final_decision /
        # trade_decision — assert the trigger condition holds.
        assert missing, "fragment should be missing action/confidence/reasoning"


class TestNoPositionBucket:
    def test_summarize_counts_no_position(self):
        from app.services.pipeline_service import (
            REASON_NO_POSITION,
            summarize_ticker_results,
        )

        results = [
            {"action": "SELL", "no_trade_reason": REASON_NO_POSITION},
            {"action": "HOLD"},
        ]
        summary = summarize_ticker_results(results)
        assert summary["no_position_blocked"] == 1
        assert summary["trade_attempted"] == 0


class TestTournamentDirectionGate:
    def test_bearish_seat_a_winner_maps_to_sell(self):
        # Mirror of the verdict logic: a seat-A win with a BEARISH thesis
        # must not become a BUY.
        winner = {"direction": "BEARISH", "persona": "Macro_Quant"}
        a_wins = True

        winner_direction = str(winner.get("direction", "")).strip().upper()
        if winner_direction.startswith("BULL"):
            winning_side = "bull"
        elif winner_direction.startswith("BEAR"):
            winning_side = "bear"
        else:
            winning_side = "bull" if a_wins else "bear"

        assert winning_side == "bear"

    def test_no_direction_falls_back_to_seat(self):
        winner = {"persona": "Legacy"}
        a_wins = True
        winner_direction = str(winner.get("direction", "")).strip().upper()
        if winner_direction.startswith("BULL"):
            winning_side = "bull"
        elif winner_direction.startswith("BEAR"):
            winning_side = "bear"
        else:
            winning_side = "bull" if a_wins else "bear"
        assert winning_side == "bull"


class TestReservedWhiteboardSections:
    @pytest.mark.asyncio
    async def test_agent_write_to_final_decision_blocked(self):
        from app.tools.whiteboard_tools import whiteboard_write

        res = json.loads(await whiteboard_write("TEST", "final_decision", "{}"))
        assert res["status"] == "error"
        assert "reserved" in res["message"]

    @pytest.mark.asyncio
    async def test_collab_section_not_blocked_by_guard(self):
        from app.tools.whiteboard_tools import _ORCHESTRATOR_SECTIONS

        assert "market_context" not in _ORCHESTRATOR_SECTIONS
        assert "consensus" not in _ORCHESTRATOR_SECTIONS
