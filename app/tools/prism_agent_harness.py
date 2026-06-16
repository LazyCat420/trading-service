"""
Prism Agent Harness — Phase 6: Onion Layered Architecture.

Delegates the agentic tool-calling loop to Prism Gateway so that:
  Layer 1 (trading-cycle-backend): Defines tools, holds data state.
  Layer 2 (Prism Gateway):         Runs the agentic loop, tracks everything.
  Layer 3 (Hermes/vLLM):           Executes raw LLM completions.

This module provides `run_prism_agent()` as a drop-in replacement for
`run_tool_agent()` when you want Prism to manage the loop instead of
the local executor.py while loop.

When Prism is unhealthy, it transparently falls back to the local
executor so the pipeline never stalls.

IMPORTANT: This only changes code in the trading-cycle-backend.
           No Prism code is modified.
"""

import asyncio
import json
import logging
import time
import uuid
from typing import Any

import httpx

from app.config import settings
from app.services.prism_client import PrismClient
from app.services.vllm_client import llm, Priority
from app.tools.registry import registry

logger = logging.getLogger(__name__)


# ── Data Structures ────────────────────────────────────────────────────

class PrismAgentResult:
    """Structured result from a Prism-delegated agent run."""

    def __init__(
        self,
        final_text: str,
        token_usage: int,
        execution_ms: int,
        conversation_id: str,
        routed_via: str,  # "prism" or "local_fallback"
        tool_history: list[str] | None = None,
    ):
        self.final_text = final_text
        self.token_usage = token_usage
        self.execution_ms = execution_ms
        self.conversation_id = conversation_id
        self.routed_via = routed_via
        self.tool_history = tool_history or []

    def to_dict(self) -> dict[str, Any]:
        return {
            "final_text": self.final_text,
            "token_usage": self.token_usage,
            "execution_ms": self.execution_ms,
            "conversation_id": self.conversation_id,
            "routed_via": self.routed_via,
            "tool_history": self.tool_history,
        }


# ── JSON Recovery Helper ───────────────────────────────────────────────

async def recover_json_output(
    final_text: str,
    agent_name: str,
    ticker: str,
    cycle_id: str,
    bot_id: str,
    priority: Priority,
) -> tuple[dict, str, int, int]:
    """Attempt fast JSON recovery based on agent schema."""
    from app.utils.text_utils import parse_json_response

    if agent_name == "pre_trade":
        recovery_system = (
            "You are a precise data converter. Your job is to extract the structured pre-trade risk decision "
            "from the provided unstructured analysis text and output it as a strictly valid JSON object."
        )
        recovery_user = (
            "Here is the unstructured analysis text:\n"
            f"{final_text}\n\n"
            "Extract the risk fields and output EXACTLY this JSON format (no markdown formatting, no other text, just the raw JSON object):\n"
            "{\n"
            '  "decision": "APPROVE" or "VETO",\n'
            '  "ticker": "<ticker>",\n'
            '  "shares": <shares count, integer>,\n'
            '  "entry_price": <entry price, float>,\n'
            '  "stop_loss": <stop loss price, float>,\n'
            '  "risk_reward_ratio": <risk reward ratio, float>,\n'
            '  "position_pct": <position percentage, float>,\n'
            '  "total_cost": <total trade cost, float>,\n'
            '  "veto_reason": null or "<veto reason string>",\n'
            '  "rationale": "<brief explanation of the decision>"\n'
            "}\n"
            "If the text does not specify some values, calculate them: total_cost = shares * entry_price. If the decision is not clear, decide based on the tone."
        )
    elif agent_name == "portfolio_allocator":
        recovery_system = (
            "You are a precise data converter. Your job is to extract the structured portfolio allocation decisions "
            "from the provided unstructured analysis text and output it as a strictly valid JSON object."
        )
        recovery_user = (
            "Here is the unstructured analysis text:\n"
            f"{final_text}\n\n"
            "Extract the allocations and output EXACTLY this JSON format (no markdown formatting, no other text, just the raw JSON object):\n"
            "{\n"
            '  "allocations": [\n'
            "    {\n"
            '      "ticker": "<ticker>",\n'
            '      "decision": "APPROVE" or "VETO",\n'
            '      "adjusted_size_pct": <percentage, float>,\n'
            '      "shares": <shares count, integer>,\n'
            '      "total_cost": <total trade cost, float>,\n'
            '      "veto_reason": null or "<veto reason string>",\n'
            '      "rationale": "<explanation>"\n'
            "    }\n"
            "  ]\n"
            "}\n"
            "If the decision is not clear, decide based on the tone."
        )
    else:
        recovery_system = (
            "You are a precise data converter. Your job is to extract the structured financial decision "
            "from the provided unstructured analysis text and output it as a strictly valid JSON object."
        )
        recovery_user = (
            "Here is the unstructured analysis text:\n"
            f"{final_text}\n\n"
            "Extract the following fields and output EXACTLY this JSON format (no markdown formatting or other text, just the raw JSON object):\n"
            "{\n"
            '  "action": "BUY" or "SELL",\n'
            '  "claims": ["claim 1 with source citation", "claim 2...", ...],\n'
            '  "confidence": <integer 0-100>,\n'
            '  "key_argument": "single strongest argument"\n'
            "}\n"
            "If the text does not specify claims or arguments, fill them in based on the text. If the action is not clear, decide based on the tone."
        )

    try:
        recovered_text, rec_tokens, rec_ms = await llm.chat(
            system=recovery_system,
            user=recovery_user,
            temperature=0.1,
            max_tokens=8192,
            priority=priority,
            agent_name=agent_name + "_recovery",
            ticker=ticker,
            cycle_id=cycle_id,
            bot_id=bot_id,
        )
        recovered_parsed = parse_json_response(recovered_text)

        # Verify success based on agent schema requirements
        success = False
        if agent_name == "pre_trade":
            if recovered_parsed and "decision" in recovered_parsed and "ticker" in recovered_parsed:
                success = True
        elif agent_name == "portfolio_allocator":
            if recovered_parsed and "allocations" in recovered_parsed:
                success = True
        else:
            if recovered_parsed and "action" in recovered_parsed and "claims" in recovered_parsed:
                success = True

        if success:
            logger.info(
                "[PrismHarness] Fast JSON recovery succeeded for %s: %s",
                agent_name,
                recovered_parsed.get("decision") or recovered_parsed.get("action") or "allocations"
            )
            return recovered_parsed, json.dumps(recovered_parsed), rec_tokens, rec_ms
    except Exception as re_err:
        logger.warning("[PrismHarness] Fast JSON recovery failed for %s: %s", agent_name, re_err)

    return {}, final_text, 0, 0


def clean_surrogates(obj: Any) -> Any:
    """Recursively clean lone surrogate characters from strings to prevent encoding errors."""
    if isinstance(obj, str):
        return "".join(c for c in obj if not (0xD800 <= ord(c) <= 0xDFFF))
    elif isinstance(obj, dict):
        return {clean_surrogates(k): clean_surrogates(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [clean_surrogates(x) for x in obj]
    return obj


# ── Core Function ──────────────────────────────────────────────────────

async def run_prism_agent(
    system_prompt: str,
    user_prompt: str,
    ticker: str,
    agent_name: str = "prism_agent",
    cycle_id: str = "",
    bot_id: str = "",
    priority: Priority = Priority.NORMAL,
    tools_override: list[dict] | None = None,
    max_tokens: int = 2048,
    temperature: float = 0.3,
    timeout_seconds: int = 300,
    actor_label: str | None = None,
) -> dict[str, Any]:
    """Run an agent via Prism Gateway's /agent endpoint.

    Prism handles the full agentic loop (LLM → tool call → LLM → ...),
    logging every step natively. This gives you complete visibility
    in the Prism dashboard.

    When Prism is unhealthy, falls back to the local `run_tool_agent`.

    Args:
        system_prompt: System prompt for the agent.
        user_prompt: User prompt / task description.
        ticker: Stock ticker context.
        agent_name: Agent identifier for tracking.
        cycle_id: Trading cycle ID for session grouping.
        bot_id: Bot ID for tracking.
        priority: Queue priority level.
        tools_override: Optional tool schemas. If None, uses all registry tools.
        max_tokens: Max tokens for the LLM response.
        temperature: LLM temperature.
        timeout_seconds: Max time for the full agent run.
        actor_label: Optional label for the actor/username overriding default.

    Returns:
        dict with keys: final_text, token_usage, execution_ms,
        conversation_id, routed_via.
    """
    from app.telemetry import send_system_log
    send_system_log("AGENT", f"[{agent_name}] Starting agent execution (ticker={ticker})")
    start = time.monotonic()

    prism = llm.prism_client

    if ticker:
        ticker = ticker.upper()

    # Check if Prism is available
    prism_healthy = await prism.check_health()

    # Publish start event to telemetry bus
    try:
        from app.telemetry.bus import publish_event
        from app.telemetry.schema import TelemetryEvent
        from datetime import datetime, timezone

        use_prism = prism_healthy and settings.PRISM_ENABLED
        publish_event(TelemetryEvent(
            ts=datetime.now(timezone.utc).isoformat(),
            cycle_id=cycle_id,
            ticker=ticker,
            kind="llm",
            source="prism" if use_prism else "local_fallback",
            status="ok",
            step="prism_agent_start",
            detail=f"Routing agent {agent_name} to Prism /agent" if use_prism else f"Local fallback execution for agent {agent_name}",
            elapsed_ms=0,
            data={"agent_name": agent_name, "model": llm._resolve_model(agent_name)}
        ))
    except Exception as tel_e:
        logger.debug("[run_prism_agent] Telemetry start failed: %s", tel_e)

    if not prism_healthy or not settings.PRISM_ENABLED:
        logger.info(
            "[PrismHarness] Prism unavailable — falling back to local executor for %s",
            agent_name,
        )
        return await _fallback_to_local(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            ticker=ticker,
            agent_name=agent_name,
            cycle_id=cycle_id,
            bot_id=bot_id,
            priority=priority,
            tools_override=tools_override,
        )

    # Build the tools list — force dynamic tool discovery mode.
    # By passing None for tools, we only provide core built-ins and
    # Prism meta-tools, forcing the agent to use discover_and_enable_tools.
    active_tools = None

    # Inject dynamic tool prompt into system prompt
    from app.agents.dynamic_tool_prompt import DYNAMIC_TOOL_DISCOVERY_PROMPT
    if DYNAMIC_TOOL_DISCOVERY_PROMPT not in system_prompt:
        system_prompt = system_prompt + "\n\n" + DYNAMIC_TOOL_DISCOVERY_PROMPT

    # Inject strict JSON enforcement guardrail for all Prism-routed agents
    JSON_GUARDRAIL = (
        "\n\n### OUTPUT DIRECTIVE\n"
        "You MUST output ONLY ONE valid JSON object. "
        "Do NOT output multiple JSON blocks. Do NOT include conversational preamble or postamble. "
        "Your entire response MUST be parsable by a standard JSON parser."
    )
    if "OUTPUT DIRECTIVE" not in system_prompt:
        system_prompt = system_prompt + JSON_GUARDRAIL

    # Extract tool names from active_tools
    tool_names = []
    built_ins = {
        "execute_python", "search_web", "read_file", "write_file",
        "str_replace_file", "file_info", "file_diff", "browser_action",
        "browser_script", "precise_calculator"
    }

    if active_tools is None:
        # Default to core built-in tools when no specific override is set
        tool_names = list(built_ins)
    else:
        # Prism built-in tools that must NEVER be enabled during automated cycles.
        # ask_user_question blocks the agentic loop for 5 minutes waiting for
        # human input that will never arrive.
        prism_blocked_tools = {
            "ask_user_question",
        }
        mcp_prefix = "mcp__lazy-tool-service__"
        for t in active_tools:
            if isinstance(t, dict):
                name = t.get("name") or t.get("function", {}).get("name")
                if name and name not in prism_blocked_tools:
                    if name not in built_ins and not name.startswith(mcp_prefix):
                        tool_names.append(f"{mcp_prefix}{name}")
                    else:
                        tool_names.append(name)

    # Add Prism-native dynamic tool discovery meta-tools.
    # These are Prism-local tools (NOT MCP-prefixed) that allow agents
    # to discover and enable additional tools mid-loop.
    from app.agents.dynamic_tool_prompt import PRISM_DYNAMIC_META_TOOLS
    for meta_tool in PRISM_DYNAMIC_META_TOOLS:
        if meta_tool not in tool_names:
            tool_names.append(meta_tool)

    # Dynamically register/update the custom agent persona in Prism
    # to preserve custom system prompts and whitelisted tools.
    resolved_agent_id = agent_name
    try:
        logger.info("[PrismHarness] Registering/updating custom agent %s in Prism", agent_name)
        resolved_agent_id = await prism.register_or_update_custom_agent(
            name=agent_name,
            identity=system_prompt,
            enabled_tools=tool_names,
            project=prism.project,
        )
    except Exception as re_err:
        logger.warning(
            "[PrismHarness] Failed to dynamically register custom agent %s in Prism: %s. "
            "Falling back to resolve_agent_id logic.",
            agent_name, re_err
        )


    # Build messages — system prompt is sent separately via Prism's
    # systemPrompt field (in get_chat_payload_and_url), so we do NOT
    # include it in the messages array to avoid triple-injection.
    messages = [
        {"role": "user", "content": user_prompt},
    ]

    # Get the model resolved for this agent
    model = llm._resolve_model(agent_name)

    # CRITICAL: Resolve the correct provider name based on the model's location.
    # DO NOT remove or alter this. It prevents heavy models (like 122B Qwen)
    # from defaulting to the Jetson endpoint, which causes execution failures.
    provider = llm.resolve_provider_for_model(model)

    # ── Dynamic context budget gate ──────────────────────────────────
    # Measure actual input cost (including system_prompt which will be
    # sent via Prism's systemPrompt field) and compute safe max_tokens.
    from app.services.context_gate import compute_safe_max_tokens
    safe_max = compute_safe_max_tokens(
        messages, active_tools,
        system_prompt_extra=system_prompt,
        model_context=llm.get_model_context_window(),
        requested_max=max_tokens,
    )

    # Build Prism payload — agentic_mode=True so Prism runs the loop
    payload, url, headers = prism.get_chat_payload_and_url(
        model=model,
        messages=messages,
        max_tokens=safe_max,
        temperature=temperature,
        system_prompt=system_prompt,
        agent_name=resolved_agent_id,
        ticker=ticker,
        cycle_id=cycle_id,
        enable_thinking=False,
        tools=active_tools,
        agentic_mode=True,
        provider=provider,
        actor_label=actor_label,
    )

    # Add metadata for tracking
    title_parts = [agent_name]
    if ticker:
        title_parts.append(ticker)
    if cycle_id:
        title_parts.append(cycle_id[:12])
    payload["conversationMeta"]["title"] = " · ".join(title_parts)
    payload["autoApprove"] = settings.PRISM_AUTO_APPROVE

    # Clean surrogates from payload to prevent UnicodeEncodeError
    payload = clean_surrogates(payload)

    logger.info(
        "[PrismHarness] Delegating %s to Prism /agent (model=%s, tools=%d, ticker=%s)",
        agent_name,
        model,
        len(active_tools) if active_tools is not None else 0,
        ticker,
    )
    from app.telemetry import send_system_log
    send_system_log("AGENT", f"[{agent_name}] Delegating agentic loop to Prism (model={model})")

    # Execute via Prism
    try:
        client = await llm._get_client()
        response = await asyncio.wait_for(
            client.post(url, json=payload, headers=headers, timeout=float(timeout_seconds)),
            timeout=float(timeout_seconds) + 5,
        )
        response.raise_for_status()

        elapsed_ms = int((time.monotonic() - start) * 1000)
        result_data = response.json()

        # Raise error if response represents an error payload
        if "error" in result_data or result_data.get("error") is True:
            error_msg = result_data.get("message") or result_data.get("error") or "Unknown Prism error"
            raise RuntimeError(f"Prism error: {error_msg}")

        # Prism /agent?stream=false wraps the result inside a "response" dictionary
        response_data = result_data.get("response")
        if isinstance(response_data, dict):
            result_data = response_data

        if "error" in result_data or result_data.get("error") is True:
            error_msg = result_data.get("message") or result_data.get("error") or "Unknown Prism error"
            raise RuntimeError(f"Prism error: {error_msg}")

        # Extract the final assistant response from Prism's response
        final_text = _extract_final_text(result_data)
        token_usage = (
            result_data.get("usage", {}).get("total_tokens", 0)
            or result_data.get("usage", {}).get("totalTokens", 0)
        )
        conversation_id = payload.get("conversationId", "")

        # ── Detect Prism-level finish reason for truncation alerting ──
        prism_finish_reason = ""
        try:
            # Prism wraps the final choice; try to extract finish_reason
            choices = result_data.get("choices") or []
            if choices and isinstance(choices, list):
                prism_finish_reason = choices[0].get("finish_reason", "") or ""
        except Exception:
            pass

        # All base/analytical agents require valid JSON outputs.
        # Fallback to local if the response does not parse into a valid JSON object.
        from app.utils.text_utils import parse_json_response
        parsed = parse_json_response(final_text)
        if not parsed:
            # Attempt fast JSON recovery before falling back to local
            logger.info(
                "[PrismHarness] %s response from Prism is not valid JSON. Attempting fast JSON recovery...",
                agent_name
            )
            # Log raw response on parse failure for post-mortem debugging
            try:
                from app.log_manager import log_manager
                log_manager.log_cycle_error(
                    cycle_id, "prism_json_parse_failure",
                    ticker=ticker, error=f"{agent_name} returned non-JSON from Prism",
                    stage="prism_agent",
                    extra={"raw_llm_response": (final_text[:2000] if final_text else "")},
                )
            except Exception:
                pass

            parsed, recovered_text, rec_tokens, rec_ms = await recover_json_output(
                final_text=final_text,
                agent_name=agent_name,
                ticker=ticker,
                cycle_id=cycle_id,
                bot_id=bot_id,
                priority=priority,
            )
            if parsed:
                final_text = recovered_text
                token_usage += rec_tokens
                elapsed_ms += rec_ms

        if not parsed:
            logger.warning(
                "[PrismHarness] %s returned invalid/empty JSON response from Prism — falling back to local: %s",
                agent_name,
                repr(final_text[:200])
            )
            return await _fallback_to_local(
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                ticker=ticker,
                agent_name=agent_name,
                cycle_id=cycle_id,
                bot_id=bot_id,
                priority=priority,
                tools_override=tools_override,
            )

        # ── Structured turn tracing for Prism-routed agents ──
        try:
            from app.log_manager import log_manager
            prism_tool_calls_raw = result_data.get("toolCalls") or []
            log_manager.log_agent_turn(
                cycle_id, agent_name, 0,
                action_type="reasoning",
                ticker=ticker,
                content_preview=final_text,
                tool_calls=[{"function": {"name": tc.get("name", "?"), "arguments": str(tc.get("input", ""))}} for tc in prism_tool_calls_raw[:10]] if prism_tool_calls_raw else None,
                tokens_used=token_usage,
                elapsed_ms=elapsed_ms,
                finish_reason=prism_finish_reason,
                extra={"routed_via": "prism", "conversation_id": conversation_id},
            )
        except Exception:
            pass  # Turn tracing must never crash the pipeline

        # ── LLM truncation detection ──
        if prism_finish_reason == "length":
            logger.warning(
                "[PrismHarness] LLM output TRUNCATED for %s/%s (finish_reason=length)",
                agent_name, ticker,
            )
            try:
                from app.log_manager import log_manager
                log_manager.log_truncation_warning(
                    cycle_id, agent_name,
                    ticker=ticker,
                    finish_reason=prism_finish_reason,
                    response_preview=final_text,
                )
            except Exception:
                pass

        logger.info(
            "[PrismHarness] %s completed via Prism in %dms (%d tokens)",
            agent_name,
            elapsed_ms,
            token_usage,
        )
        from app.telemetry import send_system_log
        send_system_log("AGENT", f"[{agent_name}] Finished successfully in {elapsed_ms}ms ({token_usage} tokens)")

        # Record tool optimization stats for Prism-routed agents.
        # Prism's JSON response includes toolCalls (names of tools that were called).
        # We extract actual used tool names for accurate optimization tracking.
        _prism_tool_history = []
        try:
            # Extract tool call names from Prism's response
            prism_tool_calls = result_data.get("toolCalls") or []

            # ── Dynamic Tool Discovery Telemetry ──
            # Detect when agents used discover_and_enable_tools, enable_tools,
            # or disable_tools to dynamically modify their toolset mid-loop.
            _dynamic_meta_names = {
                "discover_and_enable_tools", "enable_tools",
                "disable_tools", "search_tools",
            }
            for tc in prism_tool_calls:
                if isinstance(tc, dict):
                    tc_name = tc.get("name", "")
                    if tc_name in _dynamic_meta_names:
                        tc_result = tc.get("result", {})
                        enabled_list = []
                        if isinstance(tc_result, dict):
                            enabled_list = tc_result.get("auto_enabled", []) or tc_result.get("activated", [])
                        logger.info(
                            "[PrismHarness] DYNAMIC TOOL EVENT: %s called %s — enabled: %s",
                            agent_name, tc_name, enabled_list,
                        )
                        try:
                            from app.telemetry.bus import publish_event
                            from app.telemetry.schema import TelemetryEvent
                            from datetime import datetime, timezone
                            publish_event(TelemetryEvent(
                                ts=datetime.now(timezone.utc).isoformat(),
                                cycle_id=cycle_id,
                                ticker=ticker,
                                kind="tool",
                                source="prism_dynamic",
                                status="ok",
                                step="dynamic_tool_enabled",
                                detail=f"Agent {agent_name} dynamically {tc_name}: {enabled_list}",
                                elapsed_ms=int(tc.get("executionMs", 0) or 0),
                                data={
                                    "meta_tool": tc_name,
                                    "tools_affected": enabled_list,
                                    "query": tc.get("args", {}).get("query") if isinstance(tc.get("args"), dict) else None,
                                    "domain": tc.get("args", {}).get("domain") if isinstance(tc.get("args"), dict) else None,
                                }
                            ))
                        except Exception as dyn_tel_e:
                            logger.debug("[PrismHarness] Dynamic tool telemetry failed: %s", dyn_tel_e)

                        # Log dynamic tool events to tool_usage_stats
                        # with service_source = "prism_dynamic" for analytics
                        try:
                            from app.services.logging.tool_logging import log_tool_call
                            log_tool_call(
                                tool_name=tc_name,
                                agent_name=agent_name,
                                ticker=ticker,
                                cycle_id=cycle_id,
                                success=True,
                                execution_ms=int(tc.get("executionMs", 0) or 0),
                                service_source="prism_dynamic",
                            )
                        except Exception as dyn_log_e:
                            logger.debug("[PrismHarness] Dynamic tool DB log failed: %s", dyn_log_e)
            used_tool_names = [
                tc.get("name", "") for tc in prism_tool_calls
                if isinstance(tc, dict) and tc.get("name")
            ]

            # Build tool_history for cross-examiner (matches local executor format)
            _prism_tool_history = []
            for tc in prism_tool_calls:
                if isinstance(tc, dict) and tc.get("name"):
                    tc_name = tc.get("name", "unknown")
                    tc_args = json.dumps(tc.get("arguments", tc.get("args", {})))
                    tc_output = ""
                    tc_result_data = tc.get("result", tc.get("output", ""))
                    if isinstance(tc_result_data, dict):
                        tc_output = json.dumps(tc_result_data)
                    elif isinstance(tc_result_data, str):
                        tc_output = tc_result_data
                    _prism_tool_history.append(
                        f"### Tool Call: {tc_name}({tc_args})\n{tc_output[:5000]}"
                    )

            # Publish tool call events to telemetry bus
            try:
                from app.telemetry.bus import publish_event
                from app.telemetry.schema import TelemetryEvent
                from datetime import datetime, timezone
                for tc in prism_tool_calls:
                    if isinstance(tc, dict) and tc.get("name"):
                        tc_name = tc["name"]
                        tc_success = not bool(tc.get("error"))
                        tc_ms = int(tc.get("executionMs", 0) or tc.get("duration_ms", 0) or 0)
                        from app.telemetry import send_system_log
                        send_system_log(
                            "AGENT",
                            f"[{agent_name}] Executed tool '{tc_name}' ({'success' if tc_success else 'failed'} in {tc_ms}ms)"
                        )
                        
                        publish_event(TelemetryEvent(
                            ts=datetime.now(timezone.utc).isoformat(),
                            cycle_id=cycle_id,
                            ticker=ticker,
                            kind="tool",
                            source="prism",
                            status="ok" if tc_success else "error",
                            step="tool_call",
                            detail=f"Agent {agent_name} executed tool {tc_name} ({'success' if tc_success else 'failed'})",
                            elapsed_ms=tc_ms,
                            data={"tool_name": tc_name, "arguments": tc.get("arguments", {}), "error": tc.get("error")}
                        ))
            except Exception as tel_e:
                logger.debug("[run_prism_agent] Telemetry tool calls failed: %s", tel_e)

            if "toolCalls" in result_data:
                # Log each tool call to tool_usage_stats for reputation tracking
                # Parse per-tool results when Prism provides them
                from app.services.logging.tool_logging import log_tool_call
                tool_results_map = {}
                for tc in prism_tool_calls:
                    if isinstance(tc, dict) and tc.get("name"):
                        tc_name = tc["name"]
                        # Prism may include result/error fields per tool call
                        tc_success = not bool(tc.get("error"))
                        tc_ms = int(tc.get("executionMs", 0) or tc.get("duration_ms", 0) or 0)
                        tool_results_map[tc_name] = (tc_success, tc_ms)

                for tool_name in used_tool_names:
                    tc_success, tc_ms = tool_results_map.get(tool_name, (True, 0))
                    log_tool_call(
                        tool_name=tool_name,
                        agent_name=agent_name,
                        ticker=ticker,
                        cycle_id=cycle_id,
                        success=tc_success,
                        execution_ms=tc_ms,
                        service_source="prism",
                    )
                if used_tool_names:
                    # Count successes vs failures for logging
                    success_count = sum(1 for n in used_tool_names if tool_results_map.get(n, (True, 0))[0])
                    fail_count = len(used_tool_names) - success_count
                    logger.info(
                        "[PrismHarness] Logged %d Prism tool calls for %s (%d ok, %d failed): %s",
                        len(used_tool_names), agent_name, success_count, fail_count, used_tool_names,
                    )

                # Data-driven optimization: only mark actually-used tools as active
                from app.services.tool_optimizer import record_tool_optimization_usage
                asyncio.create_task(
                    record_tool_optimization_usage(
                        agent_name=agent_name,
                        offered_tools=active_tools,
                        used_tool_names=used_tool_names,
                    )
                )
            else:
                # No tool call data available from Prism.
                # This is a DATA LOSS scenario — the agent likely used tools but
                # Prism didn't return toolCalls metadata. Log prominently so
                # operators can detect and investigate.
                logger.info(
                    "[PrismHarness] Prism returned NO toolCalls data for %s/%s (cycle=%s). "
                    "Tool usage stats will be incomplete for this agent run. "
                    "Check Prism logs for conversation %s.",
                    agent_name, ticker, cycle_id, conversation_id,
                )
                # Emit telemetry event so the dashboard can surface this gap
                try:
                    from app.telemetry.bus import publish_event
                    from app.telemetry.schema import TelemetryEvent
                    from datetime import datetime, timezone
                    publish_event(TelemetryEvent(
                        ts=datetime.now(timezone.utc).isoformat(),
                        cycle_id=cycle_id,
                        ticker=ticker,
                        kind="tool",
                        source="prism",
                        status="warning",
                        step="tool_calls_missing",
                        detail=f"Prism returned no toolCalls for {agent_name} — stats gap",
                        elapsed_ms=0,
                        data={"agent_name": agent_name, "conversation_id": conversation_id},
                    ))
                except Exception:
                    pass

                # Fall back to marking all offered tools as active
                # to prevent false pruning in the optimizer
                from app.services.tool_optimizer import mark_tools_as_used_by_prism
                asyncio.create_task(
                    mark_tools_as_used_by_prism(
                        agent_name=agent_name,
                        offered_tools=active_tools,
                    )
                )
        except Exception as rec_err:
            logger.warning("[PrismHarness] Failed to record Prism tool usage: %s", rec_err)

        # Publish success event to telemetry bus
        try:
            from app.telemetry.bus import publish_event
            from app.telemetry.schema import TelemetryEvent
            from datetime import datetime, timezone

            publish_event(TelemetryEvent(
                ts=datetime.now(timezone.utc).isoformat(),
                cycle_id=cycle_id,
                ticker=ticker,
                kind="llm",
                source="prism",
                status="ok",
                step="prism_agent_end",
                detail=f"Agent {agent_name} finished successfully via Prism",
                elapsed_ms=elapsed_ms,
                data={"token_usage": token_usage, "tools_called": used_tool_names}
            ))
        except Exception as tel_e:
            logger.debug("[run_prism_agent] Telemetry success failed: %s", tel_e)

        return PrismAgentResult(
            final_text=final_text,
            token_usage=token_usage,
            execution_ms=elapsed_ms,
            conversation_id=conversation_id,
            routed_via="prism",
            tool_history=_prism_tool_history,
        ).to_dict()


    except asyncio.TimeoutError:
        elapsed_ms = int((time.monotonic() - start) * 1000)
        logger.error(
            "[PrismHarness] %s timed out after %ds — falling back to local",
            agent_name,
            timeout_seconds,
        )
        from app.telemetry import send_system_log
        send_system_log("AGENT", f"[{agent_name}] Execution timed out, falling back to local executor", level="warning")
        # Publish timeout event
        try:
            from app.telemetry.bus import publish_event
            from app.telemetry.schema import TelemetryEvent
            from datetime import datetime, timezone
            publish_event(TelemetryEvent(
                ts=datetime.now(timezone.utc).isoformat(),
                cycle_id=cycle_id,
                ticker=ticker,
                kind="llm",
                source="prism",
                status="error",
                step="prism_agent_end",
                detail=f"Agent {agent_name} timed out after {timeout_seconds}s, falling back to local",
                elapsed_ms=elapsed_ms,
                data={"error": "TimeoutError"}
            ))
        except Exception:
            pass
        return await _fallback_to_local(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            ticker=ticker,
            agent_name=agent_name,
            cycle_id=cycle_id,
            bot_id=bot_id,
            priority=priority,
            tools_override=tools_override,
        )

    except Exception as e:
        logger.exception(
            "[PrismHarness] %s failed via Prism — falling back to local",
            agent_name,
        )
        from app.telemetry import send_system_log
        send_system_log("AGENT", f"[{agent_name}] Execution failed, falling back to local executor: {e}", level="warning")
        # Publish failure event
        try:
            from app.telemetry.bus import publish_event
            from app.telemetry.schema import TelemetryEvent
            from datetime import datetime, timezone
            publish_event(TelemetryEvent(
                ts=datetime.now(timezone.utc).isoformat(),
                cycle_id=cycle_id,
                ticker=ticker,
                kind="llm",
                source="prism",
                status="error",
                step="prism_agent_end",
                detail=f"Agent {agent_name} failed via Prism, falling back to local: {e}",
                elapsed_ms=int((time.monotonic() - start) * 1000),
                data={"error": str(e)}
            ))
        except Exception:
            pass
        return await _fallback_to_local(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            ticker=ticker,
            agent_name=agent_name,
            cycle_id=cycle_id,
            bot_id=bot_id,
            priority=priority,
            tools_override=tools_override,
        )


# ── Helpers ────────────────────────────────────────────────────────────

def _extract_final_text(prism_response: dict) -> str:
    """Extract the final assistant text from Prism's /agent response.

    Prism returns different shapes depending on streaming vs non-streaming.
    This handles both.
    """
    # Unpack nested response if present
    if "response" in prism_response and isinstance(prism_response["response"], dict):
        prism_response = prism_response["response"]

    # Non-streaming: { "choices": [{ "message": { "content": "..." } }] }
    choices = prism_response.get("choices", [])
    if choices:
        message = choices[0].get("message", {})
        content = message.get("content", "")
        if content:
            return content

    # Direct content/text field
    if "text" in prism_response and prism_response["text"]:
        return prism_response["text"]
    if "content" in prism_response and prism_response["content"]:
        return prism_response["content"]

    # Fallback: stringify the whole response
    return json.dumps(prism_response)


async def _fallback_to_local(
    system_prompt: str,
    user_prompt: str,
    ticker: str,
    agent_name: str,
    cycle_id: str,
    bot_id: str,
    priority: Priority,
    tools_override: list[dict] | None,
) -> dict[str, Any]:
    """Fall back to the local executor.py when Prism is unavailable."""
    from app.tools.executor import run_tool_agent
    from app.agents.tool_whitelists import get_agent_budget_turns

    logger.info(
        "[PrismHarness] Using local executor fallback for %s (ticker=%s)",
        agent_name,
        ticker,
    )

    max_loops = get_agent_budget_turns(agent_name, enable_tools=True)

    result = await run_tool_agent(
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        ticker=ticker,
        agent_name=agent_name,
        cycle_id=cycle_id,
        bot_id=bot_id,
        priority=priority,
        tools_override=tools_override,
        bypass_prism=True,
        max_loops=max_loops,
    )

    fallback_text = result.get("final_text", "")
    token_usage = result.get("token_usage", 0)
    execution_ms = result.get("execution_ms", 0)

    from app.utils.text_utils import parse_json_response
    parsed = parse_json_response(fallback_text)
    if not parsed:
        logger.info(
            "[PrismHarness] Local fallback response for %s is not valid JSON. Attempting fast JSON recovery on local output...",
            agent_name
        )
        parsed, recovered_text, rec_tokens, rec_ms = await recover_json_output(
            final_text=fallback_text,
            agent_name=agent_name,
            ticker=ticker,
            cycle_id=cycle_id,
            bot_id=bot_id,
            priority=priority,
        )
        if parsed:
            fallback_text = recovered_text
            token_usage += rec_tokens
            execution_ms += rec_ms

    # Publish local fallback execution end event
    try:
        from app.telemetry.bus import publish_event
        from app.telemetry.schema import TelemetryEvent
        from datetime import datetime, timezone
        publish_event(TelemetryEvent(
            ts=datetime.now(timezone.utc).isoformat(),
            cycle_id=cycle_id,
            ticker=ticker,
            kind="llm",
            source="local_fallback",
            status="ok",
            step="local_agent_end",
            detail=f"Local fallback execution completed for {agent_name}",
            elapsed_ms=execution_ms,
            data={"token_usage": token_usage}
        ))
    except Exception as tel_e:
        logger.debug("[_fallback_to_local] Telemetry fallback end failed: %s", tel_e)

    # Normalize to PrismAgentResult shape
    return PrismAgentResult(
        final_text=fallback_text,
        token_usage=token_usage,
        execution_ms=execution_ms,
        conversation_id="",
        routed_via="local_fallback",
    ).to_dict()
