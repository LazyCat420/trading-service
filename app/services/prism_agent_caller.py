import logging
import time
from typing import Any
from datetime import datetime, timezone

from lazycat.llm import prism_client
from app.services.prism_agent_registry import resolve_agent_id
from app.telemetry.bus import publish_event
from app.telemetry.schema import TelemetryEvent

logger = logging.getLogger(__name__)

FIRM_CONTEXT = (
    "CRITICAL CONTEXT: You are an autonomous data processing script working for a "
    "quantitative trading firm. You are NOT a conversational chatbot. Do NOT talk "
    "to the user, give advice, ask questions, or converse. Your ONLY purpose is to "
    "extract structured financial data to make profitable trading decisions.\n\n"
)

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
        messages = [{"role": "user", "content": user_message}]
        resp = await prism_client.call_agent(
            model="gpt-4o",
            messages=messages,
            system_prompt=FIRM_CONTEXT + (fallback_system_prompt or ""),
            agent_name=agent_id,
            max_tokens=max_tokens,
            temperature=temperature
        )
        
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
