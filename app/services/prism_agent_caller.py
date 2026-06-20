"""
Prism Agent Caller — Single entry point for routing any LLM call through Prism /agent.

Every pipeline component that previously called llm.chat() directly should now use
call_prism_agent() instead. This function:

1. Checks PRISM_ENABLED + PRISM_AGENT_ROUTING
2. Routes to Prism /agent endpoint with the correct custom agent ID
3. Falls back to local llm.chat() if Prism is off/unhealthy

Pattern inspired by lupos-bot's PrismService.generateAgentResponse().
"""

import logging
import time
from typing import Any

from app.config import settings
from app.services.prism_agent_registry import resolve_agent_id
from app.monitoring.audit_middleware import (
    log_audit_event,
    hash_prompt,
    check_context_overflow,
    check_response_truncation,
)

logger = logging.getLogger(__name__)

# ── Autonomous Identity Anchor ─────────────────────────────────────────
# Ensures every agent routed through call_prism_agent() is framed as an
# autonomous data processor, not a conversational chatbot. This was
# previously only injected in agent_loop.py, leaving Prism-routed agents
# without the critical anti-chatbot preamble.
FIRM_CONTEXT = (
    "CRITICAL CONTEXT: You are an autonomous data processing script working for a "
    "quantitative trading firm. You are NOT a conversational chatbot. Do NOT talk "
    "to the user, give advice, ask questions, or converse. Your ONLY purpose is to "
    "extract structured financial data to make profitable trading decisions.\n\n"
)


# ── Prism Circuit Breaker ──────────────────────────────────────────────
# When Prism fails 3+ times within 5 minutes, stop routing through it
# to prevent cascading overload of local vLLM. Resets automatically
# after the cooldown window passes.
class _PrismCircuitBreaker:
    def __init__(self, failure_threshold: int = 3, window_seconds: float = 300.0):
        self._failure_threshold = failure_threshold
        self._window_seconds = window_seconds
        self._failures: list[float] = []
        self._open_until: float = 0.0

    def record_failure(self):
        now = time.monotonic()
        self._failures.append(now)
        # Prune old failures outside the window
        self._failures = [t for t in self._failures if now - t < self._window_seconds]
        if len(self._failures) >= self._failure_threshold:
            self._open_until = now + self._window_seconds
            logger.warning(
                "[PrismCircuitBreaker] OPEN — %d failures in %.0fs window, "
                "bypassing Prism for %.0fs",
                len(self._failures), self._window_seconds, self._window_seconds,
            )
            self._failures.clear()  # Reset count for next window

    def record_success(self):
        """A successful Prism call resets the failure counter."""
        self._failures.clear()
        self._open_until = 0.0

    @property
    def is_open(self) -> bool:
        if self._open_until and time.monotonic() < self._open_until:
            return True
        if self._open_until and time.monotonic() >= self._open_until:
            # Auto-reset after cooldown
            self._open_until = 0.0
            logger.info("[PrismCircuitBreaker] CLOSED — cooldown expired, retrying Prism")
        return False


_prism_breaker = _PrismCircuitBreaker()


async def call_prism_agent(
    agent_id: str,
    user_message: str,
    fallback_system_prompt: str,
    fallback_agent_name: str,
    priority: Any = None,
    temperature: float = 0.3,
    max_tokens: int = 1024,
    ticker: str = "",
    cycle_id: str = "",
    bot_id: str = "",
    agentic_mode: bool = False,
    actor_label: str | None = None,
) -> tuple[str, int, int]:
    """Route an LLM call through Prism /agent or fall back to local llm.chat().

    Args:
        agent_id: Prism custom agent ID (e.g. "CUSTOM_DATA_JANITOR_AGENT").
                  If empty, resolved via prism_agent_registry from fallback_agent_name.
        user_message: The user/content message to send to the agent.
        fallback_system_prompt: System prompt used for local fallback when Prism is off.
        fallback_agent_name: The agent_name string for local llm.chat() fallback.
        priority: Queue priority for local fallback.
        temperature: LLM temperature.
        max_tokens: Max tokens for generation.
        ticker: Ticker symbol for context/logging.
        cycle_id: Cycle ID for context/logging.
        bot_id: Bot ID for context/logging.
        actor_label: Optional actor label override for username.

    Returns:
        Tuple of (response_text, token_count, elapsed_ms).
    """
    from app.services.vllm_client import llm, Priority

    if priority is None:
        priority = Priority.NORMAL

    start = time.monotonic()


    if max_tokens is None:
        max_tokens = 8192

    # ── Dynamic Token Constraint Conversion ──
    # Prevent aggressive mid-word truncation by converting tight max_tokens
    # bounds into explicit sentence limits in the system prompt.
    # We skip this override for strict JSON validators that explicitly want tiny token constraints.
    is_validator = "validator" in fallback_agent_name.lower()
    is_thesis = "thesis" in fallback_agent_name.lower()
    
    if max_tokens < 4096 and not is_validator and not is_thesis:
        if max_tokens <= 128:
            sentences = "1 or 2 sentences max"
        elif max_tokens <= 256:
            sentences = "under 4 sentences"
        elif max_tokens <= 512:
            sentences = "under 8 sentences"
        elif max_tokens <= 1024:
            sentences = "under 15 sentences"
        else:
            sentences = "concise"
            
        instruction = f"\n\n[SYSTEM DIRECTIVE: Keep your response concise, {sentences}.]"
        fallback_system_prompt = (fallback_system_prompt or "") + instruction
        
        # Override the hardware ceiling to prevent abrupt cut-offs
        max_tokens = 8192

    # Always resolve the agent ID via registry mapping to ensure it maps to one of the 8 valid Prism custom agent IDs
    agent_id = resolve_agent_id(agent_id or fallback_agent_name)

    # Publish start event to telemetry bus
    try:
        from app.telemetry.bus import publish_event
        from app.telemetry.schema import TelemetryEvent
        from datetime import datetime, timezone

        use_prism = settings.PRISM_ENABLED and settings.PRISM_AGENT_ROUTING and not _prism_breaker.is_open
        publish_event(TelemetryEvent(
            ts=datetime.now(timezone.utc).isoformat(),
            cycle_id=cycle_id,
            ticker=ticker,
            kind="llm",
            source="prism" if use_prism else "local_fallback",
            status="ok",
            step="prism_agent_start",
            detail=f"Routing {fallback_agent_name} to Prism" if use_prism else f"Local fallback for {fallback_agent_name}",
            elapsed_ms=0,
            data={"agent_id": agent_id, "agent_name": fallback_agent_name}
        ))
    except Exception as tel_e:
        logger.debug("[call_prism_agent] Telemetry start failed: %s", tel_e)

    # ── Try Prism /agent routing ──
    if settings.PRISM_ENABLED and settings.PRISM_AGENT_ROUTING and not _prism_breaker.is_open:
        try:
            prism_healthy = await llm.prism_client.check_health()
            if prism_healthy:
                # ── Dynamic Custom Agent (Lego Pieces) Check ──
                from app.agents.custom import get_custom_agent
                
                custom_def = get_custom_agent(fallback_agent_name)
                dynamic_tools = None
                if custom_def:
                    logger.info("[PrismAgentCaller] Loaded custom Lego agent definition for '%s'", fallback_agent_name)
                    fallback_system_prompt = custom_def["identity"]
                    dynamic_tools = custom_def["enabled_tools"]
                    # Always route custom registered agents to Prism's /agent endpoint.
                    # The /agent endpoint reads the systemPrompt field; /chat ignores it.
                    # Previously, agents with [] tools (e.g. ticker_validator) were routed
                    # to /chat via bool([]) == False, losing their system prompt entirely.
                    agentic_mode = True
                    
                    # Dynamically register the agent in Prism to lock in its custom system prompt and tools
                    try:
                        agent_id = await llm.prism_client.register_or_update_custom_agent(
                            name=fallback_agent_name,
                            identity=fallback_system_prompt,
                            enabled_tools=dynamic_tools,
                            project=llm.prism_client.project,
                        )
                    except Exception as reg_err:
                        logger.warning(
                            "[PrismAgentCaller] Failed to dynamically register custom agent %s: %s. Using default agent_id.",
                            fallback_agent_name, reg_err
                        )

                result = await _call_via_prism(
                    agent_id=agent_id,
                    user_message=user_message,
                    fallback_system_prompt=fallback_system_prompt,
                    fallback_agent_name=fallback_agent_name,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    ticker=ticker,
                    cycle_id=cycle_id,
                    agentic_mode=agentic_mode,
                    dynamic_tools=dynamic_tools,
                    actor_label=actor_label,
                )
                
                # Publish end event
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
                        detail=f"{fallback_agent_name} completed via Prism",
                        elapsed_ms=result[2],
                        data={"token_usage": result[1]}
                    ))
                except Exception as tel_e:
                    logger.debug("[call_prism_agent] Telemetry end failed: %s", tel_e)
                    
                _prism_breaker.record_success()

                # ── Audit: successful Prism call ──
                is_truncated = check_response_truncation(result[0])
                check_context_overflow(
                    token_count=result[1],
                    agent_name=fallback_agent_name,
                    endpoint="/agent",
                )
                log_audit_event(
                    endpoint="/agent",
                    agent_name=fallback_agent_name,
                    model_used=agent_id,
                    system_prompt_hash=hash_prompt(fallback_system_prompt),
                    inference_ms=result[2],
                    tokens_total=result[1],
                    is_truncated=is_truncated,
                    fallback_triggered=False,
                    circuit_breaker_open=False,
                    ticker=ticker,
                    cycle_id=cycle_id,
                    status="ok",
                    detail=f"{fallback_agent_name} completed via Prism in {result[2]}ms",
                )

                return result
        except Exception as e:
            # Publish error/fallback event
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
                    detail=f"{fallback_agent_name} failed via Prism, falling back to local: {e}",
                    elapsed_ms=0,
                    data={"error": str(e)}
                ))
            except Exception as tel_e:
                logger.debug("[call_prism_agent] Telemetry error failed: %s", tel_e)

            _prism_breaker.record_failure()

            # ── Audit: Prism fallback triggered ──
            log_audit_event(
                endpoint="/agent",
                agent_name=fallback_agent_name,
                model_used=agent_id,
                system_prompt_hash=hash_prompt(fallback_system_prompt),
                fallback_triggered=True,
                circuit_breaker_open=_prism_breaker.is_open,
                ticker=ticker,
                cycle_id=cycle_id,
                status="fallback",
                detail=f"Prism failed for {fallback_agent_name}: {type(e).__name__}: {str(e)[:200]}",
            )

            logger.warning(
                "[PrismAgentCaller] Prism routing failed for %s (%s), falling back to local: %s",
                fallback_agent_name, agent_id, e,
            )

    # ── Fallback: local llm.chat() ──
    logger.debug(
        "[PrismAgentCaller] Local fallback for %s (Prism off or unhealthy)",
        fallback_agent_name,
    )
    # Prepend autonomous identity anchor so local fallback agents
    # are framed as data processors, not conversational chatbots.
    full_system_prompt = FIRM_CONTEXT + (fallback_system_prompt or "")
    logger.info(
        "[PrismAgentCaller] Local fallback assembled prompt for %s (ticker=%s): SYSTEM=%r | USER=%r",
        fallback_agent_name, ticker or "global", full_system_prompt, user_message
    )
    response, tokens, elapsed_ms = await llm.chat(
        system=full_system_prompt,
        user=user_message,
        temperature=temperature,
        max_tokens=max_tokens,
        priority=priority,
        agent_name=fallback_agent_name,
        ticker=ticker,
        cycle_id=cycle_id,
        bot_id=bot_id,
    )
    
    # ── Audit: local fallback call completed ──
    is_truncated = check_response_truncation(response)
    log_audit_event(
        endpoint="/agent/local_fallback",
        agent_name=fallback_agent_name,
        model_used=agent_id,
        system_prompt_hash=hash_prompt(fallback_system_prompt),
        inference_ms=elapsed_ms,
        tokens_total=tokens,
        is_truncated=is_truncated,
        fallback_triggered=True,
        ticker=ticker,
        cycle_id=cycle_id,
        status="ok",
        detail=f"{fallback_agent_name} completed via local fallback in {elapsed_ms}ms",
    )

    # Publish fallback end event
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
            detail=f"{fallback_agent_name} completed via local fallback",
            elapsed_ms=elapsed_ms,
            data={"token_usage": tokens}
        ))
    except Exception as tel_e:
        logger.debug("[call_prism_agent] Telemetry local fallback end failed: %s", tel_e)
        
    return response, tokens, elapsed_ms


async def _call_via_prism(
    agent_id: str,
    user_message: str,
    fallback_system_prompt: str,
    fallback_agent_name: str,
    temperature: float,
    max_tokens: int,
    ticker: str,
    cycle_id: str,
    agentic_mode: bool = False,
    dynamic_tools: list[str] | None = None,
    actor_label: str | None = None,
) -> tuple[str, int, int]:
    """Execute the actual Prism /agent call.

    Sends the user message to Prism's /agent endpoint with the specified
    custom agent ID. Prism handles system prompt assembly, tool policies,
    and agentic loop execution server-side.
    """
    from app.services.vllm_client import llm

    start = time.monotonic()
    client = await llm.prism_client._get_client()

    model = llm._resolve_model(fallback_agent_name)

    # CRITICAL: Resolve the correct provider name based on the model's location.
    # DO NOT remove or alter this. It prevents heavy models (like 122B Qwen)
    # from defaulting to the Jetson endpoint, which causes execution failures.
    provider = llm.resolve_provider_for_model(model)

    messages = []
    # Prepend autonomous identity anchor for Prism-routed agents
    anchored_system_prompt = FIRM_CONTEXT + (fallback_system_prompt or "")
    if provider and (provider.startswith("vllm") or provider in ("lm-studio", "lm_studio", "llama-cpp", "llama_cpp")):
        if anchored_system_prompt:
            messages.append({"role": "system", "content": anchored_system_prompt})
    messages.append({"role": "user", "content": user_message})
    
    logger.info(
        "[PrismAgentCaller] Assembled prompt messages for %s (ticker=%s): systemPrompt=%r | userMessage=%r",
        fallback_agent_name, ticker or "global", anchored_system_prompt, user_message
    )

    payload, url, headers = llm.prism_client.get_chat_payload_and_url(
        model=model,
        messages=messages,
        max_tokens=max_tokens,
        temperature=temperature,
        system_prompt=anchored_system_prompt,
        agent_name=agent_id,
        ticker=ticker,
        cycle_id=cycle_id,
        enable_thinking=False,
        tools=dynamic_tools,
        agentic_mode=agentic_mode,
        provider=provider,
        actor_label=actor_label,
    )
    payload["autoApprove"] = settings.PRISM_AUTO_APPROVE
    payload["skipConversation"] = False

    logger.info(
        "[PrismAgentCaller] Routing %s → Prism /agent (agent=%s, ticker=%s, tools=%d)",
        fallback_agent_name, agent_id, ticker or "N/A", len(payload.get("enabledTools", []))
    )

    # We no longer stream because it fragments Prism's conversation logs.
    # Route to non-streaming direct endpoint call.
    r = await llm.prism_client._call_endpoint(client, url, payload, headers)
    data = r.json()
    if "error" in data or data.get("error") is True:
        error_msg = data.get("message") or data.get("error") or "Unknown Prism error"
        raise RuntimeError(f"Prism error: {error_msg}")

    elapsed_ms = int((time.monotonic() - start) * 1000)
    response_data = data.get("response")
    text = ""
    for d in (response_data, data):
        if isinstance(d, dict):
            choices = d.get("choices", [])
            if choices:
                message = choices[0].get("message", {})
                text = message.get("content", "")
                if text:
                    break
            text = d.get("text") or d.get("content") or ""
            if text:
                break
            messages = d.get("messages", [])
            if messages:
                last = messages[-1]
                text = last.get("content", "") if isinstance(last, dict) else str(last)
                if text:
                    break

    if text and ("⚠️ The model's response was cut short" in text or "response was cut short" in text):
        raise RuntimeError(f"Prism response was cut short warning detected: {text[:100]}...")

    token_count = 0
    for d in (response_data, data):
        if isinstance(d, dict):
            usage = d.get("usage") or {}
            tc = (
                d.get("totalTokens", 0)
                or d.get("total_tokens", 0)
                or usage.get("total_tokens", 0)
                or usage.get("totalTokens", 0)
                or (usage.get("inputTokens", 0) + usage.get("outputTokens", 0))
                or 0
            )
            if tc:
                token_count = int(tc)
                break

    logger.info(
        "[PrismAgentCaller] %s completed via Prism JSON (agent=%s, tokens=%d, %dms)",
        fallback_agent_name, agent_id, token_count, elapsed_ms,
    )
    return text, token_count, elapsed_ms
