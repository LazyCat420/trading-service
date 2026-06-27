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
    # Note: Bull, Bear, and Board of Directors are pure reasoning agents
    # with no tools — they don't need Prism registration since they
    # run with enable_tools=False through the standard LLM client.
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
    from lazycat.llm import prism_client as PrismClient

    results: dict[str, bool] = {}

    for module_path in _V3_AGENT_MODULES:
        try:
            import importlib
            module = importlib.import_module(module_path)

            agent_name = module.AGENT_NAME
            agent_id = f"CUSTOM_{agent_name.upper()}"
            system_prompt = module.SYSTEM_PROMPT
            tool_whitelist = module.TOOL_WHITELIST

            # Merge with Prism dynamic meta-tools
            from app.agents.dynamic_tool_prompt import PRISM_DYNAMIC_META_TOOLS
            
            prefixed_whitelist = []
            for t in tool_whitelist:
                if t.startswith("mcp__") or t.startswith("domain:") or t in ("search_web", "discover_and_enable_tools", "enable_tools", "disable_tools", "search_tools"):
                    prefixed_whitelist.append(t)
                else:
                    prefixed_whitelist.append(f"mcp__lazy-tool-service__{t}")
            
            enabled_tools = prefixed_whitelist + list(PRISM_DYNAMIC_META_TOOLS)

            success = await PrismClient.register_or_update_custom_agent(
                name=agent_name,
                identity=system_prompt,
                guidelines=_V3_COMMON_GUIDELINES,
                enabled_tools=enabled_tools,
            )

            results[agent_id] = success
            if success:
                logger.info(
                    "[V3Prism] Registered agent %s with %d tools",
                    agent_id, len(enabled_tools),
                )
            else:
                logger.warning(
                    "[V3Prism] Failed to register agent %s", agent_id,
                )

        except Exception as e:
            logger.error(
                "[V3Prism] Error registering %s: %s", module_path, e,
            )
            results[module_path] = False

    logger.info(
        "[V3Prism] Registration complete: %d/%d agents registered",
        sum(1 for v in results.values() if v),
        len(results),
    )
    return results
