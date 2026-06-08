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

logger = logging.getLogger(__name__)


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
            step="PRISM_AGENT_START",
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
                    agentic_mode = True  # Custom agents always use the /agent endpoint
                    
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
                        step="PRISM_AGENT_END",
                        detail=f"{fallback_agent_name} completed via Prism",
                        elapsed_ms=result[2],
                        data={"token_usage": result[1]}
                    ))
                except Exception as tel_e:
                    logger.debug("[call_prism_agent] Telemetry end failed: %s", tel_e)
                    
                _prism_breaker.record_success()
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
                    step="PRISM_AGENT_END",
                    detail=f"{fallback_agent_name} failed via Prism, falling back to local: {e}",
                    elapsed_ms=0,
                    data={"error": str(e)}
                ))
            except Exception as tel_e:
                logger.debug("[call_prism_agent] Telemetry error failed: %s", tel_e)
                
            _prism_breaker.record_failure()
            logger.warning(
                "[PrismAgentCaller] Prism routing failed for %s (%s), falling back to local: %s",
                fallback_agent_name, agent_id, e,
            )

    # ── Fallback: local llm.chat() ──
    logger.debug(
        "[PrismAgentCaller] Local fallback for %s (Prism off or unhealthy)",
        fallback_agent_name,
    )
    response, tokens, elapsed_ms = await llm.chat(
        system=fallback_system_prompt,
        user=user_message,
        temperature=temperature,
        max_tokens=max_tokens,
        priority=priority,
        agent_name=fallback_agent_name,
        ticker=ticker,
        cycle_id=cycle_id,
        bot_id=bot_id,
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
            step="LOCAL_AGENT_END",
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
    if provider and (provider.startswith("vllm") or provider in ("lm-studio", "lm_studio", "llama-cpp", "llama_cpp")):
        if fallback_system_prompt:
            messages.append({"role": "system", "content": fallback_system_prompt})
    messages.append({"role": "user", "content": user_message})

    payload, url, headers = llm.prism_client.get_chat_payload_and_url(
        model=model,
        messages=messages,
        max_tokens=max_tokens,
        temperature=temperature,
        system_prompt=fallback_system_prompt,
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

    r = await llm.prism_client._call_endpoint(client, url, payload, headers)
    data = r.json()

    # Raise error if response represents an error payload
    if "error" in data or data.get("error") is True:
        error_msg = data.get("message") or data.get("error") or "Unknown Prism error"
        raise RuntimeError(f"Prism error: {error_msg}")

    elapsed_ms = int((time.monotonic() - start) * 1000)

    # Prism /agent?stream=false wraps the result inside a "response" dictionary
    response_data = data.get("response")

    # Extract response text
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

    # Extract token count
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
            )
            if tc:
                token_count = int(tc)
                break

    logger.info(
        "[PrismAgentCaller] %s completed via Prism (agent=%s, tokens=%d, %dms)",
        fallback_agent_name, agent_id, token_count, elapsed_ms,
    )

    return text, token_count, elapsed_ms
