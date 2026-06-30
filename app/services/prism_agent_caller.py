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

_dynamic_model_cache = {}

def get_live_model_from_vllm(url: str, fallback: str, force_refresh: bool = False) -> str:
    now = time.time()
    if not force_refresh and url in _dynamic_model_cache:
        model_id, timestamp = _dynamic_model_cache[url]
        if now - timestamp < 300: # 5 minutes TTL
            return model_id

    try:
        with httpx.Client(timeout=2.0) as client:
            resp = client.get(f"{url}/v1/models")
            if resp.status_code == 200:
                models = resp.json().get("data", [])
                if models:
                    model_id = models[0].get("id")
                    if model_id:
                        _dynamic_model_cache[url] = (model_id, now)
                        return model_id
    except Exception as e:
        logger.warning(f"Failed to fetch model from {url}: {e}")
    return fallback

def resolve_default_model_for_agent(agent_name: str, force_refresh: bool = False) -> tuple[str, str]:
    """Resolve default model based on agent role to balance load.
    Jetson handles lightweight janitorial, consensus, and curation tasks.
    Gold Spark handles heavy quant research, debates, and final decisions.
    """
    if not agent_name:
        return get_live_model_from_vllm(settings.PROVIDER_VLLM_2_URL, "default-model", force_refresh=force_refresh), "vllm-2"

    name_lower = agent_name.lower()
    
    # Collector & lightweight agents route to Jetson
    collector_keywords = (
        "janitor", "curator", "summarizer", "scout", "purge",
        "maintenance", "consensus", "ticker_validator"
    )
    if any(kw in name_lower for kw in collector_keywords):
        return get_live_model_from_vllm(settings.PROVIDER_VLLM_1_URL, "default-model", force_refresh=force_refresh), "vllm"
        
    return get_live_model_from_vllm(settings.PROVIDER_VLLM_2_URL, "default-model", force_refresh=force_refresh), "vllm-2"


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

    is_validator = "validator" in fallback_agent_name.lower()
    is_thesis = "thesis" in fallback_agent_name.lower()
    
    instruction = ""
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
    except Exception:
        pass
    
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

        default_model, default_provider = resolve_default_model_for_agent(fallback_agent_name or agent_id)
        model = model_override or default_model
        provider = default_provider if not model_override else default_provider
        
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
            )
        except Exception as e:
            if "404" in str(e) or "not exist" in str(e).lower() or "not found" in str(e).lower():
                logger.warning(f"[PrismAgentCaller] 404 Model Not Found. Forcing refresh and retrying...")
                # Fetch fresh model and try exactly one more time
                fresh_model, _ = resolve_default_model_for_agent(fallback_agent_name or agent_id, force_refresh=True)
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
                )
            else:
                raise e
        
        try:
            response_text = resp.json().get("text", "").strip()
        except Exception:
            response_text = resp.text.strip()
        elapsed_ms = int((time.monotonic() - start) * 1000)
        tokens = len(response_text) // 4
        
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
        except Exception:
            pass
            
        return response_text, tokens, elapsed_ms
        
    except Exception as e:
        elapsed_ms = int((time.monotonic() - start) * 1000)
        logger.error(f"[PrismAgentCaller] Call failed: {e}")
        
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
        except Exception:
            pass
            
        raise e

from enum import IntEnum

class Priority(IntEnum):
    HIGH = 0
    NORMAL = 1
    LOW = 2

class PrismLLMShim:
    """Shim class that mimics the old VLLM client interface."""
    def __init__(self):
        self._killed = False
        self.prism_client = prism_client
        self.model = "google/gemma-4-26B-A4B-it"
        
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
        if self._killed:
            raise asyncio.CancelledError("vLLM kill switch is armed — call reset_kill_switch() first")

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
        
    async def stream_prism_agent(self, payload: dict):
        """Pass-through streaming for UI OmniChat."""
        import asyncio
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

    async def close(self):
        pass

llm = PrismLLMShim()
