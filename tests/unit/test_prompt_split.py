"""
Tests for the KV-cache prompt split (plan 4.1/4.2/4.4).

Invariant: with V3_PROMPT_SPLIT enabled (the default), the system prompt sent
to the LLM is byte-identical to the agent module's static SYSTEM_PROMPT no
matter what cycle-specific context is on the desk — everything dynamic rides
in the user message. That invariance is what makes vLLM prefix caching work.
"""
import json
from unittest.mock import AsyncMock, patch

import pytest

from app.v3.agent_runner import run_v3_agent
from app.v3.shared_desk import SharedDesk, PhaseOutcome


class _FakeAgentModule:
    AGENT_NAME = "v3_junior_analyst"
    ARTIFACT_TYPE = "desk_note"
    TOOL_WHITELIST: list[str] = []
    SYSTEM_PROMPT = "You are the test Junior Analyst. Output JSON."


def _desk_with_dynamic_context(cycle_id: str) -> SharedDesk:
    desk = SharedDesk(ticker="TEST", cycle_id=cycle_id)
    desk.cycle_metadata = {
        "ticker": "TEST",
        "agent_locale": "default",
        "data_report": f"Price data for cycle {cycle_id}: TEST at $101.",
        "memory_context": "Past cycle: bought TEST, gained 3%.",
        "previous_desk_context": "Previous decision: HOLD @ 55% confidence",
        "portfolio_context": "CURRENTLY HOLDING TEST: Entry $95.00",
    }
    return desk


def _fake_run_agent_result():
    return {
        "response": json.dumps({
            "summary": "All quiet.",
            "key_findings": ["Nothing new"],
            "data_gaps": [],
            "confidence": 60,
            "leads_to_trace": [],
        }),
        "tokens_used": 100,
        "loops_used": 1,
        "stop_reason": "completed",
    }


@pytest.mark.asyncio
async def test_system_prompt_is_cycle_invariant_when_split_enabled():
    captured = []

    async def _capture_run_agent(**kwargs):
        captured.append(kwargs)
        return _fake_run_agent_result()

    with patch("app.agents.base_agent.run_agent", new=AsyncMock(side_effect=_capture_run_agent)):
        for cycle in ("cycle-A", "cycle-B"):
            desk = _desk_with_dynamic_context(cycle)
            outcome = await run_v3_agent(
                desk=desk, agent_module=_FakeAgentModule, cycle_id=cycle, bot_id="b1",
            )
            assert outcome in (PhaseOutcome.SUCCESS, PhaseOutcome.DATA_GAP)

    assert len(captured) == 2
    sys_a, sys_b = captured[0]["system_prompt"], captured[1]["system_prompt"]
    # System prompt: static, byte-identical across cycles, exactly the module prompt
    assert sys_a == sys_b == _FakeAgentModule.SYSTEM_PROMPT

    # The dynamic content must ride in the user message instead
    user_a = captured[0]["user_prompt"]
    assert "MARKET DATA BRIEFING FOR THIS CYCLE" in user_a
    assert "cycle-A" in user_a
    assert "Past Cycle Memory" in user_a
    assert "Manila Envelope" in user_a
    assert "Portfolio Context" in user_a
    # And it differs per cycle (it carries the cycle-specific data)
    assert user_a != captured[1]["user_prompt"]


@pytest.mark.asyncio
async def test_legacy_layout_when_split_disabled(monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "V3_PROMPT_SPLIT", False, raising=False)

    captured = []

    async def _capture_run_agent(**kwargs):
        captured.append(kwargs)
        return _fake_run_agent_result()

    with patch("app.agents.base_agent.run_agent", new=AsyncMock(side_effect=_capture_run_agent)):
        desk = _desk_with_dynamic_context("cycle-legacy")
        await run_v3_agent(
            desk=desk, agent_module=_FakeAgentModule, cycle_id="cycle-legacy", bot_id="b1",
        )

    # Rollback path: dynamic content goes back into the system prompt
    sys_prompt = captured[0]["system_prompt"]
    assert sys_prompt.startswith(_FakeAgentModule.SYSTEM_PROMPT)
    assert "MARKET DATA BRIEFING FOR THIS CYCLE" in sys_prompt
    assert "MARKET DATA BRIEFING" not in captured[0]["user_prompt"]


def test_handoff_brief_is_compact():
    desk = SharedDesk(ticker="TEST", cycle_id="c1")
    desk.desk_note = {
        "summary": "long summary " * 100,
        "key_findings": [f"finding number {i} with plenty of detail" for i in range(10)],
    }
    desk.final_decision = {
        "action": "BUY",
        "confidence": 72,
        "regime": "CONTRADICTORY",
        "reasoning": "words " * 200,
    }

    brief = desk.get_handoff_brief()

    assert "BUY" in brief and "72" in brief
    assert "CONTRADICTORY" in brief
    assert "finding number 0" in brief
    assert "finding number 3" not in brief  # top-3 only
    assert len(brief) <= 800


def test_handoff_brief_empty_desk():
    desk = SharedDesk(ticker="TEST", cycle_id="c1")
    assert desk.get_handoff_brief() == ""
