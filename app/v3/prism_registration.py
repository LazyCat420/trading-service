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

from app.services.mcp_prefix import mcp_tool_name

logger = logging.getLogger(__name__)

def _discover_v3_agent_modules() -> list[str]:
    """Every agent module in `app.v3.agents`, discovered rather than listed.

    This was a hand-maintained list of 11 module paths, and it had drifted:
    `delta_analyst` was missing. It is a real agent — 14 tool calls in
    telemetry — but nothing here registered it, so its Prism persona was never
    refreshed with our identity, our tool whitelist, or (as of 2026-08-03) the
    DENY policies that are the only working restriction on a custom agent.
    Verified live against prism's /custom-agents: 11 of 12 CUSTOM_V3_* personas
    carried `policies`, and CUSTOM_V3_DELTA_ANALYST carried none.

    A list you must remember to append to fails silently and invisibly: the
    missing agent still runs, just unrestricted. Discovery makes adding an
    agent module sufficient. `tests/unit/test_forbidden_tool_policies.py`
    asserts the two can never diverge again.

    The filter is the same one the tests use — a module is an agent when it
    declares both AGENT_NAME and TOOL_WHITELIST. Helpers and shared mixins in
    the package declare neither and are skipped.
    """
    import importlib
    import pkgutil

    import app.v3.agents as pkg

    modules: list[str] = []
    for mod_info in pkgutil.iter_modules(pkg.__path__):
        module_path = f"app.v3.agents.{mod_info.name}"
        try:
            module = importlib.import_module(module_path)
        except Exception as e:  # noqa: BLE001 — one bad module must not stop the rest
            logger.error(
                "[V3Prism] Could not import %s for registration: %s",
                module_path, e,
            )
            continue
        if getattr(module, "AGENT_NAME", None) and getattr(module, "TOOL_WHITELIST", None) is not None:
            modules.append(module_path)
    return sorted(modules)

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

# Common guidelines appended to all V3 agents.
#
# RULES 7 AND 8 ARE THE ONLY LEVER WE HAVE ON THE FORCE-ADDED TOOLS. The DENY
# policies above stop a denied call from executing, but they cannot stop a
# model from TRYING — and each attempt costs a loop. In cycle-v3-1786455000
# agents spent 14 calls (12 execute_javascript, 2 execute_command) discovering
# a rejection the prompt could have told them about; one of them was the
# junior analyst's first ASIC attempt, which then answered with a 51-character
# fallback and failed the loop outright. Since `enabled_tools` cannot hide
# these tools (prism force-adds them — see _V3_DENIED_TOOLS), naming them here
# is the intervention: the model reads this, the resolver does not.
#
# Rule 8 targets the other measured loss in the same cycle: 4 of 20
# emit_structured_output calls were rejected with "'data' is required and must
# be an object" — a 20% failure rate on the tool models reach for by default,
# caused by a wrapper nothing in this prompt ever described.
_V3_COMMON_GUIDELINES = """
## V3 Pipeline Rules
1. You are a V3 agent in a linear pipeline. You MUST produce a valid JSON artifact.
2. Do NOT engage in conversation. You are an autonomous data processing script.
3. If a tool fails 3 times, stop calling it and mark the data as a DataGap.
4. Your output will be parsed as JSON. Do NOT wrap it in markdown code blocks.
5. Every claim must cite which tool or data source it came from.
6. Do NOT hallucinate data. If data is missing, say so explicitly.
7. The platform advertises tools beyond the ones listed for your role. These
   are DENIED by policy and every call is rejected before it runs, wasting a
   turn you cannot get back: execute_command, execute_javascript,
   execute_skill, write_file, query_datastore. Do not call them. For any
   calculation use execute_python, which IS permitted and sandboxed.
8. If you emit your artifact with emit_structured_output, the artifact must be
   wrapped in a top-level "data" object — {"data": {...your artifact...}}.
   Calling it with the artifact's own fields at the top level is rejected with
   "'data' is required and must be an object" and your work is lost.
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

    agent_modules = _discover_v3_agent_modules()
    logger.info(
        "[V3Prism] Discovered %d V3 agent modules to register: %s",
        len(agent_modules),
        ", ".join(m.rsplit(".", 1)[-1] for m in agent_modules),
    )

    for module_path in agent_modules:
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
            # `mcp_tool_name` passes `mcp__*` and `domain:` selectors through
            # untouched and reads the emitted prefix from one place, so the
            # lazy-tool-service -> lazy-agent-service rename lands by config.
            prefixed_whitelist = [mcp_tool_name(t) for t in tool_whitelist]
            
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
                        enabled_tools=[mcp_tool_name("lazy_web_search")],
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
