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


def _desk_with_oversized_context(cycle_id: str) -> SharedDesk:
    """A desk whose dynamic block cannot fit Prism's 2048-token memory embedder."""
    desk = SharedDesk(ticker="TEST", cycle_id=cycle_id)
    desk.cycle_metadata = {
        "ticker": "TEST",
        "agent_locale": "default",
        # Non-sheddable core, deliberately small so shedding CAN succeed.
        "data_report": "Price data: TEST at $101.",
        # Sheddable, and each is large enough to force the overflow.
        "memory_context": "M" * 2000,
        "previous_desk_context": "P" * 2000,
        "portfolio_context": "F" * 2000,
        "directives_context": "D" * 2000,
    }
    return desk


@pytest.mark.asyncio
async def test_oversized_context_sheds_instead_of_relocating():
    """Overflow must DROP low-priority sections, not move them to the system prompt.

    The old behaviour appended the whole oversized block to the system prompt,
    which kept every token in the payload (the model still received all of it)
    and silently defeated prefix caching. Only the embed error was avoided.
    """
    captured = []

    async def _capture_run_agent(**kwargs):
        captured.append(kwargs)
        return _fake_run_agent_result()

    with patch("app.agents.base_agent.run_agent", new=AsyncMock(side_effect=_capture_run_agent)):
        desk = _desk_with_oversized_context("cycle-big")
        await run_v3_agent(
            desk=desk, agent_module=_FakeAgentModule, cycle_id="cycle-big", bot_id="b1",
        )

    sys_prompt = captured[0]["system_prompt"]
    user_prompt = captured[0]["user_prompt"]

    # The system prompt stays pristine — no relocation, so prefix caching survives.
    assert sys_prompt == _FakeAgentModule.SYSTEM_PROMPT

    # The non-sheddable core survived...
    assert "MARKET DATA BRIEFING FOR THIS CYCLE" in user_prompt
    # ...and the lowest-priority sections were actually dropped, not moved.
    assert "Past Cycle Memory" not in user_prompt
    assert "Past Cycle Memory" not in sys_prompt

    # The payload genuinely shrank below the embedder budget (2048 - 400) * 3.
    assert len(user_prompt) < (2048 - 400) * 3


@pytest.mark.asyncio
async def test_max_tokens_is_measured_not_constant():
    """max_tokens must come from context_gate, not the old hardcoded 8192."""
    captured = []

    async def _capture_run_agent(**kwargs):
        captured.append(kwargs)
        return _fake_run_agent_result()

    with patch("app.agents.base_agent.run_agent", new=AsyncMock(side_effect=_capture_run_agent)):
        desk = _desk_with_dynamic_context("cycle-budget")
        await run_v3_agent(
            desk=desk, agent_module=_FakeAgentModule, cycle_id="cycle-budget", bot_id="b1",
        )

    max_tokens = captured[0]["max_tokens"]
    # Small prompt + no tools → the gate should allow the full requested ceiling.
    assert max_tokens == 8192

    # And a payload large enough to eat the window must reduce it.
    #
    # THE PAYLOAD MUST BE VARIED TEXT. This assertion was red on master for an
    # unknown period with `"x" * 400_000`, annotated "~100k tokens against a
    # 128k default window". It is not: BPE merges a run of identical characters
    # into 8-char tokens, so o200k_base scores that string at **exactly 50,000
    # tokens** — 400k chars of realistic prose is ~188,600. The payload fit the
    # window with 60k to spare, `context_gate` correctly declined to squeeze,
    # and the test condemned working code for it. Measured 2026-08-08.
    from app.services.context_gate import measure_payload
    from app.v3.agent_runner import _safe_max_tokens

    filler = "".join(
        f"paragraph {i} concerning the quarterly filing and its footnotes. "
        for i in range(9_000)
    )

    # The probe checks its own premise first. A payload that silently stopped
    # being oversized would otherwise make the real assertion below vacuous —
    # which is precisely the failure being fixed here.
    measured = measure_payload(
        [{"role": "system", "content": filler}, {"role": "user", "content": "y" * 40_000}],
        None, "", 128_000,
    )
    assert measured.total_input_tokens > 128_000 - 8192, (
        f"the probe is no longer oversized ({measured.total_input_tokens:,} tokens "
        "against a 128k window) — it cannot test a squeeze it does not trigger"
    )

    squeezed = _safe_max_tokens(
        agent_name="v3_junior_analyst",
        system_prompt=filler,
        user_prompt="y" * 40_000,
        tool_whitelist=None,
    )
    assert squeezed < 8192


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
