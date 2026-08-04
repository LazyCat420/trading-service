import json
import logging
import time
from typing import Any
from datetime import datetime, timezone

from lazycat.llm import prism_client
from app.services.prism_agent_registry import resolve_agent_id
from app.telemetry.bus import publish_event
from app.telemetry.schema import TelemetryEvent

from app.config import settings

logger = logging.getLogger(__name__)

FIRM_CONTEXT = (
    "CRITICAL CONTEXT: You are an autonomous data processing script working for a "
    "quantitative trading firm. You are NOT a conversational chatbot. Do NOT talk "
    "to the user, give advice, ask questions, or converse. Your ONLY purpose is to "
    "extract structured financial data to make profitable trading decisions.\n\n"
)

import httpx
import re

_dynamic_model_cache = {}

# ── Reasoning-leak canary (2026-08-03) ──────────────────────────────────────
# Gold Spark swapped to deepseek-v4-flash-0731 on 07-31; prism's thinking-off
# flag uses the Qwen spelling (enable_thinking) which DeepSeek silently
# ignores, so reasoning ran on every call and intermittently leaked into
# content (flash_briefings 126/127 opened with "The user wants me to…").
# The real fix is the vllm-shim in lazy-agent-service; this is the tripwire
# that catches the NEXT silent model swap. Applied at every response site —
# call_prism_agent, chat_with_tools, and base_agent's harness path — because
# a shared helper is only a shared fix if every caller actually calls it.
_REASONING_LEAK_RE = re.compile(
    r"^(?:the user (?:wants|is asking|has asked)|let me\b|i need to\b|okay[,.]"
    r"|i am an?\b|i'm an?\b|the task\b|first,? (?:let|i)\b|we need to\b)",
    re.IGNORECASE,
)
_FIRST_HEADING_RE = re.compile(r"\n#{1,3} ")

# Milder tier: not a reasoning trace, just a conversational acknowledgment
# before the real report ("I'll write the market close briefing based on the
# provided data.\n\n# Market Close Flash Briefing…" — observed live 08-03 with
# thinking correctly OFF). Same salvage rule, quieter log.
_PREAMBLE_RE = re.compile(
    r"^(?:i'll\b|i will\b|sure[,!]|certainly[,!.]|here (?:is|'s)\b)",
    re.IGNORECASE,
)


def strip_reasoning_leak(text: str, agent_name: str = "") -> tuple[str, bool]:
    """Detect a reasoning trace leaked into response content; salvage if safe.

    Returns (text, leaked). When the leak is followed by the real markdown
    report (observed shape: briefing 126), cut to the first heading — but only
    if that keeps ≥30% of the text AND ≥400 chars, because the other observed
    shape (briefing 127) is reasoning all the way down to a trailing Sources
    heading, where cutting would keep 9% and destroy the only content there is.
    Unsalvageable leaks are returned unchanged so the caller ships *something*
    while the canary log points at the real problem.
    """
    stripped = (text or "").lstrip()
    if not stripped:
        return text, False

    if not _REASONING_LEAK_RE.match(stripped):
        # Conversational acknowledgment before the report — trim it with the
        # same retention-guarded heading cut, but don't sound the leak alarm.
        if _PREAMBLE_RE.match(stripped):
            m = _FIRST_HEADING_RE.search(stripped)
            if m:
                remainder = stripped[m.start():].lstrip()
                if len(remainder) >= 400 and len(remainder) >= 0.3 * len(stripped):
                    logger.info(
                        "[PREAMBLE] %s: trimmed conversational preamble "
                        "(%d chars) before first heading",
                        agent_name or "unknown", m.start(),
                    )
                    return remainder, False
        return text, False

    logger.error(
        "[THINK-LEAK] %s: response content starts with a reasoning trace "
        "(%r). The thinking-off flag is not reaching the model — check the "
        "vllm-shim and whether the endpoint's model changed.",
        agent_name or "unknown", stripped[:80],
    )
    m = _FIRST_HEADING_RE.search(stripped)
    if m:
        remainder = stripped[m.start():].lstrip()
        if len(remainder) >= 400 and len(remainder) >= 0.3 * len(stripped):
            logger.warning(
                "[THINK-LEAK] %s: salvaged report from first heading "
                "(kept %d of %d chars)", agent_name or "unknown",
                len(remainder), len(stripped),
            )
            return remainder, True
    return text, True


def _extract_token_usage(resp: Any, response_text: str) -> int:
    """Real token count from the prism/vLLM response envelope.

    The old `len(response_text) // 4` counted OUTPUT characters only, so
    input tokens (which dominate — evidence packets, long prompts) were
    invisible. This made the tournament debate report ~2.5K tokens for an
    8-minute, many-call run. Prefer the provider's `usage`; fall back to the
    char estimate only when usage is absent.
    """
    try:
        payload = resp.json() if hasattr(resp, "json") else None
        if isinstance(payload, dict):
            usage = payload.get("usage") or {}
            if isinstance(usage, dict) and usage:
                # Prism/lazy-agent (TypeScript) emit camelCase — the SDK's
                # streaming path sums inputTokens+outputTokens+reasoningOutputTokens
                # (lazycat/agent.py). Match that first; keep snake_case + a
                # totalTokens field as fallbacks for other providers.
                total = usage.get("totalTokens") or usage.get("total_tokens")
                if isinstance(total, (int, float)) and total > 0:
                    return int(total)
                inp = usage.get("inputTokens") or usage.get("prompt_tokens") or usage.get("input_tokens") or 0
                out = usage.get("outputTokens") or usage.get("completion_tokens") or usage.get("output_tokens") or 0
                reasoning = usage.get("reasoningOutputTokens") or usage.get("reasoning_tokens") or 0
                if inp or out or reasoning:
                    return int(inp) + int(out) + int(reasoning)
    except Exception:
        pass
    # Fallback: rough estimate from output length (better than nothing).
    return len(response_text or "") // 4

async def get_live_model_from_vllm(url: str, force_refresh: bool = False) -> str:
    now = time.time()
    if not force_refresh and url in _dynamic_model_cache:
        model_id, timestamp = _dynamic_model_cache[url]
        if now - timestamp < 300: # 5 minutes TTL
            return model_id

    try:
        async with httpx.AsyncClient(timeout=2.0) as client:
            resp = await client.get(f"{url}/v1/models")
            if resp.status_code == 200:
                models = resp.json().get("data", [])
                if models:
                    model_id = models[0].get("id")
                    if model_id:
                        _dynamic_model_cache[url] = (model_id, now)
                        return model_id
    except Exception as e:
        logger.error("Failed to fetch model from %s: %s", url, e, exc_info=True)
        raise RuntimeError(f"VLLM endpoint offline: {url} (error: {e})")
    
    raise RuntimeError(f"No models found at vLLM endpoint: {url}")

async def resolve_default_model_for_agent(agent_name: str, force_refresh: bool = False) -> tuple[str, str]:
    """Resolve default model based on agent role to balance load.
    Jetson handles lightweight janitorial, consensus, and curation tasks.
    Gold Spark handles heavy quant research, debates, and final decisions.
    """
    from app.services.prism_agent_caller import llm

    provider = "vllm-2"
    endpoint_key = "dgx_spark"

    if agent_name:
        name_lower = agent_name.lower()
        # Collector & lightweight agents route to Jetson
        collector_keywords = (
            "janitor", "curator", "summarizer", "scout", "purge",
            "maintenance", "consensus", "ticker_validator"
        )
        if any(kw in name_lower for kw in collector_keywords):
            provider = "vllm"
            endpoint_key = "jetson"

    ep = llm._endpoints.get(endpoint_key)
    if not ep or not ep.enabled:
        raise RuntimeError(f"VLLM endpoint '{endpoint_key}' is not configured or disabled.")
    
    url = ep.url
    if not url:
        raise RuntimeError(f"VLLM endpoint '{endpoint_key}' has no configured URL.")

    discovered_model = await get_live_model_from_vllm(url, force_refresh=force_refresh)
    return discovered_model, provider


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
    parent_conversation_id: str | None = None,
    parent_agent_session_id: str | None = None,
    model_override: str | None = None,
    project: str | None = None,
) -> tuple[str, int, int]:
    """Route an LLM call through Prism SDK."""
    start = time.monotonic()

    if max_tokens is None:
        max_tokens = 8192

    # Prism's ContextExhaustionGuard rejects ANY request whose output budget
    # is under MINIMUM_VIABLE_OUTPUT_TOKENS (4096) — there are no exemptions,
    # so every call must be floored at 4096. Small budgets are expressed via
    # the conciseness directive instead.
    instruction = ""
    if max_tokens < 4096:
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
        max_tokens = 8192

    agent_id = resolve_agent_id(agent_id or fallback_agent_name)

    try:
        publish_event(TelemetryEvent(
            ts=datetime.now(timezone.utc).isoformat(),
            cycle_id=cycle_id,
            ticker=ticker,
            kind="llm",
            source="prism",
            status="ok",
            step="prism_agent_start",
            detail=f"Starting call to {agent_id}"
        ))
    except Exception as tel_err:
        logger.warning("Failed to publish telemetry start event: %s", tel_err)
    
    try:
        # Prepend system prompt directly to messages list for OpenAI/vLLM compatibility.
        # We interleave a dummy user message between our system prompt and the actual user message.
        # This forces prism-service's vLLM patch to rewrite the injected system context block
        # to a user message, maintaining exactly one leading system message for Qwen.
        messages = [
            {"role": "system", "content": FIRM_CONTEXT + (fallback_system_prompt or "")},
            {"role": "user", "content": "Acknowledged. I am ready to process the quantitative data."},
            {"role": "user", "content": user_message}
        ]
        from app.v3.guardrails import get_budget_for_role
        max_iter = get_budget_for_role(agent_id).max_turns

        default_model, default_provider = await resolve_default_model_for_agent(fallback_agent_name or agent_id)
        model = model_override or default_model
        
        if model_override:
            name_lower = model_override.lower()
            if "gpt-" in name_lower:
                provider = "openai"
            elif "claude-" in name_lower:
                provider = "anthropic"
            elif "gemini-" in name_lower:
                provider = "google"
            else:
                provider = default_provider
        else:
            provider = default_provider
        
        # vLLM will reject requests if (prompt_tokens + max_tokens > max_model_len).
        # We subtract the estimated prompt tokens from max_tokens to give the largest possible budget without crashing.
        all_text = (
            FIRM_CONTEXT + (fallback_system_prompt or "") +
            "Acknowledged. I am ready to process the quantitative data." +
            user_message
        )
        est_input_tokens = len(all_text) // 4 + 100
        if max_tokens >= 4096:
            # Never drop below 4096: Prism's ContextExhaustionGuard rejects
            # smaller output budgets outright (and Prism does its own output
            # clamping against the real context window, so the subtraction is
            # just a vLLM-overflow courtesy, not a correctness requirement).
            max_tokens = max(4096, max_tokens - est_input_tokens)
        
        try:
            resp = await prism_client.call_agent(
                model=model,
                messages=messages,
                system_prompt=FIRM_CONTEXT + (fallback_system_prompt or ""),
                agent_name=agent_id,
                max_tokens=max_tokens,
                temperature=temperature,
                project=project or settings.PROJECT_NAME,
                max_iterations=max_iter,
                provider=provider,
                thinking_enabled=False,
            )
        except Exception as e:
            if "404" in str(e) or "not exist" in str(e).lower() or "not found" in str(e).lower():
                logger.warning(f"[PrismAgentCaller] 404 Model Not Found. Forcing refresh and retrying...")
                # Fetch fresh model and try exactly one more time
                fresh_model, _ = await resolve_default_model_for_agent(fallback_agent_name or agent_id, force_refresh=True)
                resp = await prism_client.call_agent(
                    model=fresh_model,
                    messages=messages,
                    system_prompt=FIRM_CONTEXT + (fallback_system_prompt or ""),
                    agent_name=agent_id,
                    max_tokens=max_tokens,
                    temperature=temperature,
                    project=project or settings.PROJECT_NAME,
                    max_iterations=max_iter,
                    provider=provider,
                    thinking_enabled=False,
                )
            else:
                raise e
        
        try:
            # Prism sets text to null (not missing) on textless turns —
            # .get("text", "") returns None there, and falling back to
            # resp.text would hand the caller the raw JSON envelope as if it
            # were the agent's answer.
            response_text = (resp.json().get("text") or "").strip()
        except Exception as parse_err:
            logger.error("[PrismAgentCaller] %s: response body was not JSON (%s) — returning empty text", agent_id, parse_err)
            response_text = ""
        response_text, leaked = strip_reasoning_leak(response_text, agent_id)
        if not response_text:
            # A reasoning-capable model that burns its whole output budget on
            # reasoning returns content="" with finish=stop — same symptom as
            # a genuinely textless turn, so make it loud instead of silent.
            logger.warning(
                "[THINK-LEAK] %s: empty response text — if the model is a "
                "reasoner, the output budget may have been consumed by "
                "reasoning tokens.", agent_id,
            )
        elapsed_ms = int((time.monotonic() - start) * 1000)
        tokens = _extract_token_usage(resp, response_text)
        
        try:
            publish_event(TelemetryEvent(
                ts=datetime.now(timezone.utc).isoformat(),
                cycle_id=cycle_id,
                ticker=ticker,
                kind="llm",
                source="prism",
                status="ok",
                step="prism_agent_success",
                detail=f"Completed {agent_id} in {elapsed_ms}ms"
            ))
        except Exception as tel_err:
            logger.warning("Failed to publish telemetry success event: %s", tel_err)
            
        return response_text, tokens, elapsed_ms
        
    except Exception as e:
        elapsed_ms = int((time.monotonic() - start) * 1000)
        logger.error("[PrismAgentCaller] Call failed: %s", e, exc_info=True)
        
        try:
            publish_event(TelemetryEvent(
                ts=datetime.now(timezone.utc).isoformat(),
                cycle_id=cycle_id,
                ticker=ticker,
                kind="llm",
                source="prism",
                status="error",
                step="prism_agent_error",
                detail=str(e)
            ))
        except Exception as tel_err:
            logger.warning("Failed to publish telemetry error event: %s", tel_err)
            
        raise e

from enum import IntEnum
from dataclasses import dataclass

class Priority(IntEnum):
    HIGH = 0
    NORMAL = 1
    LOW = 2

@dataclass
class VLLMEndpoint:
    name: str
    url: str
    max_concurrent: int
    enabled: bool = True
    model: str | None = None
    cache_usage: float = 0.0
    requests_running: int = 0
    requests_waiting: int = 0
    last_model_sync: float = 0.0

class PrismLLMShim:
    """Shim class that mimics the old VLLM client interface."""
    def __init__(self):
        self._killed = False
        self.prism_client = prism_client
        self.model = None
        
        self._endpoints: dict[str, VLLMEndpoint] = {}
        
        # Load from config settings
        from app.config import settings
        if settings.PROVIDER_VLLM_1_URL:
            self._endpoints["jetson"] = VLLMEndpoint(
                name="jetson",
                url=settings.PROVIDER_VLLM_1_URL,
                max_concurrent=getattr(settings, "PROVIDER_VLLM_1_CONCURRENCY", 8),
                model=None
            )
        if settings.PROVIDER_VLLM_2_URL:
            self._endpoints["dgx_spark"] = VLLMEndpoint(
                name="dgx_spark",
                url=settings.PROVIDER_VLLM_2_URL,
                max_concurrent=getattr(settings, "PROVIDER_VLLM_2_CONCURRENCY", 16),
                model=None
            )
            
        self._metrics_task = None
        
    async def _sync_endpoint_model(self, ep: VLLMEndpoint, force: bool = False) -> str | None:
        import httpx
        import time
        if not ep or not getattr(ep, "url", None):
            return None
        now_time = time.monotonic()
        last_sync = getattr(ep, "last_model_sync", 0.0)
        if force or now_time - last_sync > 5.0:
            setattr(ep, "last_model_sync", now_time)
            try:
                async with httpx.AsyncClient(timeout=3.0) as client:
                    r = await client.get(f"{ep.url}/v1/models")
                    if r.status_code == 200:
                        data = r.json()
                        models = data.get("data", [])
                        if models:
                            new_model = models[0]["id"]
                            ep.model = new_model
            except Exception as e:
                logger.debug("[PrismLLMShim] Failed to sync model for %s: %s", ep.name, e)
        return getattr(ep, "model", None)

    def reset_kill_switch(self):
        self._killed = False
        
    async def abort_active_requests(self):
        self._killed = True
        
    async def chat(
        self,
        system: str,
        user: str,
        temperature: float = 0.3,
        max_tokens: int = 8192,
        enable_thinking: bool = False,
        priority: Priority = Priority.NORMAL,
        agent_name: str = "unknown",
        ticker: str = "",
        cycle_id: str = "",
        bot_id: str = "",
        model_override: str | None = None,
        endpoint_override: str | None = None,
        history: list[dict] | None = None,
        images: list[str] | None = None,
        tools: list[dict] | None = None,
        actor_label: str | None = None,
        stream_callback: Any = None,
    ) -> tuple[str, int, int]:
        import asyncio
        self.start_metrics_polling()
        if self._killed:
            raise asyncio.CancelledError("vLLM kill switch is armed — call reset_kill_switch() first")

        from app.services.adaptive_concurrency import concurrency_controller

        # Calculate estimated tokens
        est_tokens = (len(system or "") + len(user or "")) // 4
        for msg in (history or []):
            est_tokens += len(msg.get("content", "") or "") // 4

        priority_val = priority.value if hasattr(priority, "value") else int(priority)

        async with concurrency_controller.track(label=agent_name, tokens=est_tokens, priority=priority_val):
            return await call_prism_agent(
                agent_id="",
                user_message=user,
                fallback_system_prompt=system,
                fallback_agent_name=agent_name,
                priority=priority,
                temperature=temperature,
                max_tokens=max_tokens,
                ticker=ticker,
                cycle_id=cycle_id,
                bot_id=bot_id,
                actor_label=actor_label,
                model_override=model_override,
            )

    async def chat_with_tools(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        temperature: float = 0.3,
        max_tokens: int = 8192,
        enable_thinking: bool = False,
        priority: Priority = Priority.NORMAL,
        agent_name: str = "unknown",
        ticker: str = "",
        cycle_id: str = "",
        bot_id: str = "",
        model_override: str | None = None,
        endpoint_override: str | None = None,
        stream_callback: Any = None,
    ) -> dict:
        import asyncio
        import time
        from app.services.adaptive_concurrency import concurrency_controller
        from app.config import settings

        if self._killed:
            raise asyncio.CancelledError("vLLM kill switch is armed — call reset_kill_switch() first")

        self.start_metrics_polling()

        # Estimate tokens of history/messages
        est_tokens = 0
        for msg in messages:
            est_tokens += len(msg.get("content", "") or "") // 4
            if "tool_calls" in msg and msg["tool_calls"]:
                est_tokens += len(str(msg["tool_calls"])) // 4

        start = time.monotonic()
        priority_val = priority.value if hasattr(priority, "value") else int(priority)

        async with concurrency_controller.track(label=agent_name, tokens=est_tokens, priority=priority_val):
            default_model, default_provider = await resolve_default_model_for_agent(agent_name)
            model = model_override or default_model
            
            if model_override:
                name_lower = model_override.lower()
                if "gpt-" in name_lower:
                    provider = "openai"
                elif "claude-" in name_lower:
                    provider = "anthropic"
                elif "gemini-" in name_lower:
                    provider = "google"
                else:
                    provider = default_provider
            else:
                provider = default_provider

            from app.v3.guardrails import get_budget_for_role
            max_iter = get_budget_for_role(agent_name).max_turns

            final_max_tokens = max_tokens or 8192
            if final_max_tokens >= 4096:
                # Floor at 4096: Prism's ContextExhaustionGuard rejects any
                # smaller output budget outright (no provider call is made).
                final_max_tokens = max(4096, final_max_tokens - est_tokens - 100)

            resp = await self.prism_client.call_agent(
                model=model,
                messages=messages,
                system_prompt="",
                agent_name=agent_name,
                tools=tools,
                max_tokens=final_max_tokens,
                temperature=temperature,
                project=settings.PROJECT_NAME,
                max_iterations=max_iter,
                provider=provider,
                thinking_enabled=False,
            )

            try:
                payload_json = resp.json()
                # text is null (not missing) on textless turns — don't let the
                # raw envelope leak through as the response.
                response_text = (payload_json.get("text") or "").strip()
                response_text, _leaked = strip_reasoning_leak(response_text, agent_name)
                # Prism emits camelCase toolCalls with {id, name, args} items;
                # normalize to the OpenAI {function:{name, arguments}} shape
                # the client-side tool loop (registry.execute_tool_call) expects.
                raw_tool_calls = payload_json.get("toolCalls") or payload_json.get("tool_calls") or []
                tool_calls = []
                for tc in raw_tool_calls:
                    if not isinstance(tc, dict):
                        continue
                    if "function" in tc:
                        tool_calls.append(tc)
                    else:
                        tool_calls.append({
                            "id": tc.get("id", ""),
                            "type": "function",
                            "function": {
                                "name": tc.get("name", ""),
                                "arguments": json.dumps(tc.get("args") or {}),
                            },
                        })
            except Exception as parse_err:
                logger.error("[PrismAgentCaller] chat_with_tools: response body was not JSON (%s) — returning empty text", parse_err)
                response_text = ""
                tool_calls = []
                payload_json = {}

            elapsed_ms = int((time.monotonic() - start) * 1000)
            total_tokens = _extract_token_usage(resp, response_text)

            return {
                "text": response_text,
                "total_tokens": total_tokens,
                "elapsed_ms": elapsed_ms,
                "tool_calls": tool_calls,
                # Prism's server-side resolved model, not an echo of the
                # request — the value a per-model scorecard must attribute to.
                "model_used": payload_json.get("model"),
                "provider": payload_json.get("provider"),
            }
        
    async def stream_prism_agent(self, payload: dict):
        """Pass-through streaming for UI OmniChat."""
        import asyncio
        self.start_metrics_polling()
        if self._killed:
            raise asyncio.CancelledError("vLLM kill switch is armed")

        client = await self.prism_client._get_client()
        url = f"{self.prism_client.url}/agent"
        headers = {
            "Content-Type": "application/json",
            "x-project": payload.get("project", "vllm-trading-bot"),
            "x-username": payload.get("username", "omni_chat"),
        }
        try:
            async with client.stream("POST", url, json=payload, headers=headers, timeout=180.0) as response:
                if response.status_code != 200:
                    err = await response.aread()
                    raise RuntimeError(f"Prism HTTP {response.status_code}: {err.decode('utf-8')}")
                async for line in response.aiter_lines():
                    if line:
                        yield line + "\n"
        except Exception as e:
            logger.error("[PRISM] stream_prism_agent error: %s", e)
            yield f"data: {{\"type\": \"error\", \"message\": \"{str(e)}\"}}\n\n"

    def start_metrics_polling(self):
        if self._metrics_task is None or self._metrics_task.done():
            import asyncio
            try:
                loop = asyncio.get_running_loop()
                self._metrics_task = loop.create_task(self._poll_all_metrics())
                logger.info("[PrismLLMShim] Started background metrics polling for vLLM endpoints.")
            except RuntimeError:
                pass

    async def _poll_all_metrics(self):
        import httpx
        import asyncio
        _METRIC_MAP = {
            "vllm:gpu_cache_usage_perc": ("cache_usage", float),
            "vllm:kv_cache_usage_perc": ("cache_usage", float),
            "vllm_gpu_cache_usage_perc": ("cache_usage", float),
            "vllm:num_requests_running": ("requests_running", lambda v: int(float(v))),
            "vllm_num_requests_running": ("requests_running", lambda v: int(float(v))),
            "vllm:num_requests_waiting": ("requests_waiting", lambda v: int(float(v))),
            "vllm_num_requests_waiting": ("requests_waiting", lambda v: int(float(v))),
        }
        while True:
            for ep in self._endpoints.values():
                if not ep.enabled or not ep.url:
                    continue
                try:
                    async with httpx.AsyncClient(timeout=3.0) as client:
                        r = await client.get(f"{ep.url}/metrics")
                        if r.status_code == 200:
                            # Reset values before parsing new ones
                            ep.requests_running = 0
                            ep.requests_waiting = 0
                            for line in r.text.splitlines():
                                if line.startswith("#") or not line.strip():
                                    continue
                                for metric_prefix, (attr, conv) in _METRIC_MAP.items():
                                    if line.startswith(metric_prefix):
                                        parts = line.split()
                                        if len(parts) >= 2:
                                            try:
                                                setattr(ep, attr, conv(parts[-1]))
                                            except Exception:
                                                pass
                                        break
                except Exception as e:
                    logger.debug("[PrismLLMShim] Failed to poll metrics from %s: %s", ep.name, e)
            await asyncio.sleep(5.0)

    async def close(self):
        if self._metrics_task and not self._metrics_task.done():
            self._metrics_task.cancel()

llm = PrismLLMShim()
