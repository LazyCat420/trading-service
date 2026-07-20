"""
Tests for the artifact salvage pass (v3/agent_runner.py).

A tool-enabled agent that reaches Prism's iteration ceiling is told to
summarize, and models frequently answer with one more *pseudo* tool call
written as prose — e.g. `call:mcp__lazy-tool-service__get_sec_filings{...}`.
Nothing executes it, so the literal string becomes the final answer and
artifact parsing fails, discarding the whole run's research and (after the
circuit breaker's one retry) aborting the ticker.

The salvage pass retries once with tools disabled, showing the model its own
unparseable reply and asking only for the JSON.
"""
import json
from unittest.mock import AsyncMock, patch

import pytest

from app.v3.agent_runner import run_v3_agent
from app.v3.shared_desk import SharedDesk, PhaseOutcome

# Verbatim from prism's conversation store for the WFC failure on 2026-07-19.
PSEUDO_TOOL_CALL = "call:mcp__lazy-tool-service__get_sec_filings{ticker:WFC}"

VALID_ARTIFACT = json.dumps({
    "summary": "Margins stable, leverage unchanged.",
    "key_findings": ["FCF positive"],
    "data_gaps": [],
    "confidence": 65,
    "leads_to_trace": [],
})


class _ToolEnabledAgent:
    AGENT_NAME = "v3_junior_analyst"
    ARTIFACT_TYPE = "desk_note"
    TOOL_WHITELIST = ["get_sec_filings", "get_market_data"]
    SYSTEM_PROMPT = "You are the test analyst. Output JSON."


class _ToollessAgent(_ToolEnabledAgent):
    TOOL_WHITELIST: list[str] = []


def _desk() -> SharedDesk:
    desk = SharedDesk(ticker="WFC", cycle_id="cycle-test")
    desk.cycle_metadata = {"ticker": "WFC", "agent_locale": "default"}
    return desk


def _result(response: str) -> dict:
    return {
        "response": response,
        "tokens_used": 100,
        "loops_used": 7,
        "stop_reason": "completed",
    }


@pytest.mark.asyncio
async def test_pseudo_tool_call_is_salvaged_into_an_artifact():
    """The failure that aborted FCF/ASML now recovers on the repair retry."""
    calls = []

    async def _run(**kwargs):
        calls.append(kwargs)
        # First call reproduces the ceiling behaviour; the repair call succeeds.
        return _result(PSEUDO_TOOL_CALL if len(calls) == 1 else VALID_ARTIFACT)

    with patch("app.agents.base_agent.run_agent", new=AsyncMock(side_effect=_run)):
        outcome = await run_v3_agent(
            desk=_desk(), agent_module=_ToolEnabledAgent,
            cycle_id="cycle-test", bot_id="b1",
        )

    assert outcome != PhaseOutcome.AGENT_ERROR, (
        "salvageable output should not surface as an agent error"
    )
    assert len(calls) == 2, "expected exactly one repair retry"

    repair = calls[1]
    assert repair["enable_tools"] is False, "repair must run with tools disabled"
    assert PSEUDO_TOOL_CALL in repair["user_prompt"], (
        "the model needs to see its own unparseable reply to correct it"
    )


@pytest.mark.asyncio
async def test_repair_is_not_attempted_for_toolless_agents():
    """No tools means no iteration ceiling, so the extra call is pure cost."""
    calls = []

    async def _run(**kwargs):
        calls.append(kwargs)
        return _result(PSEUDO_TOOL_CALL)

    with patch("app.agents.base_agent.run_agent", new=AsyncMock(side_effect=_run)):
        outcome = await run_v3_agent(
            desk=_desk(), agent_module=_ToollessAgent,
            cycle_id="cycle-test", bot_id="b1",
        )

    assert outcome == PhaseOutcome.AGENT_ERROR
    assert len(calls) == 1, "tool-less agents must not trigger a repair retry"


@pytest.mark.asyncio
async def test_still_fails_when_repair_also_returns_garbage():
    """The breaker must still engage when the output is genuinely unusable."""
    calls = []

    async def _run(**kwargs):
        calls.append(kwargs)
        return _result(PSEUDO_TOOL_CALL)

    with patch("app.agents.base_agent.run_agent", new=AsyncMock(side_effect=_run)):
        outcome = await run_v3_agent(
            desk=_desk(), agent_module=_ToolEnabledAgent,
            cycle_id="cycle-test", bot_id="b1",
        )

    assert outcome == PhaseOutcome.AGENT_ERROR
    assert len(calls) == 2, "one repair attempt, then give up"


@pytest.mark.asyncio
async def test_valid_first_response_skips_the_repair_path():
    """The happy path must not pay for an extra LLM call."""
    calls = []

    async def _run(**kwargs):
        calls.append(kwargs)
        return _result(VALID_ARTIFACT)

    with patch("app.agents.base_agent.run_agent", new=AsyncMock(side_effect=_run)):
        outcome = await run_v3_agent(
            desk=_desk(), agent_module=_ToolEnabledAgent,
            cycle_id="cycle-test", bot_id="b1",
        )

    assert outcome != PhaseOutcome.AGENT_ERROR
    assert len(calls) == 1
