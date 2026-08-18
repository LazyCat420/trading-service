"""The repair ask must say what THIS failure was (app/v3/agent_runner.py).

Before this, every unparseable reply got the same sentence — "Your previous
reply could not be parsed as the required artifact" — followed by the buffer.
That sentence is wrong for at least two of the measured classes: it tells a
model that returned nothing to correct a reply it never sent, and it tells a
model whose JSON was cut off to start writing JSON.

These tests assert on the prompt the repair call actually receives, which is
the only place the directive can be observed.
"""

import json
from unittest.mock import AsyncMock, patch

import pytest

from app.v3.agent_runner import run_v3_agent
from app.v3.output_rules import (
    EMPTY_RESPONSE,
    NARRATED_NO_ARTIFACT,
    TRUNCATED_JSON,
)
from app.v3.shared_desk import SharedDesk, PhaseOutcome

NARRATION = (
    "I have both the bull argument and bear rebuttal. Let me also check the "
    "whiteboard for the bull defense turn before I judge."
)
EMPTY_SENTINEL = "Agent failed: empty response from v3_junior_analyst"

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


def _desk() -> SharedDesk:
    desk = SharedDesk(ticker="WFC", cycle_id="cycle-test")
    desk.cycle_metadata = {"ticker": "WFC", "agent_locale": "default"}
    return desk


def _result(response: str) -> dict:
    return {"response": response, "tokens_used": 100, "loops_used": 7,
            "stop_reason": "completed"}


async def _run_with(first_reply: str, second_reply: str = VALID_ARTIFACT):
    """Drive one agent run whose first reply fails; return the repair kwargs."""
    calls = []

    async def _run(**kwargs):
        calls.append(kwargs)
        return _result(first_reply if len(calls) == 1 else second_reply)

    with patch("app.agents.base_agent.run_agent", new=AsyncMock(side_effect=_run)):
        outcome = await run_v3_agent(
            desk=_desk(), agent_module=_ToolEnabledAgent,
            cycle_id="cycle-test", bot_id="b1",
        )
    return outcome, calls


@pytest.mark.asyncio
async def test_narration_gets_the_narration_directive():
    outcome, calls = await _run_with(NARRATION)

    assert len(calls) == 2, "the repair pass must still run"
    prompt = calls[1]["user_prompt"]
    assert NARRATED_NO_ARTIFACT.directive in prompt
    assert outcome != PhaseOutcome.AGENT_ERROR


@pytest.mark.asyncio
async def test_the_empty_sentinel_is_never_quoted_back():
    """The buffer is ours, not the model's. Showing it as "your previous
    reply" is the defect — 58 v3_bear_agent runs in 7 days took that ask."""
    outcome, calls = await _run_with(EMPTY_SENTINEL)

    prompt = calls[1]["user_prompt"]
    assert EMPTY_SENTINEL not in prompt, (
        "a sentinel this service wrote must not be presented as the model's "
        "own output"
    )
    assert EMPTY_RESPONSE.directive in prompt
    assert "## PREVIOUS ATTEMPT" not in prompt


@pytest.mark.asyncio
async def test_a_truncated_artifact_is_quoted_from_its_tail():
    """The head of a truncated artifact is perfectly good JSON; the damage is
    at the end. A head-only excerpt shows the model none of it."""
    head = '{"summary": "' + ("margins stable. " * 400)
    truncated = head + "and the coverage rat"

    outcome, calls = await _run_with(truncated)

    prompt = calls[1]["user_prompt"]
    assert TRUNCATED_JSON.directive in prompt
    assert "and the coverage rat" in prompt, "the tail is what must be shown"
    assert prompt.count('{"summary": "margins stable') == 0, (
        "quoting the head would spend the excerpt budget on the intact part"
    )


@pytest.mark.asyncio
async def test_a_clean_run_never_classifies_or_repairs():
    """The happy path must not pay for the classifier's remediation."""
    outcome, calls = await _run_with(VALID_ARTIFACT)

    assert len(calls) == 1
    assert outcome != PhaseOutcome.AGENT_ERROR


@pytest.mark.asyncio
async def test_the_firing_is_recorded_with_its_repair_outcome():
    """A class that cannot be counted cannot be shown to have improved."""
    fired = []

    with patch("app.v3.telemetry.record_guardrail_firing",
               side_effect=lambda name, **kw: fired.append((name, kw))):
        outcome, calls = await _run_with(NARRATION)

    assert fired, "the classified failure must leave a queryable row"
    name, kw = fired[0]
    assert name == "output_rule:NARRATED_NO_ARTIFACT"
    assert kw["ticker"] == "WFC"
    assert kw["detail"]["agent"] == "v3_junior_analyst"
    assert kw["detail"]["repaired"] is True


@pytest.mark.asyncio
async def test_a_failed_repair_is_recorded_as_failed_not_as_untried():
    """`repaired` distinguishes three states and must not pool them: True,
    False (we tried), None (there was no repair path)."""
    fired = []

    with patch("app.v3.telemetry.record_guardrail_firing",
               side_effect=lambda name, **kw: fired.append((name, kw))):
        outcome, calls = await _run_with(NARRATION, second_reply=NARRATION)

    assert outcome == PhaseOutcome.AGENT_ERROR
    assert fired[0][1]["detail"]["repaired"] is False
