"""
Fundamental Analyst — Refactored to use dynamic Subagent tools.

This agent acts as the Supervisor for fundamental analysis. It receives the
Pre-Collected Data Report and must decide when to spawn specialized subagents
using `create_subagents` or `create_subagent` for Earnings, Balance Sheet, and Valuation.
"""

import logging
from typing import Any
from app.v3.shared_desk import SharedDesk, PhaseOutcome
from app.agents.base_agent import run_agent
from app.v3.artifacts import validate_artifact

logger = logging.getLogger(__name__)

AGENT_NAME = "v3_fundamental_analyst"
ARTIFACT_TYPE = "fundamental_report"
TOOL_WHITELIST = ["create_subagents", "create_subagent"]  # Will dynamically merge with Meta tools

SYSTEM_PROMPT = """You are the Senior Fundamental Analyst Supervisor.

Your job is to analyze the Pre-Collected Data Report for the target stock and synthesize a comprehensive `fundamental_report`. 

## DEEP RESEARCH VIA SUBAGENTS
You are managing a team of virtual subagents. If you need deep research into specific pillars (e.g. Earnings, Balance Sheet, Valuation), you MUST use the `create_subagents` tool to spin up specialized workers.
- Define a clear goal and specialized prompt for each subagent.
- Wait for their results before synthesizing your final JSON artifact.
- You can spawn multiple agents in parallel to handle distinct parts of the analysis.

## OUTPUT FORMAT
When you have gathered all necessary information, you MUST output valid JSON matching the `fundamental_report` schema:
{
    "summary": "2-3 paragraph fundamental analysis narrative",
    "pillars": {
        "revenue_growth": "Final synthesized growth assessment",
        "profitability": "Final synthesized profitability assessment",
        "moat": "Final synthesized moat assessment",
        "management": "Final synthesized management assessment",
        "valuation": "Final synthesized valuation assessment"
    },
    "thesis_direction": "BULLISH|BEARISH|NEUTRAL",
    "confidence": 0-100,
    "data_gaps": ["DataGap: [description of missing data]"],
    "catalysts": ["Upcoming catalysts"],
    "risks": ["Identified risks"]
}

CRITICAL OUTPUT DIRECTIVE:
You MUST respond ONLY with a raw JSON object matching the schema above.
Do NOT include any conversational introduction, summary takeaways, preambles, or markdown headings.
Do NOT wrap the JSON response in markdown code blocks (do NOT use ```json).
Your response MUST start with '{' and end with '}'."""

async def run_custom_agent(
    desk: SharedDesk,
    cycle_id: str,
    bot_id: str,
    emit: Any,
    timeout_seconds: float,
) -> PhaseOutcome:
    """Supervisor execution logic using dynamic subagent spawning."""
    
    data_report = desk.cycle_metadata.get("data_report", "")
    
    prompt = f"## Ticker: {desk.ticker}\n\n## Pre-Collected Data Report\n{data_report}\n\nAnalyze this data and synthesize the fundamental report."
    
    res = await run_agent(
        agent_name=AGENT_NAME,
        ticker=desk.ticker,
        cycle_id=cycle_id,
        bot_id=bot_id,
        system_prompt=SYSTEM_PROMPT,
        user_prompt=prompt,
        max_tokens=8192,
        enable_tools=True,
        harness_provider=desk.cycle_metadata.get("harness_provider", "local"),
    )

    try:
        from app.utils.text_utils import parse_json_response
        artifact = parse_json_response(res.get("response", ""))
    except Exception as e:
        logger.error("[V3 fundamental_analyst] Failed to parse supervisor output: %s", e)
        return PhaseOutcome.AGENT_ERROR

    errors = validate_artifact(ARTIFACT_TYPE, artifact)
    if errors:
        artifact["_validation_warnings"] = errors

    desk.append_artifact(ARTIFACT_TYPE, artifact)
    return PhaseOutcome.SUCCESS
