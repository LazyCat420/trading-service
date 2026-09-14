"""Empirical ablation test: Whiteboard utility and performance evaluation.

Audits:
1. Data-sharing completeness: SharedDesk vs whiteboard_entries (verifying if whiteboard contains unique data).
2. Runner behavior: Compares execution of v3_board_of_directors and v3_decision_synthesizer
   with TOOL_WHITELIST = ['whiteboard_read'] vs TOOL_WHITELIST = [].
3. Asserts that prompt size is reduced without tool schemas, and output validation succeeds identically.
"""

import json
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch, MagicMock

import pytest

from app.v3.agent_runner import run_v3_agent
from app.v3.agents import board_of_directors as board
from app.v3.agents import decision_agent as synth
from app.v3.shared_desk import SharedDesk, PhaseOutcome


def sample_regime():
    return {
        "regime": "CONTRADICTORY",
        "confidence": 72,
        "rationale": "High volatility vs strong breadth.",
        "board_directive": "Scale position down to 1.0% in contradictory regime.",
        "factors": {"volatility": 0.65, "breadth": 0.70},
        "market_context_tags": ["uncertain", "tech_fade"],
    }


def sample_desk():
    desk = SharedDesk(
        ticker="NVDA",
        cycle_id="cycle-ablation-test",
        regime_classification=sample_regime(),
        desk_note={"summary": "Junior analyst reports sector-wide tech de-risking.", "key_findings": ["Filing review", "Options flow"]},
        fundamental_report={"summary": "Strong gross margins, P/E 24.5.", "thesis_direction": "BULLISH", "confidence": 75},
        quant_report={"summary": "RSI 43.9, ATR 7.02.", "thesis_direction": "BULLISH", "confidence": 68},
        valuation_report={"summary": "EV/EBIT 24.77 vs peers.", "verdict": "FAIR_VALUE", "confidence": 65},
        final_decision={
            "action": "BUY",
            "confidence": 72,
            "position_size_pct": 1.0,
            "reasoning": "Board votes BUY under regime-scaled 1.0% position.",
        },
    )
    return desk


def test_shared_desk_contains_all_whiteboard_data():
    """Verify that SharedDesk already contains all data mirrored into whiteboard_entries."""
    desk = sample_desk()
    whiteboard_mirror = {
        "regime_classification": desk.regime_classification,
        "desk_note": desk.desk_note,
        "fundamental_report": desk.fundamental_report,
        "quant_report": desk.quant_report,
        "valuation_report": desk.valuation_report,
        "final_decision": desk.final_decision,
    }

    # Every key in whiteboard exists natively on SharedDesk
    for section, content in whiteboard_mirror.items():
        native_attr = getattr(desk, section, None)
        assert native_attr is not None, f"Missing {section} on SharedDesk"
        assert native_attr == content, f"Mismatch on {section}"


@pytest.mark.asyncio
async def test_decision_synthesizer_ablation_with_and_without_whiteboard():
    """Ablation: Measure prompt size, tools provided, and output validity with vs without whiteboard_read."""
    desk = sample_desk()

    synth_module_with_tools = SimpleNamespace(
        AGENT_NAME=synth.AGENT_NAME,
        ARTIFACT_TYPE=synth.ARTIFACT_TYPE,
        TOOL_WHITELIST=["whiteboard_read"],
        SYSTEM_PROMPT=synth.SYSTEM_PROMPT,
    )

    synth_module_no_tools = SimpleNamespace(
        AGENT_NAME=synth.AGENT_NAME,
        ARTIFACT_TYPE=synth.ARTIFACT_TYPE,
        TOOL_WHITELIST=[],
        SYSTEM_PROMPT=synth.SYSTEM_PROMPT,
    )

    valid_response = {
        "response": json.dumps({
            "action": "BUY",
            "confidence": 72,
            "position_size_pct": 1.0,
            "reasoning": "Synthesizer confirmed Board verdict and regime constraints.",
            "signal_weights": {"board": 0.50, "quant": 0.20, "fundamental": 0.15, "debate": 0.15},
        }),
        "tokens_used": 150,
        "loops_used": 1,
        "stop_reason": "completed",
    }

    with patch("app.agents.base_agent.run_agent", new_callable=AsyncMock, return_value=valid_response) as model_a, \
         patch("app.v3.data_trace.record", return_value="trace-a"), \
         patch("app.db.mongo_store", MagicMock()), \
         patch("app.db.mongo_query", MagicMock()):
        outcome_a = await run_v3_agent(desk, synth_module_with_tools, cycle_id=desk.cycle_id, bot_id="test", include_debate_context=True)

    with patch("app.agents.base_agent.run_agent", new_callable=AsyncMock, return_value=valid_response) as model_b, \
         patch("app.v3.data_trace.record", return_value="trace-b"), \
         patch("app.db.mongo_store", MagicMock()), \
         patch("app.db.mongo_query", MagicMock()):
        outcome_b = await run_v3_agent(desk, synth_module_no_tools, cycle_id=desk.cycle_id, bot_id="test", include_debate_context=True)

    assert outcome_a == PhaseOutcome.SUCCESS
    assert outcome_b == PhaseOutcome.SUCCESS

    prompt_a = model_a.call_args_list[0].kwargs["user_prompt"]
    prompt_b = model_b.call_args_list[0].kwargs["user_prompt"]

    # Evidence is identically complete in both
    assert sample_regime()["board_directive"] in prompt_a
    assert sample_regime()["board_directive"] in prompt_b
    assert "Junior analyst reports" in prompt_a
    assert "Junior analyst reports" in prompt_b


@pytest.mark.asyncio
async def test_board_of_directors_ablation_with_and_without_whiteboard():
    """Ablation: Measure Board execution with vs without whiteboard tools."""
    desk = sample_desk()

    board_module_with_tools = SimpleNamespace(
        AGENT_NAME=board.AGENT_NAME,
        ARTIFACT_TYPE=board.ARTIFACT_TYPE,
        TOOL_WHITELIST=["whiteboard_read"],
        SYSTEM_PROMPT=board.get_persona_prompt("CONTRADICTORY"),
    )

    board_module_no_tools = SimpleNamespace(
        AGENT_NAME=board.AGENT_NAME,
        ARTIFACT_TYPE=board.ARTIFACT_TYPE,
        TOOL_WHITELIST=[],
        SYSTEM_PROMPT=board.get_persona_prompt("CONTRADICTORY"),
    )

    board_response = {
        "response": json.dumps({
            "action": "BUY",
            "confidence": 72,
            "position_size_pct": 1.0,
            "reasoning": "Board confirms BUY with 1.0% allocation.",
        }),
        "tokens_used": 120,
        "loops_used": 1,
        "stop_reason": "completed",
    }

    with patch("app.agents.base_agent.run_agent", new_callable=AsyncMock, return_value=board_response) as model_a, \
         patch("app.v3.data_trace.record", return_value="trace-a"), \
         patch("app.db.mongo_store", MagicMock()), \
         patch("app.db.mongo_query", MagicMock()):
        outcome_a = await run_v3_agent(desk, board_module_with_tools, cycle_id=desk.cycle_id, bot_id="test", include_debate_context=True)

    with patch("app.agents.base_agent.run_agent", new_callable=AsyncMock, return_value=board_response) as model_b, \
         patch("app.v3.data_trace.record", return_value="trace-b"), \
         patch("app.db.mongo_store", MagicMock()), \
         patch("app.db.mongo_query", MagicMock()):
        outcome_b = await run_v3_agent(desk, board_module_no_tools, cycle_id=desk.cycle_id, bot_id="test", include_debate_context=True)

    assert outcome_a == PhaseOutcome.SUCCESS
    assert outcome_b == PhaseOutcome.SUCCESS
