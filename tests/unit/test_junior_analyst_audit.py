"""Tests for the 2026-07-24 Junior Analyst audit (Phase 2).

Measured over 337 live runs, the junior had two structural holes:

- `triage_recommendation` drives orchestrator routing (FULL runs the whole
  expensive panel) and the orchestrator treats anything unrecognized as FULL —
  but the field was MISSING in 90 of 337 runs (27%) with nothing logged, and
  SKIP had never once fired.
- it carried no direction field at all, so it was 0-for-53 "decisive" in the
  agent scorecard: structurally incapable of being right or wrong.
"""

import pytest

from app.v3.artifact_validators import validate_artifact as coerce
from app.v3.artifacts import validate_artifact as schema_check


def _note(**fields):
    base = {"summary": "s", "key_findings": ["f"], "data_gaps": [], "confidence": 60}
    base.update(fields)
    return base


class TestTriageIsRequired:
    def test_missing_triage_is_flagged(self):
        """It routed to the most expensive path silently for 27% of runs."""
        errors = schema_check("desk_note", _note())
        assert errors == ["Missing required field: triage_recommendation"]

    def test_complete_note_validates(self):
        assert schema_check("desk_note", _note(triage_recommendation="FULL")) == []

    @pytest.mark.parametrize("raw", ["full", "Full", "QUANT_ONLY", "skip"])
    def test_case_is_normalized(self, raw):
        out = coerce("desk_note", _note(triage_recommendation=raw))
        assert out["triage_recommendation"] == raw.upper()

    @pytest.mark.parametrize("raw", ["gibberish", "FULL|QUANT_ONLY|SKIP", "maybe"])
    def test_unroutable_values_become_full_explicitly(self, raw):
        """The orchestrator already fell back to FULL; make it visible in the
        stored artifact instead of implicit in routing."""
        out = coerce("desk_note", _note(triage_recommendation=raw))
        assert out["triage_recommendation"] == "FULL"
        assert any("triage_recommendation" in n for n in out.get("_validator_notes", []))


class TestCatalystCall:
    def test_direction_and_conviction_are_normalized(self):
        out = coerce("desk_note", _note(
            triage_recommendation="FULL",
            catalyst_call={"direction": "bullish", "conviction": "70",
                           "already_priced_in": "yes", "catalyst": "earnings beat"},
        ))
        call = out["catalyst_call"]
        assert call["direction"] == "BULLISH"
        assert call["conviction"] == 70.0
        assert call["already_priced_in"] is True
        assert call["catalyst"] == "earnings beat"

    def test_echoed_schema_literal_drops_the_call(self):
        out = coerce("desk_note", _note(
            triage_recommendation="FULL",
            catalyst_call={"direction": "BULLISH|BEARISH|NEUTRAL", "conviction": 150},
        ))
        assert "catalyst_call" not in out

    def test_call_without_direction_is_dropped(self):
        """A catalyst with no direction is a headline, not a claim."""
        out = coerce("desk_note", _note(
            triage_recommendation="FULL",
            catalyst_call={"catalyst": "earnings", "conviction": 50},
        ))
        assert "catalyst_call" not in out

    def test_non_dict_call_is_dropped(self):
        out = coerce("desk_note", _note(
            triage_recommendation="FULL", catalyst_call="it will go up"))
        assert "catalyst_call" not in out

    def test_conviction_is_clamped(self):
        out = coerce("desk_note", _note(
            triage_recommendation="FULL",
            catalyst_call={"direction": "BEARISH", "conviction": -5}))
        assert out["catalyst_call"]["conviction"] == 0.0

    def test_absent_call_is_not_invented(self):
        out = coerce("desk_note", _note(triage_recommendation="FULL"))
        assert "catalyst_call" not in out


class TestBudget:
    def test_junior_budget_allows_the_traced_lead(self):
        """96% of runs finished at the old 5-turn ceiling, so the depth-first
        trace in step 3 could never run."""
        from app.agents.tool_whitelists import get_agent_budget_turns

        assert get_agent_budget_turns("v3_junior_analyst", enable_tools=True) >= 7
        # No tools still means one generation turn — unchanged.
        assert get_agent_budget_turns("v3_junior_analyst", enable_tools=False) == 1


class TestPromptContract:
    """The prompt is the agent's spec; these pin the parts the code depends on."""

    def test_prompt_stops_mandating_a_redundant_whiteboard_read(self):
        from app.v3.agents.junior_analyst import SYSTEM_PROMPT

        assert "Do NOT call `whiteboard_read`" in SYSTEM_PROMPT

    def test_prompt_documents_both_new_output_fields(self):
        from app.v3.agents.junior_analyst import SYSTEM_PROMPT

        assert "catalyst_call" in SYSTEM_PROMPT
        assert "REQUIRED" in SYSTEM_PROMPT

    def test_whiteboard_read_stays_available_for_truncated_sections(self):
        """The injected summary truncates fat sections with a read-for-full
        pointer — the tool must still be reachable to follow it."""
        from app.v3.agents.junior_analyst import TOOL_WHITELIST

        assert "whiteboard_read" in TOOL_WHITELIST
        assert "whiteboard_write" in TOOL_WHITELIST
