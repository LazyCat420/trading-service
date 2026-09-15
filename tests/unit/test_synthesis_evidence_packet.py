"""Unit tests for the Decision Synthesizer unified evidence packet."""

import pytest
import json
from unittest.mock import MagicMock, patch

from app.v3.shared_desk import SharedDesk, DeskPhase
from app.v3.synthesis_evidence import build_synthesis_packet


def test_build_synthesis_packet_full_desk():
    """Verify that build_synthesis_packet produces an authoritative manifest and complete un-truncated text."""
    desk = SharedDesk(ticker="NVDA", cycle_id="test-cycle-synthesis")

    # 1. Populate Board verdict
    desk.append_artifact("final_decision", {
        "action": "BUY",
        "confidence": 78,
        "position_size_pct": 2.5,
        "stop_loss": 115.0,
        "take_profit": 140.0,
        "dynamic_trigger": {"type": "breakout", "level": 125.0},
        "reasoning": "Strong enterprise demand, margins intact, favorable regime.",
        "timing_override_reason": None,
    })

    # 2. Populate Debate
    desk.append_artifact("bull_argument", {
        "summary": "Data center acceleration continues with sovereign AI tailwinds.",
        "confidence": 85,
        "claims": ["Gross margin 75%", "Hyperscaler capex expanding"],
    })
    desk.append_artifact("bear_rebuttal", {
        "summary": "Customer concentration and supply chain bottlenecks present risk.",
        "confidence": 60,
        "independent_risks": ["Custom ASIC competition", "Capex digestion phase"],
    })
    desk.append_artifact("bull_defense", {
        "summary": "I concede custom ASICs are growing, but CUDA moat preserves dominance.",
        "final_confidence": 80,
        "thesis_survives": True,
        "concessions": ["ASIC share in inference is rising"],
        "independent_risks_answered": ["Blackwell architecture leap protects pricing"],
    })
    desk.append_artifact("debate_judge", {
        "winning_side": "bull",
        "confidence": 75,
        "summary": "Bull demonstrated superior structural defensibility.",
        "weaknesses_of_winner": ["Valuation multiple rich"],
        "strongest_point_of_loser": ["Capex deceleration risk"],
    })

    # 3. Populate Research
    desk.append_artifact("fundamental_report", {
        "thesis_direction": "BULLISH",
        "confidence": 82,
        "summary": "Revenue grew 122% YoY, operating margin expanded to 62%.",
        "metrics": {"pe_ratio": 45.2, "gross_margin": 0.75, "fcf_conversion": 0.88},
    })
    desk.append_artifact("quant_report", {
        "thesis_direction": "BULLISH",
        "confidence": 76,
        "summary": "Momentum score 88th percentile, low drawdown relative to semi index.",
        "risk_metrics": {"atr_14": 4.5, "rsi_14": 58.2, "beta": 1.45},
    })
    desk.append_artifact("valuation_report", {
        "verdict": "FAIRLY_VALUED",
        "confidence": 70,
        "summary": "DCF implies fair value near $130 with terminal growth 4%.",
        "fair_value_estimate": 130.0,
        "valuation_metrics": {"ev_to_ebit": 38.5},
    })
    desk.append_artifact("desk_note", {
        "summary": "High institutional interest ahead of earnings announcement.",
        "key_findings": ["Options implied move +/-7.5%", "Retail sentiment bullish"],
    })

    packet_text, receipt = build_synthesis_packet(desk)

    # Assertions on content
    assert "## SHAREDDESK COMPLETE EVIDENCE PACKET & DELIVERY MANIFEST" in packet_text
    assert "Do NOT call whiteboard_read for sections verified as COMPLETE" in packet_text
    assert "### 1. GOVERNING BOARD OF DIRECTORS VERDICT" in packet_text
    assert "**Action:** BUY | **Confidence:** 78% | **Position Size:** 2.5%" in packet_text
    assert "### 2. DEBATE JUDGE VERDICT" in packet_text
    assert "**Winner:** bull @ 75% confidence" in packet_text
    assert "### 3. ADVERSARIAL DEBATE CLAIMS & DEFENSE" in packet_text
    assert "CUDA moat preserves dominance" in packet_text
    assert "### 5. RESEARCH ANALYST FINDINGS & METRICS" in packet_text
    assert "pe_ratio=45.2" in packet_text
    assert "atr_14=4.5" in packet_text

    # Verify no truncation notices exist
    assert "TRUNCATED" not in packet_text
    assert "OMITTED:" not in packet_text
    assert "omitted evidence is unknown" not in packet_text

    # Assertions on receipt
    assert receipt["available"] is True
    assert receipt["complete"] is True
    assert receipt["board_action"] == "BUY"
    assert receipt["board_confidence"] == 78
    assert "final_decision" in receipt["sections_included"]
    assert "debate_judge" in receipt["sections_included"]
    assert "fundamental_report" in receipt["sections_included"]
    assert "quant_report" in receipt["sections_included"]
    assert "valuation_report" in receipt["sections_included"]
    assert receipt["chars"] > 2000
    assert len(receipt["sha256"]) == 64


def test_build_synthesis_packet_empty_desk():
    """Verify that build_synthesis_packet handles an empty/degraded desk gracefully."""
    desk = SharedDesk(ticker="TSLA", cycle_id="test-cycle-empty")

    packet_text, receipt = build_synthesis_packet(desk)

    assert "## SHAREDDESK COMPLETE EVIDENCE PACKET & DELIVERY MANIFEST" in packet_text
    assert "| `final_decision` | **UNAVAILABLE** |" in packet_text
    assert receipt["available"] is False
    assert receipt["board_action"] is None
    assert "final_decision" in receipt["sections_empty"]


@pytest.mark.asyncio
async def test_runner_synthesizer_packet_delivery():
    """Verify that run_v3_agent injects the synthesis packet and omits truncated summaries."""
    from app.v3.agent_runner import run_v3_agent
    from app.v3.agents import decision_agent
    from app.v3.shared_desk import PhaseOutcome

    desk = SharedDesk(ticker="NVDA", cycle_id="test-cycle-runner")
    desk.append_artifact("final_decision", {
        "action": "BUY",
        "confidence": 80,
        "position_size_pct": 3.0,
        "stop_loss": 110.0,
        "take_profit": 140.0,
        "reasoning": "Strong fundamentals",
    })

    captured_prompt = {}

    async def mock_run_agent(system_prompt, user_prompt, *args, **kwargs):
        captured_prompt["system"] = system_prompt
        captured_prompt["user"] = user_prompt
        artifact = {
            "action": "BUY",
            "confidence": 80,
            "reasoning": "Follows governing Board verdict",
            "signal_weights": {"board": 0.5, "quant": 0.2, "fundamental": 0.15, "debate": 0.15},
            "signal_assessments": {"board": "Strong", "quant": "Good", "fundamental": "Good", "debate": "Bullish"},
            "risk_flags": [],
            "stop_loss": 110.0,
            "take_profit": 140.0,
            "position_size_pct": 3.0,
        }
        return {
            "response": json.dumps(artifact),
            "loops_used": 1,
            "tokens_used": 500,
            "prompt_tokens": 400,
            "completion_tokens": 100,
            "stop_reason": "completed",
            "model_used": "GLM-5.3-Flash-EXL3",
            "provider": "vllm-2",
        }

    with patch("app.agents.base_agent.run_agent", side_effect=mock_run_agent), \
         patch("app.db.mongo_store.insert_docs"):
        outcome = await run_v3_agent(desk, decision_agent, cycle_id="test-cycle-runner")

    assert outcome == PhaseOutcome.SUCCESS
    assert "## SHAREDDESK COMPLETE EVIDENCE PACKET & DELIVERY MANIFEST" in captured_prompt["user"]
    assert "## SharedDesk Context Summary" not in captured_prompt["user"]
    assert "=== SHARED WHITEBOARD ===" not in captured_prompt["user"]

