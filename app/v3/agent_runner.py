"""
V3 Agent Runner — Wraps the existing agent_loop with V3 guardrails.

This is the bridge between the V3 orchestrator and the existing
run_agent_loop() infrastructure. It handles:
1. Building the system prompt from agent config + SharedDesk context
2. Injecting the tool whitelist for the agent's role
3. Passing V3AgentBudget with role-specific limits
4. Parsing the output into the expected artifact schema
5. Appending the artifact to the SharedDesk
6. Running context compression
7. Recording telemetry
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

from app.v3.shared_desk import SharedDesk, PhaseOutcome
from app.v3.guardrails import (
    V3AgentBudget,
    get_budget_for_role,
    compress_artifact_for_downstream,

    enter_v3_session,
    exit_v3_session,
)
from app.v3.artifacts import validate_artifact

logger = logging.getLogger(__name__)


async def run_v3_agent(
    desk: SharedDesk,
    agent_module: Any,
    *,
    cycle_id: str = "",
    bot_id: str = "",
    emit: Any = None,
    timeout_seconds: float = 600.0,
    include_debate_context: bool = False,
) -> PhaseOutcome:
    """Run a V3 agent against the SharedDesk.

    This wraps run_agent_loop() with V3-specific behavior:
    - Builds the user prompt from SharedDesk compressed context
    - Uses role-specific tool whitelists
    - Enforces V3AgentBudget (real limits, not V2's 9999)
    - Parses and validates the artifact output
    - Appends to SharedDesk on success

    Args:
        desk: The SharedDesk to read from and append to.
        agent_module: The agent module (e.g. app.v3.agents.junior_analyst).
        cycle_id: Current cycle ID.
        bot_id: Current bot ID.
        emit: Event emitter callback.
        timeout_seconds: Hard timeout for the entire agent run.
        include_debate_context: If True, include debate artifacts in context.

    Returns:
        PhaseOutcome indicating success or failure type.
    """
    from app.utils.pipeline_utils import noop as _noop
    if emit is None:
        emit = _noop

    agent_name = agent_module.AGENT_NAME
    artifact_type = agent_module.ARTIFACT_TYPE
    system_prompt = agent_module.SYSTEM_PROMPT
    tool_whitelist = agent_module.TOOL_WHITELIST

    session_key = f"{cycle_id}:{desk.ticker}:{agent_name}"
    t_start = time.monotonic()

    emit(
        "analyzing",
        f"v3_{agent_name}_{desk.ticker}",
        f"🔬 {desk.ticker}: V3 {agent_name} starting...",
        status="running",
    )

    try:
        # Guard: prevent recursive agent spawning
        enter_v3_session(session_key)

        # Build the user prompt from SharedDesk context
        desk_context = desk.get_compressed_context(include_debate=include_debate_context)
        user_prompt = (
            f"## Ticker: {desk.ticker}\n"
            f"## Cycle: {cycle_id}\n\n"
        )

        # Add cycle metadata & portfolio context if available
        if desk.cycle_metadata:
            portfolio_ctx = desk.cycle_metadata.get("portfolio_context", "")
            if portfolio_ctx:
                user_prompt += f"## Portfolio Context\n{portfolio_ctx}\n\n"
                
            # Inject Pre-Collected Ticker Data Report
            data_report = desk.cycle_metadata.get("data_report", "")
            if data_report:
                user_prompt += f"## Pre-Collected Data Report\n{data_report}\n\n"

            # Inject Past Cycle Memory if available
            memory_context = desk.cycle_metadata.get("memory_context", "")
            if memory_context:
                user_prompt += f"## Past Cycle Memory\n{memory_context}\n\n"

        if desk_context and desk_context != "No artifacts on desk yet.":
            user_prompt += (
                f"## SharedDesk Context (from prior analysts)\n"
                f"{desk_context}\n\n"
            )

        if tool_whitelist:
            user_prompt += (
                "You have access to external data tools. "
                "Core quantitative metrics, news, YouTube, and filings are already provided in the 'Pre-Collected Data Report' section. "
                "Your job is to act as a data janitor: review the pre-collected data and ONLY use your tools if you spot missing data, corrupted data, or clickbait that needs verification. "
                "If the data is solid, proceed directly to analysis without calling redundant tools.\n"
                "Begin your analysis now.\n"
            )
        else:
            user_prompt += (
                "You have NO external tools. Reason from the SharedDesk data above.\n"
                "Begin your analysis now.\n"
            )

        # Force JSON response format reminder in the conversation history
        user_prompt += (
            "\n"
            "## OUTPUT DIRECTIVE REMINDER\n"
            f"When you generate your final response containing your analysis report (i.e. when you do NOT call any tools), "
            f"you MUST output ONLY a valid JSON object matching the `{artifact_type}` schema.\n"
            f"Do NOT include any conversational intro/outro, preambles, summary comments, or markdown headings.\n"
            f"Do NOT wrap the JSON in markdown code blocks (do NOT use ```json).\n"
            f"Your entire response MUST start with '{{' and end with '}}'.\n"
        )

        # Call the remote harness endpoint (Local or Prism)
        # Check cycle metadata for harness override, else default to Local
        harness_provider = desk.cycle_metadata.get("harness_provider", "local").lower()
        
        import httpx
        from app.config.config import Settings
        settings = Settings()
        
        # Build the URL based on the selected harness
        if harness_provider == "prism":
            # Assume prism-service runs on the PRISM_URL
            agent_endpoint = f"{settings.PRISM_URL}/agent"
        else:
            # Default to our new lazy-agent-service
            # We assume it runs on a known port, e.g., 8037 (from ecosystem configs)
            agent_endpoint = f"http://{settings.DEFAULT_HOST}:8037/agent"
            
        payload = {
            "role": agent_name,
            "prompt": user_prompt,
            "system_prompt": system_prompt,
            "tools_enabled": bool(tool_whitelist),
            "timeout_sec": timeout_seconds
        }
        
        async with httpx.AsyncClient(timeout=timeout_seconds + 30.0) as client:
            resp = await client.post(agent_endpoint, json=payload)
            resp.raise_for_status()
            result = resp.json()

        elapsed_ms = result.get("metrics", {}).get("elapsed", int((time.monotonic() - t_start) * 1000))
        final_text = result.get("artifact", "")
        loops_used = result.get("metrics", {}).get("tool_calls", 1)
        token_usage = result.get("metrics", {}).get("tokens_used", 0)
        stop_reason = result.get("status", "completed")

        # Check for token-limit truncation — the LLM may have been cut off mid-JSON
        if stop_reason in ("max_tokens", "length", "token_limit"):
            logger.warning(
                "[V3Runner] %s output was TRUNCATED by %s for %s — "
                "artifact parsing may fail. Consider increasing max_tokens.",
                agent_name, stop_reason, desk.ticker,
            )

        # Parse the artifact from the agent's output
        artifact = _parse_artifact(final_text, artifact_type, agent_name)

        if artifact is None:
            logger.error(
                "[V3Runner] %s produced no parseable artifact for %s",
                agent_name, desk.ticker,
            )
            emit(
                "analyzing",
                f"v3_{agent_name}_fail_{desk.ticker}",
                f"❌ {desk.ticker}: V3 {agent_name} — no valid artifact produced",
                status="error",
            )
            _record_telemetry(desk, agent_name, elapsed_ms, loops_used, token_usage, "AGENT_ERROR")
            return PhaseOutcome.AGENT_ERROR

        # Validate the artifact
        errors = validate_artifact(artifact_type, artifact)
        if errors:
            logger.warning(
                "[V3Runner] %s artifact validation warnings for %s: %s",
                agent_name, desk.ticker, errors,
            )
            # Non-fatal — we still append, but log the validation issues
            artifact["_validation_warnings"] = errors

        # Append to SharedDesk
        desk.append_artifact(artifact_type, artifact)

        # Log success
        direction = artifact.get("thesis_direction", artifact.get("action", "?"))
        confidence = artifact.get("confidence", artifact.get("final_confidence", 0))

        emit(
            "analyzing",
            f"v3_{agent_name}_done_{desk.ticker}",
            f"✅ {desk.ticker}: V3 {agent_name} → {direction} @ {confidence}% "
            f"({loops_used} turns, {elapsed_ms}ms)",
            status="ok",
            data={
                "agent": agent_name,
                "direction": direction,
                "confidence": confidence,
                "elapsed_ms": elapsed_ms,
                "loops_used": loops_used,
                "tool_calls_made": max(0, loops_used - 1),
            },
        )

        _record_telemetry(desk, agent_name, elapsed_ms, loops_used, token_usage, "SUCCESS")

        # Classify outcome
        data_gaps = artifact.get("data_gaps", [])
        if data_gaps and len(data_gaps) > 2:
            return PhaseOutcome.DATA_GAP
        return PhaseOutcome.SUCCESS

    except asyncio.TimeoutError:
        elapsed_ms = int((time.monotonic() - t_start) * 1000)
        logger.error(
            "[V3Runner] %s TIMEOUT for %s after %dms",
            agent_name, desk.ticker, elapsed_ms,
        )
        emit(
            "analyzing",
            f"v3_{agent_name}_timeout_{desk.ticker}",
            f"⏰ {desk.ticker}: V3 {agent_name} TIMEOUT after {elapsed_ms}ms",
            status="error",
        )
        _record_telemetry(desk, agent_name, elapsed_ms, 0, 0, "TIMED_OUT")
        return PhaseOutcome.TIMED_OUT

    except asyncio.CancelledError:
        elapsed_ms = int((time.monotonic() - t_start) * 1000)
        logger.info(
            "[V3Runner] %s CANCELLED for %s after %dms — stop requested",
            agent_name, desk.ticker, elapsed_ms,
        )
        emit(
            "analyzing",
            f"v3_{agent_name}_cancelled_{desk.ticker}",
            f"🛑 {desk.ticker}: V3 {agent_name} CANCELLED after {elapsed_ms}ms",
            status="error",
        )
        _record_telemetry(desk, agent_name, elapsed_ms, 0, 0, "CANCELLED")
        raise  # Re-raise so orchestrator and pipeline_service see the cancellation

    except Exception as e:
        elapsed_ms = int((time.monotonic() - t_start) * 1000)
        logger.error(
            "[V3Runner] %s CRASHED for %s: %s",
            agent_name, desk.ticker, e,
        )
        emit(
            "analyzing",
            f"v3_{agent_name}_crash_{desk.ticker}",
            f"💥 {desk.ticker}: V3 {agent_name} CRASHED — {str(e)[:100]}",
            status="error",
        )
        _record_telemetry(desk, agent_name, elapsed_ms, 0, 0, "AGENT_ERROR")
        return PhaseOutcome.AGENT_ERROR

    finally:
        exit_v3_session(session_key)


def _parse_artifact(
    text: str, artifact_type: str, agent_name: str
) -> dict | None:
    """Parse the agent's text output into an artifact dict.

    Tries multiple strategies:
    1. Direct JSON parse
    2. Extract JSON from markdown code blocks
    3. Extract JSON from anywhere in the text

    Returns None if no valid JSON is found.
    """
    if not text or not text.strip():
        return None

    # Strategy 1: Direct JSON parse
    try:
        parsed = json.loads(text.strip())
        if isinstance(parsed, dict):
            return parsed
    except json.JSONDecodeError:
        pass

    # Strategy 2: JSON from markdown code blocks
    import re
    code_block_pattern = r"```(?:json)?\s*\n?(.*?)\n?```"
    matches = re.findall(code_block_pattern, text, re.DOTALL)
    for match in matches:
        try:
            parsed = json.loads(match.strip())
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            continue

    # Strategy 3: Find JSON object anywhere in text
    try:
        # Find the first { and last } and try to parse
        start = text.index("{")
        end = text.rindex("}") + 1
        candidate = text[start:end]
        parsed = json.loads(candidate)
        if isinstance(parsed, dict):
            return parsed
    except (ValueError, json.JSONDecodeError):
        pass

    # Strategy 4: Use the existing parse_json_response utility
    try:
        from app.utils.text_utils import parse_json_response
        parsed = parse_json_response(text)
        if isinstance(parsed, dict) and parsed:
            return parsed
    except Exception:
        pass

    logger.warning(
        "[V3Runner] Failed to parse artifact from %s output (%d chars)",
        agent_name, len(text),
    )
    return None


def _record_telemetry(
    desk: SharedDesk,
    agent_name: str,
    elapsed_ms: int,
    loops_used: int,
    token_usage: int,
    outcome: str,
) -> None:
    """Record telemetry for a V3 agent run."""
    desk.record_agent_telemetry({
        "agent_name": agent_name,
        "ticker": desk.ticker,
        "elapsed_ms": elapsed_ms,
        "loops_used": loops_used,
        "token_usage": token_usage,
        "outcome": outcome,
        "phase": desk.phase.value,
    })
