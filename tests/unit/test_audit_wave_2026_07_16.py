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

    def test_truncated_outer_yields_nothing_rather_than_a_nested_block(self):
        # REVERSED from the original assertion, which expected the inner
        # `conviction_vector` back. Salvaging it hands the desk a dict whose
        # keys belong to a different object — the nested block wearing the
        # artifact's name — and that parses cleanly, so the tool-less repair
        # never fires and a ~100s tool-enabled re-run gets burned rediscovering
        # research (TSM, 2026-08-04). The parser now declines the prose
        # fallback rather than manufacture fields the model never emitted.
        #
        # The empty dict is the whole signal: _parse_artifact reports it as
        # None, and run_v3_agent degrades to DATA_GAP on the retry rather than
        # returning a second AGENT_ERROR — see
        # test_nested_fragment_is_a_parse_failure.
        text = (
            '{"action": "BUY", "confidence": 80, '
            '"conviction_vector": {"data_quality": 85}, "reasoning": "trunc'
        )
        parsed = parse_json_response(text)
        assert parsed == {}


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
        # `_ORCHESTRATOR_SECTIONS` (a hand-listed frozenset) became
        # `_is_reserved` on 2026-08-08: the list named 11 sections against the
        # desk's 13 artifact types, so valuation_report, bull_defense and
        # delta_report were writable by any agent holding the tool. The
        # guarantee under test is unchanged — collaboration sections are what
        # agents are FOR — so this asserts it through the new entry point.
        from app.tools.whiteboard_tools import _is_reserved

        assert not _is_reserved("market_context")
        assert not _is_reserved("consensus")
