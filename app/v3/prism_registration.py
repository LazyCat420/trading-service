"""
V3 Prism Registration — Registers all V3 agents as Prism Custom Agents.

Called once on startup. Each agent gets:
- A unique agent_id (e.g. CUSTOM_V3_FUNDAMENTAL_ANALYST)
- An identity prompt (the system prompt from the agent module)
- A guidelines string (guardrail rules)
- An enabledTools list (the role-specific tool whitelist)

Uses the existing prism_client.register_or_update_custom_agent() method.
No changes to Rod's repos.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

# Registry of V3 agents to register with Prism
_V3_AGENT_MODULES = [
    "app.v3.agents.junior_analyst",
    "app.v3.agents.fundamental_analyst",
    "app.v3.agents.quant_analyst",
    "app.v3.agents.regime_engine",
    "app.v3.agents.portfolio_manager",
    "app.v3.agents.decision_agent",
    "app.v3.agents.debate_judge",
    "app.v3.agents.bull_agent",
    "app.v3.agents.bear_agent",
    "app.v3.agents.board_of_directors",
]

# Common guidelines appended to all V3 agents
_V3_COMMON_GUIDELINES = """
## V3 Pipeline Rules
1. You are a V3 agent in a linear pipeline. You MUST produce a valid JSON artifact.
2. Do NOT engage in conversation. You are an autonomous data processing script.
3. If a tool fails 3 times, stop calling it and mark the data as a DataGap.
4. Your output will be parsed as JSON. Do NOT wrap it in markdown code blocks.
5. Every claim must cite which tool or data source it came from.
6. Do NOT hallucinate data. If data is missing, say so explicitly.
"""


async def register_v3_agents() -> dict[str, bool]:
    """Register all V3 agents with Prism.

    Returns a dict mapping agent_id → success status.
    Failures are logged but non-fatal.
    """
    from lazycat.llm import PrismClient as PrismClientClass
    from app.config import settings as app_settings

    results: dict[str, bool] = {}

    # Target the primary PRISM_URL (port 5591 proxy)
    urls = {
        app_settings.PRISM_URL
    }
    urls = {u for u in urls if u}

    for module_path in _V3_AGENT_MODULES:
        try:
            import importlib
            module = importlib.import_module(module_path)

            agent_name = module.AGENT_NAME
            agent_id = f"CUSTOM_{agent_name.upper()}"
            system_prompt = getattr(module, "SYSTEM_PROMPT", "You are an autonomous V3 trading agent. Your identity will be provided dynamically at runtime.")
            tool_whitelist = module.TOOL_WHITELIST

            # V3 agents get ONLY their strict role-specific whitelists.
            # No dynamic tool discovery — discover_and_enable_tools caused
            # agents to pull in 766 tools and blow the 262k context limit.
            prefixed_whitelist = []
            for t in tool_whitelist:
                if t.startswith("mcp__") or t.startswith("domain:"):
                    prefixed_whitelist.append(t)
                else:
                    prefixed_whitelist.append(f"mcp__lazy-tool-service__{t}")
            
            enabled_tools = prefixed_whitelist

            agent_success = True
            for target_url in urls:
                try:
                    temp_client = PrismClientClass()
                    temp_client.url = target_url
                    success = await temp_client.register_or_update_custom_agent(
                        name=agent_name,
                        identity=system_prompt,
                        guidelines=_V3_COMMON_GUIDELINES,
                        enabled_tools=enabled_tools,
                    )
                    if not success:
                        agent_success = False
                        logger.warning(
                            "[V3Prism] Failed to register agent %s at %s", agent_id, target_url
                        )
                except Exception as ex:
                    agent_success = False
                    logger.error(
                        "[V3Prism] Exception registering agent %s at %s: %s", agent_id, target_url, ex
                    )

            results[agent_id] = agent_success
            if agent_success:
                logger.info(
                    "[V3Prism] Registered agent %s with %d tools across all targets",
                    agent_id, len(enabled_tools),
                )

        except Exception as e:
            logger.error(
                "[V3Prism] Error registering %s: %s", module_path, e,
            )
            results[module_path] = False

    # Register fallback agent
    fallback_agent_id = "CUSTOM_SYSTEM_JANITOR_AGENT"
    try:
        agent_success = True
        for target_url in urls:
            try:
                temp_client = PrismClientClass()
                temp_client.url = target_url
                success = await temp_client.register_or_update_custom_agent(
                    name="SYSTEM_JANITOR_AGENT",
                    identity="You are a system fallback agent handling triage.",
                    guidelines=_V3_COMMON_GUIDELINES,
                    enabled_tools=["mcp__lazy-tool-service__lazy_web_search"],
                )
                if not success:
                    agent_success = False
                    logger.warning("[V3Prism] Failed to register fallback agent %s at %s", fallback_agent_id, target_url)
            except Exception as ex:
                agent_success = False
                logger.error("[V3Prism] Exception registering fallback agent %s at %s: %s", fallback_agent_id, target_url, ex)
        results[fallback_agent_id] = agent_success
    except Exception as e:
        logger.error("[V3Prism] Error registering fallback agent %s: %s", fallback_agent_id, e)

    logger.info(
        "[V3Prism] Registration complete: %d/%d agents registered",
        sum(1 for v in results.values() if v),
        len(results),
    )
    return results
