import logging
from enum import IntEnum
from typing import Any, Callable
from lazycat.llm import prism_client

logger = logging.getLogger(__name__)

class Priority(IntEnum):
    HIGH = 0
    NORMAL = 1
    LOW = 2

class VLLMClientShim:
    """Shim to replace the legacy 3700-line vllm_client with the new lazycat-sdk PrismClient."""
    
    def __init__(self):
        self.prism_client = prism_client
        self._killed = False
        
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
        stream_callback: Callable[[str], None] | None = None,
    ) -> tuple[str, int, int]:
        from app.services.prism_agent_caller import call_prism_agent
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
            actor_label=actor_label
        )
        
    async def close(self):
        pass
        
llm = VLLMClientShim()
