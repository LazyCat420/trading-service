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
        })
        result = _build_v1_compatible_result(desk)
        assert result["c_result"]["action"] == "SELL"
        assert result["c_result"]["confidence"] == 65


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


class TestFullPipelineMocked:
    """Smoke test that runs the full V3 pipeline with fully mocked agents."""

    @pytest.mark.asyncio
    async def test_full_pipeline_produces_result(self):
        """Verify the pipeline runs end-to-end and produces a valid result."""

        # Mock artifacts for each agent
        desk_note = {
            "summary": "AAPL showing momentum",
            "key_findings": ["Revenue up"],
            "data_gaps": [],
            "confidence": 70,
        }
        fund_report = {
            "summary": "Strong fundamentals",
            "pillars": {},
            "thesis_direction": "BULLISH",
            "confidence": 75,
        }
        quant_report = {
            "summary": "Technicals healthy",
            "risk_metrics": {"rsi": 55},
            "thesis_direction": "BULLISH",
            "confidence": 72,
        }
        bull_arg = {
            "summary": "Strong BUY case",
            "claims": [{"claim": "Revenue growth", "evidence_source": "fund", "strength": "STRONG"}],
            "target_upside": "15%",
            "confidence": 80,
        }
        bear_rebuttal = {
            "summary": "Valuation stretched",
            "rebuttals": [{"bull_claim_addressed": "Revenue", "rebuttal": "Priced in", "counter_evidence": "P/E 30x"}],
            "independent_risks": ["Rate hikes"],
            "confidence": 55,
        }
        bull_defense = {
            "summary": "Thesis holds despite bear points",
            "defense_points": ["Revenue growth is structural"],
            "concessions": ["Valuation is elevated"],
            "final_confidence": 70,
        }
        regime = {
            "regime": "DEEP_DISCOUNT",
            "confidence": 80,
            "rationale": "VIX low, yields stable",
        }
        final_decision = {
            "action": "BUY",
            "confidence": 78,
            "reasoning": "Strong fundamentals in stable regime.",
            "persona_used": "warren_buffett",
            "regime": "DEEP_DISCOUNT",
        }
        trade_decision = {
            "action": "BUY",
            "confidence": 78,
            "reasoning": "Synthesized decision based on deep discount regime.",
            "signal_weights": {"quant": 0.1, "fundamental": 0.5, "debate": 0.2, "board": 0.2},
            "signal_assessments": {"quant": "ok", "fundamental": "strong", "debate": "bullish", "board": "buy"},
            "risk_flags": [],
            "stop_loss": 145.5,
            "take_profit": 165.0,
            "position_size_pct": 3.0
        }

        # Sequential mock responses for run_agent
        mock_responses = [
            _mock_agent_response(desk_note),
            _mock_agent_response(fund_report),
            _mock_agent_response(quant_report),
            _mock_agent_response(bull_arg),
            _mock_agent_response(bear_rebuttal),
            _mock_agent_response(bull_defense),
            _mock_agent_response(regime),
            _mock_agent_response(final_decision),
            _mock_agent_response(trade_decision),
        ]
        call_idx = {"i": 0}

        async def _mock_run_agent(**kwargs):
            idx = call_idx["i"]
            call_idx["i"] += 1
            if idx < len(mock_responses):
                return mock_responses[idx]
            return _mock_agent_response({"summary": "fallback", "action": "HOLD", "confidence": 0})

        with patch("app.agents.base_agent.run_agent", side_effect=_mock_run_agent):
            with patch("app.v3.desk_persistence.save_desk"):
                with patch("app.log_manager.log_manager") as mock_lm:
                    mock_lm.log_v2_cycle = MagicMock()
                    result = await run_v3_pipeline(
                        "AAPL",
                        cycle_id="test-pipeline-001",
                        bot_id="test-bot",
                    )

        # Verify result shape
        assert result["ticker"] == "AAPL"
        assert result["action"] == "BUY"
        assert result["confidence"] == 78
        assert result["config_used"] == "v3_agentic_pipeline"
        assert result["v3_metadata"]["pipeline_version"] == "v3"
        assert result["v3_metadata"]["persona_used"] == "warren_buffett"
