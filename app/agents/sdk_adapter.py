import logging
from typing import Optional, Dict, Any, List

logger = logging.getLogger(__name__)

# Try to import the new Dev 2 SDK; fallback to a stub if it's not yet installed during rollout
try:
    from lazycat_sdk import Client, RunRequest, RunProfile
except ImportError:
    class RunProfile:
        def __init__(self, name: str, system_prompt: str, tools: List[str]):
            self.name = name
            self.system_prompt = system_prompt
            self.tools = tools

    class RunRequest:
        def __init__(self, profile: RunProfile, input_prompt: str, budget: int = 7):
            self.profile = profile
            self.input_prompt = input_prompt
            self.budget = budget

    class Client:
        def __init__(self, endpoint: str = ""):
            pass

        async def run_agent(self, request: RunRequest) -> Dict[str, Any]:
            logger.warning("Using lazycat_sdk STUB - returning empty result")
            return {"status": "error", "error": "SDK not installed"}

def create_analyst_profile(agent_name: str) -> 'RunProfile':
    """
    Application-owned trading role profiles mapped to SDK format.
    """
    if agent_name == "v3_junior_analyst":
        from app.v3.agents.junior_analyst import SYSTEM_PROMPT, TOOL_WHITELIST
        return RunProfile(
            name=agent_name,
            system_prompt=SYSTEM_PROMPT,
            tools=TOOL_WHITELIST
        )
    raise ValueError(f"Agent profile {agent_name} not yet mapped to SDK")

async def run_analyst_via_sdk(agent_name: str, input_prompt: str, max_turns: int = 7) -> Dict[str, Any]:
    """
    Adapter from existing agent invocation to Dev 2's SDK.
    """
    client = Client()
    profile = create_analyst_profile(agent_name)
    request = RunRequest(
        profile=profile,
        input_prompt=input_prompt,
        budget=max_turns
    )
    
    # Run the agent via the SDK (abstracting away SSE parsing, retries, usage parsing)
    # The SDK returns the final outcome which we map into trading-service constructs
    result = await client.run_agent(request)
    
    # Map runtime outcomes to pipeline outcomes
    if result.get("status") == "success":
        return result.get("data", {})
    elif result.get("status") == "timeout" or result.get("status") == "budget_exceeded":
        logger.error(f"Agent {agent_name} budget exceeded or timed out.")
        return {}
    else:
        logger.error(f"Agent {agent_name} failed: {result.get('error')}")
        return {}
