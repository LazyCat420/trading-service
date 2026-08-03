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
    "app.v3.agents.valuation_analyst",
    "app.v3.agents.regime_engine",
    "app.v3.agents.portfolio_manager",
    "app.v3.agents.decision_agent",
    "app.v3.agents.debate_judge",
    "app.v3.agents.bull_agent",
    "app.v3.agents.bear_agent",
    "app.v3.agents.board_of_directors",
]

#: Tools a v3 trading agent must never reach, enforced as prism DENY policies.
#:
#: WHY A POLICY AND NOT THE WHITELIST. `enabled_tools` below is not a
#: restriction. Prism's AgentPersonaRegistry.registerCustom() copies
#: `availableTools` out of the Mongo document but NOT `coreToolsLocked`, so
#: AgenticToolResolver's `persona?.coreToolsLocked ?? true` defaults a CUSTOM_*
#: agent to LOCKED and force-adds every CORE_AGENTIC / system tool on top of
#: the list we register — even though the SDK explicitly sends
#: coreToolsLocked:false. Measured in agent_tool_telemetry:
#:
#:   execute_command    1 call  2026-07-18  SUCCEEDED  v3_fundamental_analyst
#:   write_file         2 calls 2026-07-19..20 both SUCCEEDED
#:   query_datastore    1 call  2026-07-18  SUCCEEDED
#:   execute_javascript 6 calls to 2026-07-30 all SUCCEEDED
#:   execute_skill      1 call  2026-07-22  failed
#:
#: The ToolCanary in app/v3/tool_telemetry.py logged every one of these — AFTER
#: they ran. It is telemetry; it blocks nothing. A DENY policy is evaluated by
#: prism's AutoApprovalEngine BEFORE the tier system and BEFORE full-auto, and
#: is documented there as a terminal rejection. Since these agents register
#: with auto_approve=True, it is the only thing that can actually stop a call.
#:
#: DELIBERATELY NOT LISTED — execute_python. It is the most-used of the set (32
#: calls, all successful: reverse-DCF ladders, ATR stops, contradiction
#: analysis) and it does not run in this container: tools-service executes it
#: as a subprocess with socket creation blocked, RLIMIT_DATA capped, and cwd a
#: temp dir that is wiped afterwards. Kept as a deliberate, reviewed exception
#: on 2026-08-03 rather than by omission.
_V3_DENIED_TOOLS = (
    "execute_command",
    "execute_javascript",
    "execute_skill",
    "write_file",
    "query_datastore",
)

#: Serialized PolicyRule shape prism reconstructs in registerCustom():
#: `{tool, decision, name}`. Tool matching is exact, and DENY sorts first.
_V3_TOOL_POLICIES = [
    {"tool": tool, "decision": "DENY", "name": f"deny({tool})"}
    for tool in _V3_DENIED_TOOLS
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

    # One client per target: a fresh PrismClient per agent defeated the SDK's
    # per-instance registration cache and re-opened a connection pool each time.
    clients: dict[str, object] = {}
    for target_url in urls:
        client = PrismClientClass()
        client.url = target_url
        clients[target_url] = client

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
                    # register_or_update_custom_agent returns the agent_id string
                    # (empty/None only on a non-raising failure), not a bool.
                    registered_id = await clients[target_url].register_or_update_custom_agent(
                        name=agent_name,
                        identity=system_prompt,
                        guidelines=_V3_COMMON_GUIDELINES,
                        enabled_tools=enabled_tools,
                        # Non-interactive pipeline: Qwen's <think> block burns
                        # tokens/latency on every call with nobody watching.
                        thinking_default=False,
                        # The actual enforcement — see _V3_DENIED_TOOLS.
                        policies=_V3_TOOL_POLICIES,
                    )
                    if not registered_id:
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

    # Register core custom agents and fallback agents
    core_agents = {
        "CUSTOM_SYSTEM_JANITOR_AGENT": "SYSTEM_JANITOR_AGENT",
        "CUSTOM_TRADING_CYCLE_ANALYSIS_AGENT": "TRADING_CYCLE_ANALYSIS_AGENT",
        "CUSTOM_QUANT_RESEARCH_AGENT": "QUANT_RESEARCH_AGENT",
        "CUSTOM_TECHNICAL_ANALYSIS_AGENT": "TECHNICAL_ANALYSIS_AGENT",
        "CUSTOM_AGENT_ARCHITECT": "AGENT_ARCHITECT",
        "CUSTOM_AGENT_BUDGET_MANAGER": "AGENT_BUDGET_MANAGER",
        "CUSTOM_BULLISH_DEBATER": "BULLISH_DEBATER",
        "CUSTOM_MARKET_ALPHA": "MARKET_ALPHA",
        "CUSTOM_RETRIEVER_AGENT": "RETRIEVER_AGENT",
        "CUSTOM_VERIFIER_AGENT": "VERIFIER_AGENT",
        "CUSTOM_SYNTHESIZER_AGENT": "SYNTHESIZER_AGENT",
        "CUSTOM_PRE_TRADE_AGENT": "PRE_TRADE_AGENT",
        "CUSTOM_META_AUDIT_AGENT": "META_AUDIT_AGENT",
        "CUSTOM_DEBATE_COORDINATOR": "DEBATE_COORDINATOR",
    }

    for agent_id, agent_name in core_agents.items():
        try:
            agent_success = True
            for target_url in urls:
                try:
                    registered_id = await clients[target_url].register_or_update_custom_agent(
                        name=agent_name,
                        identity=f"You are a core custom agent ({agent_name}) handling trading analysis and auxiliary tasks.",
                        guidelines=_V3_COMMON_GUIDELINES,
                        enabled_tools=["mcp__lazy-tool-service__lazy_web_search"],
                        thinking_default=False,
                        # Same denylist: these auxiliary agents run in the same
                        # container and have even less reason to reach a shell.
                        policies=_V3_TOOL_POLICIES,
                    )
                    if not registered_id:
                        agent_success = False
                        logger.warning("[V3Prism] Failed to register core agent %s at %s", agent_id, target_url)
                except Exception as ex:
                    agent_success = False
                    logger.error("[V3Prism] Exception registering core agent %s at %s: %s", agent_id, target_url, ex)
            results[agent_id] = agent_success
        except Exception as e:
            logger.error("[V3Prism] Error registering core agent %s: %s", agent_id, e)

    logger.info(
        "[V3Prism] Registration complete: %d/%d agents registered",
        sum(1 for v in results.values() if v),
        len(results),
    )
    return results
