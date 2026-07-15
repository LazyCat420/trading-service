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
from app.v3.quality_scorer import score_artifact

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
    custom_instructions: str = "",
    parent_agent: str = "",
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
    # Scope any in-process tool execution (whiteboard, peer requests) to this
    # agent + cycle; the HTTP bridge path sets the same context from headers.
    from app.tools.tool_context import set_tool_context

    set_tool_context(
        agent_name=getattr(agent_module, "AGENT_NAME", None), cycle_id=cycle_id
    )
    from app.utils.pipeline_utils import noop as _noop
    if emit is None:
        emit = _noop

    agent_name = agent_module.AGENT_NAME
    artifact_type = agent_module.ARTIFACT_TYPE

    # Check for custom agent override execution
    if hasattr(agent_module, "run_custom_agent"):
        try:
            return await agent_module.run_custom_agent(
                desk=desk,
                cycle_id=cycle_id,
                bot_id=bot_id,
                emit=emit,
                timeout_seconds=timeout_seconds,
            )
        except Exception as custom_err:
            logger.error("[V3Runner] Custom agent execution failed: %s", custom_err)
            return PhaseOutcome.AGENT_ERROR

    system_prompt = agent_module.SYSTEM_PROMPT
    tool_whitelist = agent_module.TOOL_WHITELIST

    session_key = f"{cycle_id}:{desk.ticker}:{agent_name}"
    t_start = time.monotonic()

    emit(
        "analyzing",
        f"v3_{agent_name}_{desk.ticker}",
        f"🔬 {desk.ticker}: V3 {agent_name} starting...",
        status="running",
        data={"parent": parent_agent} if parent_agent else None,
    )

    try:
        # Guard: prevent recursive agent spawning
        enter_v3_session(session_key)

        # Build the user prompt from SharedDesk context
        desk_context = desk.get_compressed_context(include_debate=include_debate_context)
        
        # Inject current whiteboard summary (if any entries exist) for collaborative blackboard context
        try:
            from app.agents.whiteboard import whiteboard
            wb_summary = await whiteboard.summarize(ticker=desk.ticker, cycle_id=cycle_id)
            if wb_summary:
                system_prompt += f"\n\n{wb_summary}"
        except Exception as wb_err:
            logger.warning("[V3Runner] Failed to fetch whiteboard summary: %s", wb_err)

        user_prompt = (
            f"## Ticker: {desk.ticker}\n"
            f"## Cycle: {cycle_id}\n\n"
        )

        # Add cycle metadata & portfolio context if available (STATIC)
        if desk.cycle_metadata:
            portfolio_ctx = desk.cycle_metadata.get("portfolio_context", "")
            if portfolio_ctx:
                system_prompt += f"\n\n## Portfolio Context\n{portfolio_ctx}"
                
            # Use compressed data_report (summary only) if possible
            data_report = desk.cycle_metadata.get("data_report", "")
            if data_report:
                # Keep it concise to prevent prompt blowup
                if len(data_report) > 5000:
                    data_report = data_report[:5000] + "\n...[TRUNCATED FOR LENGTH]..."
                system_prompt += f"\n\n## Pre-Collected Data Report (Summary)\n{data_report}"

            # Inject Past Cycle Memory if available (STATIC)
            memory_context = desk.cycle_metadata.get("memory_context", "")
            if memory_context:
                system_prompt += f"\n\n## Past Cycle Memory\n{memory_context}"

            # Inject Previous Cycle's SharedDesk (Manila Envelope)
            previous_desk_context = desk.cycle_metadata.get("previous_desk_context", "")
            if previous_desk_context:
                system_prompt += f"\n\n## Previous Cycle's SharedDesk (Manila Envelope)\n{previous_desk_context}"

        # Add Tool/Reasoning Instructions (STATIC)
        if tool_whitelist:
            user_prompt += (
                "You have access to a specific subset of tools for your domain. "
                "Use them only if you need deeper research beyond the pre-collected data. "
                "Do not redundantly fetch data already provided.\n\n"
            )
        else:
            user_prompt += (
                "You have NO external tools. Reason from the SharedDesk data.\n\n"
            )

        # Force JSON response format reminder in the conversation history (STATIC)
        user_prompt += (
            "## OUTPUT DIRECTIVE REMINDER\n"
            f"When you generate your final response containing your analysis report (i.e. when you do NOT call any tools), "
            f"you MUST output ONLY a valid JSON object matching the `{artifact_type}` schema.\n"
            f"Do NOT include any conversational intro/outro, preambles, summary comments, or markdown headings.\n"
            f"Do NOT wrap the JSON in markdown code blocks (do NOT use ```json).\n"
            f"Your entire response MUST start with '{{' and end with '}}'.\n\n"
        )

        # Append concise SharedDesk Context summary
        if desk_context and desk_context != "No artifacts on desk yet.":
            system_prompt += (
                f"\n\n## SharedDesk Context Summary\n"
                f"{desk_context}"
            )

        # Append custom peer instructions if requested
        if custom_instructions:
            user_prompt += (
                f"\n## Peer Request / Instructions\n"
                f"A peer agent requested your specific analysis:\n"
                f"\"{custom_instructions}\"\n\n"
                f"Address this request directly in your findings.\n\n"
            )

        # Append locale directive if set
        agent_locale = desk.cycle_metadata.get("agent_locale", "default")
        if agent_locale and agent_locale != "default":
            try:
                from app.config.locales import AGENT_LOCALES
                locale_override = AGENT_LOCALES.get(agent_locale)
                if locale_override:
                    system_prompt += locale_override
                    logger.info(
                        "[V3Runner] %s: agent_locale '%s' directive appended to system prompt",
                        agent_name, agent_locale,
                    )
                else:
                    logger.warning(
                        "[V3Runner] %s: unknown agent_locale '%s' — no directive applied "
                        "(known: %s)", agent_name, agent_locale, sorted(AGENT_LOCALES),
                    )
            except Exception as e:
                logger.warning("[V3Runner] Failed to apply agent_locale %s: %s", agent_locale, e)

        user_prompt += "Begin your analysis now.\n"

        # Call via base_agent.run_agent() which handles:
        # - Dynamic prompt generation
        # - Harness routing (Local/Prism)
        # - Real message & tool execution flow
        from app.agents.base_agent import run_agent


        model_override = getattr(agent_module, "MODEL_OVERRIDE", None)

        prism_overrides = desk.cycle_metadata.get("prism_overrides", {})

        result = await asyncio.wait_for(
            run_agent(
                agent_name=agent_name,
                ticker=desk.ticker,
                cycle_id=cycle_id,
                bot_id=bot_id,
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                max_tokens=8192,
                enable_tools=bool(tool_whitelist),
                model_override=model_override,
                prism_overrides=prism_overrides,
            ),
            timeout=timeout_seconds,
        )

        elapsed_ms = int((time.monotonic() - t_start) * 1000)
        final_text = result.get("response", "")
        loops_used = result.get("loops_used", 1)
        token_usage = result.get("tokens_used", 0)
        stop_reason = result.get("stop_reason", "completed")

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

        # Quality scoring — detect dead ends / weak artifacts
        quality_result = score_artifact(artifact_type, artifact)
        quality_score = quality_result.get("quality_score", -1)
        quality_flag = quality_result.get("flag", "unknown")
        failure_patterns = quality_result.get("failure_patterns", [])

        if quality_flag == "dead_end":
            logger.warning(
                "[V3Runner] %s produced DEAD END artifact for %s "
                "(quality=%d, patterns=%s)",
                agent_name, desk.ticker, quality_score, failure_patterns,
            )
        elif quality_flag == "weak":
            logger.info(
                "[V3Runner] %s produced WEAK artifact for %s (quality=%d)",
                agent_name, desk.ticker, quality_score,
            )

        # Store quality info on the artifact itself for downstream visibility
        artifact["_quality_score"] = quality_score
        artifact["_quality_flag"] = quality_flag
        if failure_patterns:
            artifact["_failure_patterns"] = failure_patterns

        # Log success
        direction = artifact.get("thesis_direction", artifact.get("action", "?"))
        confidence = artifact.get("confidence", artifact.get("final_confidence", 0))

        quality_emoji = "🟢" if quality_flag == "good" else "🟡" if quality_flag == "weak" else "🔴"

        emit(
            "analyzing",
            f"v3_{agent_name}_done_{desk.ticker}",
            f"✅ {desk.ticker}: V3 {agent_name} → {direction} @ {confidence}% "
            f"({loops_used} turns, {elapsed_ms}ms) {quality_emoji} Q:{quality_score}",
            status="ok",
            data={
                "agent": agent_name,
                "direction": direction,
                "confidence": confidence,
                "elapsed_ms": elapsed_ms,
                "loops_used": loops_used,
                "tool_calls_made": max(0, loops_used - 1),
                "quality_score": quality_score,
                "quality_flag": quality_flag,
            },
        )

        _record_telemetry(desk, agent_name, elapsed_ms, loops_used, token_usage, "SUCCESS", quality_score)

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
    quality_score: int = -1,
) -> None:
    """Record telemetry for a V3 agent run."""
    entry = {
        "agent_name": agent_name,
        "ticker": desk.ticker,
        "elapsed_ms": elapsed_ms,
        "loops_used": loops_used,
        "token_usage": token_usage,
        "outcome": outcome,
        "phase": desk.phase.value,
        "quality_score": quality_score,
    }
    desk.record_agent_telemetry(entry)
