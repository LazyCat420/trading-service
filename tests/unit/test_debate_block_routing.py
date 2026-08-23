"""The debate argues from verified numbers, not prose (ch.90 fix A1).

The 2026-08-23 audit replayed 592 desks: bull/bear/defense/judge received NO
verified numeric block, and 1 in 7 checkable numbers in stored debate prose
matched nothing on the desk. These tests pin the widened routing — and its
deliberate limits: the composite decision score stays out of the arguing
seats, and book context goes only to the seats that weigh the book.
"""
import json
from unittest.mock import AsyncMock, patch

import pytest

from app.v3.agent_runner import run_v3_agent
from app.v3.shared_desk import SharedDesk

TECH = "## VERIFIED TECHNICAL BASELINE\nRSI-14: 41.2 | SMA-50: 100.10"
FUND = "## FUNDAMENTAL BASELINE\ngross_margin_ttm: 30.2%"
VAL = "## VALUATION MATH\nfair_value_estimate: 112.00"
SCORE = "## DECISION SCORE\ncomposite 61 band NEUTRAL risk/reward 1.8"
BOOK = "## BOOK BRIEF\nconcentration top-1 22% | corr-to-book 0.61"


def _module(agent_name):
    class _Mod:
        AGENT_NAME = agent_name
        ARTIFACT_TYPE = "desk_note"
        TOOL_WHITELIST: list[str] = []
        SYSTEM_PROMPT = f"You are {agent_name}. Output JSON."
    return _Mod


def _desk():
    desk = SharedDesk(ticker="TEST", cycle_id="cycle-T")
    desk.cycle_metadata = {
        "ticker": "TEST",
        "agent_locale": "default",
        "technical_baseline_context": TECH,
        "fundamental_context": FUND,
        "valuation_context": VAL,
        "decision_score_context": SCORE,
        "book_brief_context": BOOK,
        "data_report": "TEST at $101.",
    }
    return desk


def _result():
    return {"response": json.dumps({"summary": "x", "confidence": 50}),
            "tokens_used": 10, "loops_used": 1, "stop_reason": "completed"}


async def _prompt_for(agent_name, desk=None):
    captured = []

    async def _cap(**kw):
        captured.append(kw)
        return _result()

    with patch("app.agents.base_agent.run_agent",
               new=AsyncMock(side_effect=_cap)):
        await run_v3_agent(desk=desk or _desk(), agent_module=_module(agent_name),
                           cycle_id="cycle-T", bot_id="b1")
    assert captured, "run_agent was never reached"
    return captured[0]["system_prompt"] + "\n" + captured[0]["user_prompt"]


DEBATERS = ["v3_bull_agent", "v3_bear_agent", "v3_bull_defense",
            "v3_debate_judge"]


@pytest.mark.asyncio
@pytest.mark.parametrize("agent", DEBATERS)
async def test_every_debater_gets_the_fact_blocks(agent):
    payload = await _prompt_for(agent)
    assert TECH in payload, "technical baseline missing"
    assert FUND in payload, "fundamental baseline missing"
    assert VAL in payload, "valuation math missing"


@pytest.mark.asyncio
async def test_book_brief_reaches_bear_and_judge_only():
    for agent in ("v3_bear_agent", "v3_debate_judge"):
        assert BOOK in await _prompt_for(agent), agent
    for agent in ("v3_bull_agent", "v3_bull_defense"):
        assert BOOK not in await _prompt_for(agent), \
            f"{agent} argues one name, not the portfolio"


@pytest.mark.asyncio
async def test_decision_score_reaches_the_judge_but_no_arguing_seat():
    """The composite is a precomputed VERDICT: handing it to the arguing seats
    collapses the disagreement the debate exists to produce. The judge grades
    claims, so the structural gates/risk-reward are grading criteria."""
    assert SCORE in await _prompt_for("v3_debate_judge")
    for agent in ("v3_bull_agent", "v3_bear_agent", "v3_bull_defense"):
        assert SCORE not in await _prompt_for(agent), agent


@pytest.mark.asyncio
async def test_fact_blocks_survive_shed_pressure():
    """The blocks are _KEEP: a desk fat enough to force shedding must still
    deliver them (sheddable would silently recreate the 0% delivery the audit
    measured)."""
    desk = _desk()
    desk.cycle_metadata["memory_context"] = "FILLER " * 40000
    payload = await _prompt_for("v3_bear_agent", desk=desk)
    assert TECH in payload and FUND in payload and VAL in payload and BOOK in payload
