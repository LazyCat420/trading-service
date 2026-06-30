"""
Fundamental Analyst — Refactored into a Map-Reduce and P2P Supervisor.

Spawns specialized subagents in parallel (Earnings, Balance Sheet, Valuation),
runs a P2P cross-audit/consensus loop, and synthesizes findings into the final fundamental_report.
"""

import asyncio
import json
import logging
import time
from typing import Any

from app.v3.shared_desk import SharedDesk, PhaseOutcome
from app.v3.artifacts import validate_artifact
from app.agents.base_agent import run_agent

logger = logging.getLogger(__name__)

AGENT_NAME = "v3_fundamental_analyst"
ARTIFACT_TYPE = "fundamental_report"
TOOL_WHITELIST = []  # Run as supervisor, calls base_agent with worker whitelist
SYSTEM_PROMPT = "Supervisor Agent for Fundamental Analysis"

# Subagent Role Definitions & Prompts
SUBAGENT_EARNINGS_PROMPT = """You are the specialized Earnings & Growth Analyst.
Your job is to analyze the Pre-Collected Data Report for the target stock.
Evaluate revenue growth (YoY, QoQ, acceleration/deceleration) and profitability margins.
Output valid JSON:
{
    "revenue_growth": "Detailed assessment of revenue growth with figures",
    "profitability": "Detailed assessment of profit margins, operating leverage, and FCF",
    "confidence": 80
}"""

SUBAGENT_BALANCE_SHEET_PROMPT = """You are the specialized Balance Sheet & Moat Analyst.
Your job is to analyze the Pre-Collected Data Report for the target stock.
Evaluate balance sheet health (debt levels, cash reserves) and competitive moat.
Output valid JSON:
{
    "moat": "Detailed assessment of moat and competitive advantages",
    "management": "Detailed assessment of management quality and recent insider activity",
    "confidence": 80
}"""

SUBAGENT_VALUATION_PROMPT = """You are the specialized Valuation Analyst.
Your job is to analyze the Pre-Collected Data Report for the target stock.
Evaluate valuation multiples (P/E, PEG, P/S) relative to peers and overall reasonableness.
Output valid JSON:
{
    "valuation": "Detailed assessment of valuation relative to sector and historical metrics",
    "confidence": 80
}"""

async def run_custom_agent(
    desk: SharedDesk,
    cycle_id: str,
    bot_id: str,
    emit: Any,
    timeout_seconds: float,
) -> PhaseOutcome:
    """Map-Reduce & P2P Fundamental Analysis execution logic."""
    t_start = time.monotonic()
    
    emit(
        "analyzing",
        f"v3_{AGENT_NAME}_{desk.ticker}",
        f"🧠 {desk.ticker}: Spawning specialized fundamental subagents (Map phase)...",
        status="running",
    )

    # 1. Prepare base user prompt (Pre-collected data + context)
    data_report = desk.cycle_metadata.get("data_report", "")
    portfolio_ctx = desk.cycle_metadata.get("portfolio_context", "")
    memory_context = desk.cycle_metadata.get("memory_context", "")
    
    base_user_prompt = (
        f"## Ticker: {desk.ticker}\n"
        f"## Cycle: {cycle_id}\n\n"
    )
    if portfolio_ctx:
        base_user_prompt += f"## Portfolio Context\n{portfolio_ctx}\n\n"
    if data_report:
        base_user_prompt += f"## Pre-Collected Data Report\n{data_report}\n\n"
    if memory_context:
        base_user_prompt += f"## Past Cycle Memory\n{memory_context}\n\n"

    # 2. Map Phase: Spawn data-gathering subagents in parallel
    async def run_worker(role_name: str, system_prompt: str):
        worker_prompt = base_user_prompt + f"\nPerform your evaluation on {desk.ticker} according to your system prompt rules."
        res = await run_agent(
            agent_name="v3_worker_fundamental",
            ticker=desk.ticker,
            cycle_id=cycle_id,
            bot_id=bot_id,
            system_prompt=system_prompt,
            user_prompt=worker_prompt,
            max_tokens=4096,
            enable_tools=True,
            harness_provider=desk.cycle_metadata.get("harness_provider", "local"),
            model_override="Qwen/Qwen3.6-35B-A3B-FP8",
        )
        return role_name, res.get("response", "")

    results = []
    for role_name, subagent_prompt in [
        ("earnings", SUBAGENT_EARNINGS_PROMPT),
        ("balance_sheet", SUBAGENT_BALANCE_SHEET_PROMPT),
        ("valuation", SUBAGENT_VALUATION_PROMPT),
    ]:
        try:
            role, text = await run_worker(role_name, subagent_prompt)
            results.append((role, text))
        except Exception as e:
            logger.error("[V3 fundamental_analyst] Subagent %s failed: %s", role_name, e)
            results.append(e)
    
    # Parse subagent responses
    subagent_reports = {}
    for r in results:
        if isinstance(r, Exception):
            logger.error("[V3 fundamental_analyst] Subagent task failed: %s", r)
            continue
        role, text = r
        try:
            # Extract JSON from response
            from app.utils.text_utils import parse_json_response
            subagent_reports[role] = parse_json_response(text)
        except Exception as e:
            logger.warning("[V3 fundamental_analyst] Failed to parse subagent %s report: %s", role, e)
            subagent_reports[role] = {"error": "Failed to parse report", "confidence": 0}

    # Verify we gathered enough data to proceed
    if not subagent_reports.get("earnings") or not subagent_reports.get("balance_sheet") or not subagent_reports.get("valuation"):
        logger.error("[V3 fundamental_analyst] Map phase failed to return all subagent reports.")
        return PhaseOutcome.AGENT_ERROR

    # 3. Peer-to-Peer Cross-Audit (Consensus/Debate Phase)
    emit(
        "analyzing",
        f"v3_{AGENT_NAME}_p2p_{desk.ticker}",
        f"⚔️ {desk.ticker}: Subagents performing Peer-to-Peer cross-audit...",
        status="running",
    )

    p2p_scratchpad = (
        f"### Gathered Subagent Findings:\n"
        f"**Earnings & Growth:**\n{json.dumps(subagent_reports.get('earnings'), indent=2)}\n\n"
        f"**Balance Sheet & Moat:**\n{json.dumps(subagent_reports.get('balance_sheet'), indent=2)}\n\n"
        f"**Valuation:**\n{json.dumps(subagent_reports.get('valuation'), indent=2)}\n\n"
    )

    cross_audit_prompt = f"""You are the Peer-to-Peer Cross-Auditor.
Review the gathered subagent findings. Challenge any inconsistencies (e.g., if growth is slowing but valuation is extremely high).
Formulate adjustments and verify data.
Scratchpad:
{p2p_scratchpad}

Identify key consensus points and contradictions. Output valid JSON:
{{
    "consensus_points": ["Verified consensus point"],
    "contradictions": ["Contradiction or concern flagged"],
    "adjusted_confidence": 75
}}"""

    audit_res = await run_agent(
        agent_name="v3_worker_fundamental",
        ticker=desk.ticker,
        cycle_id=cycle_id,
        bot_id=bot_id,
        system_prompt="You are a quantitative auditor verifying peer findings.",
        user_prompt=cross_audit_prompt,
        max_tokens=2048,
        enable_tools=False,
        harness_provider=desk.cycle_metadata.get("harness_provider", "local"),
    )
    
    try:
        from app.utils.text_utils import parse_json_response
        audit_report = parse_json_response(audit_res.get("response", ""))
    except Exception:
        audit_report = {"consensus_points": [], "contradictions": ["Auditor failed to parse"], "adjusted_confidence": 70}

    # 4. Reduce/Synthesis Phase: Supervisor compiles the final report
    emit(
        "analyzing",
        f"v3_{AGENT_NAME}_synthesis_{desk.ticker}",
        f"📝 {desk.ticker}: Supervisor synthesizing final fundamental report...",
        status="running",
    )

    synthesis_prompt = f"""You are the Senior Fundamental Analyst Supervisor.
We have finished the subagent research and peer-to-peer audit.
Synthesize these inputs into the final, structured `fundamental_report`.

### Subagent Findings:
{p2p_scratchpad}

### Peer Audit Report:
{json.dumps(audit_report, indent=2)}

## OUTPUT FORMAT
You MUST output valid JSON matching the `fundamental_report` schema:
{{
    "summary": "2-3 paragraph fundamental analysis narrative",
    "pillars": {{
        "revenue_growth": "Final synthesized growth assessment",
        "profitability": "Final synthesized profitability assessment",
        "moat": "Final synthesized moat assessment",
        "management": "Final synthesized management assessment",
        "valuation": "Final synthesized valuation assessment"
    }},
    "thesis_direction": "BULLISH|BEARISH|NEUTRAL",
    "confidence": 0-100,
    "data_gaps": ["DataGap: [description of missing data]"],
    "catalysts": ["Upcoming catalysts"],
    "risks": ["Identified risks"]
}}"""

    final_res = await run_agent(
        agent_name=AGENT_NAME,
        ticker=desk.ticker,
        cycle_id=cycle_id,
        bot_id=bot_id,
        system_prompt="You are the Senior Fundamental Analyst Supervisor synthesizing final reports.",
        user_prompt=synthesis_prompt,
        max_tokens=4096,
        enable_tools=False,
        harness_provider=desk.cycle_metadata.get("harness_provider", "local"),
    )

    final_text = final_res.get("response", "")
    
    # Parse and validate final artifact
    try:
        from app.utils.text_utils import parse_json_response
        artifact = parse_json_response(final_text)
    except Exception:
        logger.error("[V3 fundamental_analyst] Supervisor failed to output parseable JSON.")
        return PhaseOutcome.AGENT_ERROR

    errors = validate_artifact(ARTIFACT_TYPE, artifact)
    if errors:
        logger.warning("[V3 fundamental_analyst] Artifact validation warnings: %s", errors)
        artifact["_validation_warnings"] = errors

    desk.append_artifact(ARTIFACT_TYPE, artifact)
    
    elapsed_ms = int((time.monotonic() - t_start) * 1000)
    desk.record_agent_telemetry({
        "agent_name": AGENT_NAME,
        "ticker": desk.ticker,
        "elapsed_ms": elapsed_ms,
        "loops_used": 3,
        "token_usage": 0,
        "outcome": "SUCCESS",
        "phase": desk.phase.value,
    })

    emit(
        "analyzing",
        f"v3_{AGENT_NAME}_done_{desk.ticker}",
        f"✅ {desk.ticker}: V3 Fundamental Analysis complete ({elapsed_ms}ms)",
        status="ok",
    )

    return PhaseOutcome.SUCCESS
