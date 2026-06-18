"""
Tests for V3 SharedDesk state machine.

Tests:
- Phase transition validation (valid and invalid)
- Artifact append + retrieval
- Context compression produces clean narrative
- Serialization/deserialization roundtrip
"""

import json
import pytest

from app.v3.shared_desk import SharedDesk, DeskPhase, PhaseOutcome


# ═══════════════════════════════════════════════════════════════════════════
# Phase Transition Tests
# ═══════════════════════════════════════════════════════════════════════════


class TestPhaseTransitions:
    """Tests for SharedDesk.advance_phase() with strict transition validation."""

    def test_valid_init_to_research_done(self):
        desk = SharedDesk(cycle_id="test-cycle", ticker="AAPL")
        desk.advance_phase(DeskPhase.RESEARCH_DONE)
        assert desk.phase == DeskPhase.RESEARCH_DONE
        assert desk.phase_outcomes["INIT"] == "SUCCESS"

    def test_valid_research_to_debate(self):
        desk = SharedDesk(cycle_id="test-cycle", ticker="AAPL")
        desk.advance_phase(DeskPhase.RESEARCH_DONE)
        desk.advance_phase(DeskPhase.DEBATE_DONE)
        assert desk.phase == DeskPhase.DEBATE_DONE

    def test_valid_debate_to_pm(self):
        desk = SharedDesk(cycle_id="test-cycle", ticker="AAPL")
        desk.advance_phase(DeskPhase.RESEARCH_DONE)
        desk.advance_phase(DeskPhase.DEBATE_DONE)
        desk.advance_phase(DeskPhase.PM_DONE)
        assert desk.phase == DeskPhase.PM_DONE

    def test_valid_init_to_aborted(self):
        desk = SharedDesk(cycle_id="test-cycle", ticker="AAPL")
        desk.advance_phase(DeskPhase.ABORTED, PhaseOutcome.TOOL_OUTAGE)
        assert desk.phase == DeskPhase.ABORTED
        assert desk.phase_outcomes["INIT"] == "TOOL_OUTAGE"

    def test_invalid_skip_phase(self):
        """Cannot skip from INIT directly to DEBATE_DONE."""
        desk = SharedDesk(cycle_id="test-cycle", ticker="AAPL")
        with pytest.raises(ValueError, match="Invalid transition"):
            desk.advance_phase(DeskPhase.DEBATE_DONE)

    def test_invalid_skip_to_pm(self):
        """Cannot skip from INIT directly to PM_DONE."""
        desk = SharedDesk(cycle_id="test-cycle", ticker="AAPL")
        with pytest.raises(ValueError, match="Invalid transition"):
            desk.advance_phase(DeskPhase.PM_DONE)

    def test_invalid_backward_transition(self):
        """Cannot go backward from RESEARCH_DONE to INIT."""
        desk = SharedDesk(cycle_id="test-cycle", ticker="AAPL")
        desk.advance_phase(DeskPhase.RESEARCH_DONE)
        with pytest.raises(ValueError, match="Invalid transition"):
            desk.advance_phase(DeskPhase.INIT)

    def test_terminal_pm_done(self):
        """PM_DONE is terminal — cannot advance further."""
        desk = SharedDesk(cycle_id="test-cycle", ticker="AAPL")
        desk.advance_phase(DeskPhase.RESEARCH_DONE)
        desk.advance_phase(DeskPhase.DEBATE_DONE)
        desk.advance_phase(DeskPhase.PM_DONE)
        with pytest.raises(ValueError, match="Invalid transition"):
            desk.advance_phase(DeskPhase.ABORTED)

    def test_terminal_aborted(self):
        """ABORTED is terminal — cannot advance further."""
        desk = SharedDesk(cycle_id="test-cycle", ticker="AAPL")
        desk.advance_phase(DeskPhase.ABORTED)
        with pytest.raises(ValueError, match="Invalid transition"):
            desk.advance_phase(DeskPhase.RESEARCH_DONE)

    def test_outcome_tracking(self):
        """Phase outcomes are recorded correctly."""
        desk = SharedDesk(cycle_id="test-cycle", ticker="AAPL")
        desk.advance_phase(DeskPhase.RESEARCH_DONE, PhaseOutcome.DATA_GAP)
        desk.advance_phase(DeskPhase.DEBATE_DONE, PhaseOutcome.SUCCESS)
        desk.advance_phase(DeskPhase.PM_DONE, PhaseOutcome.SUCCESS)

        assert desk.phase_outcomes["INIT"] == "DATA_GAP"
        assert desk.phase_outcomes["RESEARCH_DONE"] == "SUCCESS"
        assert desk.phase_outcomes["DEBATE_DONE"] == "SUCCESS"


# ═══════════════════════════════════════════════════════════════════════════
# Artifact Tests
# ═══════════════════════════════════════════════════════════════════════════


class TestArtifacts:
    """Tests for SharedDesk artifact management."""

    def test_append_valid_artifact(self):
        desk = SharedDesk(cycle_id="test-cycle", ticker="AAPL")
        artifact = {
            "summary": "Apple shows strong fundamentals.",
            "key_findings": ["Revenue up 12% YoY"],
            "data_gaps": [],
            "confidence": 75,
        }
        desk.append_artifact("desk_note", artifact)
        assert desk.desk_note is not None
        assert desk.desk_note["summary"] == "Apple shows strong fundamentals."
        assert "_appended_at" in desk.desk_note
        assert desk.desk_note["_artifact_type"] == "desk_note"

    def test_append_invalid_type_raises(self):
        desk = SharedDesk(cycle_id="test-cycle", ticker="AAPL")
        with pytest.raises(ValueError, match="Invalid artifact_type"):
            desk.append_artifact("invalid_type", {"summary": "test"})

    def test_has_artifact(self):
        desk = SharedDesk(cycle_id="test-cycle", ticker="AAPL")
        assert not desk.has_artifact("desk_note")
        desk.append_artifact("desk_note", {"summary": "test"})
        assert desk.has_artifact("desk_note")

    def test_get_research_artifacts(self):
        desk = SharedDesk(cycle_id="test-cycle", ticker="AAPL")
        desk.append_artifact("desk_note", {"summary": "note"})
        desk.append_artifact("fundamental_report", {"summary": "fund"})

        research = desk.get_research_artifacts()
        assert "desk_note" in research
        assert "fundamental_report" in research
        assert "quant_report" not in research
        assert len(research) == 2

    def test_get_debate_artifacts(self):
        desk = SharedDesk(cycle_id="test-cycle", ticker="AAPL")
        desk.append_artifact("bull_argument", {"summary": "bull"})
        desk.append_artifact("bear_rebuttal", {"summary": "bear"})

        debate = desk.get_debate_artifacts()
        assert "bull_argument" in debate
        assert "bear_rebuttal" in debate
        assert len(debate) == 2

    def test_artifact_overwrites_previous(self):
        """Appending the same artifact type overwrites the previous one."""
        desk = SharedDesk(cycle_id="test-cycle", ticker="AAPL")
        desk.append_artifact("desk_note", {"summary": "version 1"})
        desk.append_artifact("desk_note", {"summary": "version 2"})
        assert desk.desk_note["summary"] == "version 2"


# ═══════════════════════════════════════════════════════════════════════════
# Context Compression Tests
# ═══════════════════════════════════════════════════════════════════════════


class TestContextCompression:
    """Tests for SharedDesk.get_compressed_context()."""

    def test_empty_desk_returns_placeholder(self):
        desk = SharedDesk(cycle_id="test-cycle", ticker="AAPL")
        ctx = desk.get_compressed_context()
        assert ctx == "No artifacts on desk yet."

    def test_includes_desk_note_summary(self):
        desk = SharedDesk(cycle_id="test-cycle", ticker="AAPL")
        desk.append_artifact("desk_note", {
            "summary": "Apple is doing well",
            "key_findings": ["Revenue up 12%"],
            "data_gaps": ["No insider data"],
            "confidence": 70,
        })
        ctx = desk.get_compressed_context()
        assert "Apple is doing well" in ctx
        assert "Revenue up 12%" in ctx
        assert "DataGap: No insider data" in ctx

    def test_includes_fundamental_report(self):
        desk = SharedDesk(cycle_id="test-cycle", ticker="AAPL")
        desk.append_artifact("fundamental_report", {
            "summary": "Strong moat and earnings growth.",
            "thesis_direction": "BULLISH",
            "confidence": 80,
        })
        ctx = desk.get_compressed_context()
        assert "Strong moat" in ctx
        assert "BULLISH" in ctx
        assert "80%" in ctx

    def test_debate_excluded_by_default(self):
        desk = SharedDesk(cycle_id="test-cycle", ticker="AAPL")
        desk.append_artifact("bull_argument", {
            "summary": "Strong buy thesis",
            "confidence": 85,
        })
        ctx = desk.get_compressed_context(include_debate=False)
        assert "Strong buy thesis" not in ctx

    def test_debate_included_when_requested(self):
        desk = SharedDesk(cycle_id="test-cycle", ticker="AAPL")
        desk.append_artifact("bull_argument", {
            "summary": "Strong buy thesis",
            "confidence": 85,
        })
        ctx = desk.get_compressed_context(include_debate=True)
        assert "Strong buy thesis" in ctx
        assert "85%" in ctx

    def test_truncation_on_large_context(self):
        """Context must be truncated to prevent snowball."""
        desk = SharedDesk(cycle_id="test-cycle", ticker="AAPL")
        desk.append_artifact("desk_note", {
            "summary": "A" * 10000,
            "key_findings": [],
            "data_gaps": [],
            "confidence": 50,
        })
        ctx = desk.get_compressed_context()
        assert len(ctx) <= 8100  # _MAX_COMPRESSED_CONTEXT_CHARS + margin
        assert "TRUNCATED" in ctx


# ═══════════════════════════════════════════════════════════════════════════
# Serialization Tests
# ═══════════════════════════════════════════════════════════════════════════


class TestSerialization:
    """Tests for SharedDesk serialization roundtrip."""

    def test_to_dict_roundtrip(self):
        desk = SharedDesk(cycle_id="test-cycle", ticker="AAPL")
        desk.append_artifact("desk_note", {
            "summary": "Test note",
            "key_findings": ["Finding 1"],
            "data_gaps": [],
            "confidence": 60,
        })
        desk.advance_phase(DeskPhase.RESEARCH_DONE, PhaseOutcome.DATA_GAP)

        data = desk.to_dict()
        restored = SharedDesk.from_dict(data)

        assert restored.cycle_id == "test-cycle"
        assert restored.ticker == "AAPL"
        assert restored.phase == DeskPhase.RESEARCH_DONE
        assert restored.desk_note["summary"] == "Test note"
        assert restored.phase_outcomes["INIT"] == "DATA_GAP"

    def test_json_serialization(self):
        """Ensure to_dict() produces valid JSON."""
        desk = SharedDesk(cycle_id="c1", ticker="MSFT")
        desk.append_artifact("fundamental_report", {
            "summary": "Microsoft cloud growth",
            "pillars": {"revenue_growth": "15% YoY"},
            "thesis_direction": "BULLISH",
            "confidence": 82,
        })

        json_str = json.dumps(desk.to_dict(), default=str)
        assert len(json_str) > 0
        parsed = json.loads(json_str)
        assert parsed["ticker"] == "MSFT"

    def test_telemetry_roundtrip(self):
        desk = SharedDesk(cycle_id="test", ticker="GOOG")
        desk.record_agent_telemetry({
            "agent_name": "junior_analyst",
            "elapsed_ms": 1500,
            "outcome": "SUCCESS",
        })

        data = desk.to_dict()
        restored = SharedDesk.from_dict(data)
        assert len(restored.agent_telemetry) == 1
        assert restored.agent_telemetry[0]["agent_name"] == "junior_analyst"
