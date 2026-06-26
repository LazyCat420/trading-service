import logging
import time
from typing import Any
from lazycat.agent import BaseAgent, AgentHarness
from lazycat.session import ConversationSession
from app.config import settings
from app.services.prism_agent_caller import Priority
from app.tools.registry import registry

logger = logging.getLogger(__name__)

class AgentYielded(Exception):
    def __init__(self, partial_result: dict):
        self.partial_result = partial_result
        super().__init__(f"Agent yielded after {partial_result.get('loops_used', '?')} loops")

async def run_tool_agent(
    system_prompt: str,
    user_prompt: str,
    ticker: str,
    max_loops: int = 9999,
    agent_name: str = "tool_analyst",
    cycle_id: str = "",
    bot_id: str = "",
    priority: Priority = Priority.NORMAL,
    previous_messages: list = None,
    model_override: str | None = None,
    tools_override: list[dict] | None = None,
    yield_on_limit: bool = False,
    bypass_prism: bool = False,
    parent_conversation_id: str | None = None,
    parent_agent_session_id: str | None = None,
) -> dict[str, Any]:
    
    from app.services.prism_agent_caller import llm
    agent = BaseAgent(
        name=agent_name, 
        system_prompt=system_prompt, 
        model=model_override or "gpt-4o",
        llm_client=llm.prism_client,
        project=settings.PROJECT_NAME
    )
    
    active_tools = tools_override if tools_override is not None else registry.schemas
    for t in active_tools:
        agent.add_tool(t)

    session = ConversationSession(session_id=parent_agent_session_id or f"tool_agent_{int(time.time())}")
    
    if previous_messages:
        for m in previous_messages:
            session.add_message(m["role"], m["content"])
            
    harness = AgentHarness(agent=agent, session=session)
    harness.max_iterations = max_loops
    
    t0 = time.time()
    final_content = await harness.run(user_prompt)
    total_time_ms = int((time.time() - t0) * 1000)

    # Note: AgentYielded is not currently used by AgentHarness out-of-the-box
    # because the SDK just returns when max iterations are hit.
    # To mock the old behavior, we just assume it completed cleanly or hit the limit.
    hit_limit = len(session.get_messages()) // 2 >= max_loops

    base_result = {
        "final_text": final_content,
        "token_usage": 0,
        "execution_ms": total_time_ms,
        "chat_history": session.get_messages(),
        "loops_used": len(session.get_messages()) // 2,
        "yielded": hit_limit,
    }

    if hit_limit and yield_on_limit:
        raise AgentYielded(base_result)

    return base_result
