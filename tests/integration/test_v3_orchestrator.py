"""
Integration Tests for V3 Orchestrator.

Tests the full pipeline with mocked LLM/Prism to verify:
- Phase transitions happen correctly
- Artifact flow between agents
- Circuit breaker behavior on failures
- V1-compatible result shape
"""

import asyncio
import json
import pytest
from unittest.mock import AsyncMock, patch, MagicMock

from app.v3.shared_desk import SharedDesk, DeskPhase, PhaseOutcome
from app.v3.orchestrator import (
    run_v3_pipeline,
    _build_v1_compatible_result,
    _build_noop_result,
    _build_cycle_metadata,
    _extract_debate_result,
)


# ═══════════════════════════════════════════════════════════════════════════
# Helper: mock agent response
# ═══════════════════════════════════════════════════════════════════════════


def _mock_agent_response(artifact: dict) -> dict:
    """Build a mock run_agent return value wrapping an artifact."""
    return {
        "agent": "test_agent",
        "ticker": "AAPL",
        "cycle_id": "test-cycle",
        "bot_id": "test-bot",
        "response": json.dumps(artifact),
        "tokens_used": 500,
        "execution_ms": 1000,
    }


# ═══════════════════════════════════════════════════════════════════════════
# V1-Compatible Result Tests
# ═══════════════════════════════════════════════════════════════════════════


class TestV1CompatibleResult:
    """Tests for _build_v1_compatible_result."""

    def test_basic_result_shape(self):
        desk = SharedDesk(cycle_id="test-cycle", ticker="AAPL")
        desk.append_artifact("final_decision", {
            "action": "BUY",
            "confidence": 80,
            "reasoning": "Strong fundamentals and momentum.",
            "persona_used": "warren_buffett",
            "regime": "DEEP_DISCOUNT",
            # Simulates real board output, which agent_runner stamps at the
            # append call site (2026-07-25). Unstamped it would be
            # `unattributed` — correct for a forgetful caller, wrong as a
            # stand-in for a board that genuinely decided.
            "decision_provenance": "board_reasoned",
        })

        result = _build_v1_compatible_result(desk, elapsed_s=5.0)

        # Required V1 keys
        assert result["ticker"] == "AAPL"
        assert result["action"] == "BUY"
        assert result["confidence"] == 80
        assert result["rationale"] == "Strong fundamentals and momentum."
        assert result["config_used"] == "v3_agentic_pipeline"
        assert isinstance(result["total_time_s"], float)
        assert isinstance(result["timestamp"], str)

        # V3-specific metadata
        assert result["v3_metadata"]["pipeline_version"] == "v3"
        assert result["v3_metadata"]["persona_used"] == "warren_buffett"
        assert result["v3_metadata"]["regime"] == "DEEP_DISCOUNT"

    def test_no_decision_defaults_to_hold(self):
        desk = SharedDesk(cycle_id="test-cycle", ticker="TSLA")
        result = _build_v1_compatible_result(desk)
        assert result["action"] == "HOLD"
        assert result["confidence"] == 0

    def test_c_result_present(self):
        desk = SharedDesk(cycle_id="test-cycle", ticker="GOOG")
        desk.append_artifact("final_decision", {
            "action": "SELL",
            "confidence": 65,
            "reasoning": "Overvalued.",
            "decision_provenance": "board_reasoned",  # real board output
        })
        result = _build_v1_compatible_result(desk)
        assert result["c_result"]["action"] == "SELL"
        assert result["c_result"]["confidence"] == 65

    def test_dynamic_trigger_and_levels_inherited_from_board_when_synthesizer_omits(self):
        desk = SharedDesk(cycle_id="test-cycle", ticker="NVDA")
        desk.append_artifact("final_decision", {
            "action": "BUY",
            "confidence": 72,
            "reasoning": "Strong technicals, entry extended.",
            "dynamic_trigger": {"type": "sma_50_drop", "value": 209.28},
            "stop_loss": 196.26,
            "take_profit": 245.00,
            "exit_style": "hard_stop",
            "position_size_pct": 2.5,
            "decision_provenance": "board_reasoned",
        })
        desk.append_artifact("trade_decision", {
            "action": "HOLD",
            "confidence": 63,
            "reasoning": "Waiting for dip to SMA-50 support.",
            "signal_weights": {"board": 0.45, "quant": 0.25, "fundamental": 0.15, "debate": 0.15},
            # Synthesizer omitted dynamic_trigger, stop_loss, take_profit
        })
        result = _build_v1_compatible_result(desk)
        assert result["estimate"]["dynamic_trigger"] == {"type": "sma_50_drop", "value": 209.28}
        assert result["estimate"]["stop_loss"] == 196.26
        assert result["estimate"]["take_profit"] == 245.00
        assert result["estimate"]["exit_style"] == "hard_stop"


class TestNoopResult:
    """Tests for _build_noop_result."""

    def test_noop_is_hold_zero_confidence(self):
        desk = SharedDesk(cycle_id="test-cycle", ticker="AAPL")
        result = _build_noop_result(desk, reason="Agent timeout")
        assert result["action"] == "HOLD"
        assert result["confidence"] == 0
        assert "timeout" in result["rationale"].lower()

    def test_noop_includes_abort_reason(self):
        desk = SharedDesk(cycle_id="test-cycle", ticker="AAPL")
        result = _build_noop_result(desk, reason="Circuit breaker tripped")
        assert "Circuit breaker" in result["rationale"]
        assert result["v3_metadata"]["abort_reason"] == "Circuit breaker tripped"


# ═══════════════════════════════════════════════════════════════════════════
# Debate Result Extraction Tests
# ═══════════════════════════════════════════════════════════════════════════


class TestDebateResultExtraction:
    """Tests for _extract_debate_result."""

    def test_no_debate_returns_none(self):
        desk = SharedDesk(cycle_id="test", ticker="AAPL")
        assert _extract_debate_result(desk) is None

    def test_bull_wins(self):
        desk = SharedDesk(cycle_id="test", ticker="AAPL")
        desk.append_artifact("bull_argument", {
            "summary": "Strong buy", "confidence": 80,
            "claims": [], "target_upside": "20%",
        })
        desk.append_artifact("bear_rebuttal", {
            "summary": "Weak rebuttal", "confidence": 40,
            "rebuttals": [], "independent_risks": [],
        })
        desk.append_artifact("bull_defense", {
            "summary": "Defense holds", "final_confidence": 75,
            "defense_points": [], "concessions": [],
        })
        # Winner selection comes from the debate judge artifact
        desk.append_artifact("debate_judge", {
            "winner": "bull", "final_confidence": 75, "summary": "Bull case holds",
        })

        result = _extract_debate_result(desk)
        assert result["winning_side"] == "bull"
        assert result["action"] == "BUY"

    def test_bear_wins(self):
        desk = SharedDesk(cycle_id="test", ticker="AAPL")
        desk.append_artifact("bull_argument", {
            "summary": "Moderate buy", "confidence": 50,
            "claims": [], "target_upside": "10%",
        })
        desk.append_artifact("bear_rebuttal", {
            "summary": "Strong rebuttal", "confidence": 85,
            "rebuttals": [], "independent_risks": [],
        })
        desk.append_artifact("bull_defense", {
            "summary": "Concedes", "final_confidence": 30,
            "defense_points": [], "concessions": [],
        })
        # Winner selection comes from the debate judge artifact
        desk.append_artifact("debate_judge", {
            "winner": "bear", "final_confidence": 85, "summary": "Bear case wins",
        })

        result = _extract_debate_result(desk)
        assert result["winning_side"] == "bear"
        assert result["action"] == "SELL"


# ═══════════════════════════════════════════════════════════════════════════
# Cycle Metadata Tests
# ═══════════════════════════════════════════════════════════════════════════


class TestCycleMetadata:
    """Tests for _build_cycle_metadata."""

    def test_basic_metadata(self):
        metadata = _build_cycle_metadata(
            ticker="AAPL", bot_id="bot1",
            macro_memo="VIX is elevated",
            research_focus="earnings",
            trigger_type="scheduled",
        )
        assert metadata["ticker"] == "AAPL"
        assert metadata["bot_id"] == "bot1"
        assert metadata["macro_memo"] == "VIX is elevated"
        assert metadata["research_focus"] == "earnings"
        assert metadata["trigger_type"] == "scheduled"
        assert "timestamp" in metadata

    def test_no_optional_fields(self):
        metadata = _build_cycle_metadata(ticker="MSFT", bot_id="bot2")
        assert "macro_memo" not in metadata
        assert "research_focus" not in metadata


# ═══════════════════════════════════════════════════════════════════════════
# Full Pipeline Smoke Test (Mocked)
# ═══════════════════════════════════════════════════════════════════════════

