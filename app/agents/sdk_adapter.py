"""
SDK Adapter: Bridges trading-service agent invocations to the centralized LazyCat RuntimeClient.

DELETION GATE FOR app/agents/sdk_adapter.py (Phase 5 Decommissioning):
---------------------------------------------------------------------
1. 20 historical cycle replays pass with >= 95% decision concordance and 0 schema violations.
2. 7 consecutive days of canary production traffic with USE_V2_SDK=true and 0 unhandled failures.
3. Zero unexplained tool policy broadening (only TOOL_WHITELIST permitted).
4. Zero missing receipts in MongoDB telemetry.
5. Fallback invocation rate < 0.1% across all junior analyst runs.

UPON MEETING ALL 5 CONDITIONS:
Delete base_agent.py Junior Analyst branch, retire this adapter, and promote direct
RuntimeClient invocation to the primary path.
"""

import logging
from typing import Optional, Dict, Any, List

from lazycat import (
    RuntimeClient,
    CreateRunRequest,
    RunBudget,
    RunProfile,
    RunResult,
    RuntimeClientError,
    RunNotFoundError,
)

logger = logging.getLogger(__name__)

# Adapter metadata for telemetry and canary monitoring
ADAPTER_VERSION = "0.4.0"
OWNING_ISSUE = "migration-trading / CAP-RUN-002"


def create_analyst_profile(agent_name: str) -> RunProfile:
    """
    Application-owned trading role profiles mapped to SDK format.
    
    Preserves role-specific system prompts and tool whitelists in the application
    domain while declaring them as profile constraints for the runtime.
    """
    if agent_name == "v3_junior_analyst":
        from app.v3.agents.junior_analyst import SYSTEM_PROMPT, TOOL_WHITELIST
        return RunProfile(
            name=agent_name,
            system_prompt=SYSTEM_PROMPT,
            tools=list(TOOL_WHITELIST),
            default_budget=RunBudget(max_tool_calls=7, max_tokens=4096),
        )
    raise ValueError(f"Agent profile '{agent_name}' not yet mapped to SDK")


async def run_analyst_via_sdk(
    agent_name: str,
    input_prompt: str,
    max_turns: int = 7,
    client: Optional[RuntimeClient] = None,
    idempotency_key: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Adapter executing an agent turn via the canonical v1 lazycat RuntimeClient.
    
    Maps trading prompts and tool whitelists into CreateRunRequest,
    dispatches to the runtime, and projects RunResult back into the dictionary
    format expected by agent_runner.py.
    """
    profile = create_analyst_profile(agent_name)
    
    # Construct canonical v1 run request
    request = CreateRunRequest(
        profile_id=profile.name,
        input=[
            {"role": "system", "content": profile.system_prompt},
            {"role": "user", "content": input_prompt},
        ],
        budget=RunBudget(max_tool_calls=max_turns, max_tokens=4096),
        tools=[{"name": t} for t in profile.tools],
        stream=False,
        idempotency_key=idempotency_key,
    )
    
    runtime_client = client or RuntimeClient()
    
    try:
        result: RunResult = await runtime_client.create_run(request)
    except RuntimeClientError as rce:
        logger.error(f"RuntimeClient error executing {agent_name}: {rce}")
        return {
            "response": "{}",
            "loops_used": 1,
            "tokens_used": 0,
            "status": "error",
            "error": str(rce),
        }
    except Exception as exc:
        logger.error(f"Unexpected error executing {agent_name} via SDK: {exc}")
        return {
            "response": "{}",
            "loops_used": 1,
            "tokens_used": 0,
            "status": "error",
            "error": str(exc),
        }

    # Extract assistant completion text
    response_text = "{}"
    if result.messages:
        for msg in reversed(result.messages):
            role = msg.get("role") if isinstance(msg, dict) else getattr(msg, "role", None)
            if role == "assistant":
                content = msg.get("content") if isinstance(msg, dict) else getattr(msg, "content", "")
                response_text = content or "{}"
                break

    # Extract usage metrics
    usage = result.usage
    prompt_tokens = usage.prompt_tokens if usage and usage.prompt_tokens is not None else 0
    completion_tokens = usage.completion_tokens if usage and usage.completion_tokens is not None else 0
    total_tokens = usage.total_tokens if usage and usage.total_tokens is not None else (prompt_tokens + completion_tokens)
    tool_calls = usage.tool_calls_count if usage and usage.tool_calls_count is not None else 0
    loops_used = max(tool_calls, 1)

    outcome = {
        "response": response_text,
        "loops_used": loops_used,
        "tokens_used": total_tokens,
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "usage_requests": loops_used,
        "stop_reason": "completed" if result.status == "completed" else result.status,
        "run_id": result.run_id,
        "status": "success" if result.status == "completed" else result.status,
    }

    if result.status == "completed":
        return outcome
    elif result.status in ("timed_out", "timeout", "budget_exceeded"):
        logger.error(f"Agent {agent_name} exceeded budget or timed out (status: {result.status})")
        outcome["status"] = "budget_exceeded"
        return outcome
    elif result.status in ("cancelled", "canceled"):
        logger.warning(f"Agent {agent_name} run cancelled")
        outcome["status"] = "cancelled"
        return outcome
    else:
        err_msg = result.error.message if result.error else f"Run ended with status: {result.status}"
        logger.error(f"Agent {agent_name} failed: {err_msg}")
        outcome["status"] = "error"
        outcome["error"] = err_msg
        return outcome
