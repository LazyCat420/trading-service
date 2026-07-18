"""
Tool Whitelists — Per-agent tool filtering.

Each specialist agent should only see the tools relevant to its role.
This prevents the LLM from being overwhelmed by 66+ tool schemas and
dramatically increases the probability of calling the right tools.

Usage:
    from app.agents.tool_whitelists import get_agent_tools
    schemas = get_agent_tools("risk")  # Returns filtered list of tool schemas
"""

import logging
from typing import Optional

logger = logging.getLogger(__name__)


# ── Agent → Tool Mappings ───────────────────────────────────────────────
# Each key is an agent_name, each value is the list of tool names that
# agent should have access to. Tools not in the whitelist are invisible
# to that agent during its run_agent_loop() execution.
#
# If an agent_name is NOT in this dict (and has no persona-store entry), it
# gets NO tools — get_agent_tools returns [] and logs an error. It used to
# return None, which one caller (debate_coordinator) expanded to the ENTIRE
# tool registry: a typo'd or unregistered agent name silently ran with every
# tool in the system. An agent that needs tools gets whitelisted explicitly.

AGENT_TOOL_WHITELISTS: dict[str, list[str]] = {
    # NOTE: the V3 Prism-registered pipeline agents (v3_portfolio_manager,
    # v3_junior_analyst, v3_regime_engine, the debate chain, the board, …)
    # are NOT listed here — their whitelists live as TOOL_WHITELIST in their
    # own module under app/v3/agents/ (next to the system prompt written for
    # that toolset) and are merged into this dict at import time below.
    # Keeping a second hand-written copy here is what caused the two sources
    # to drift apart.
    # ── OmniAgent / User Chat ──
    # Curated set for interactive chat — keeps context budget lean
    # while covering all common user needs (market data, research,
    # portfolio, memory, database queries).
    # Every entry here must be a REGISTERED tool (func is not None in the
    # registry). The list used to carry ~19 schema-only or entirely phantom
    # names (memory notes, brain graph, cycle control, hallucination check…)
    # — the model kept calling them, got "no local registration function"
    # back, and floundered. If a tool gets an implementation, re-add it here.
    "user_chat": [
        # Core market data
        "get_market_data",
        "get_technical_indicators",
        "get_polygon_price_history",
        "get_finviz_fundamentals",
        "get_options_flow",
        "get_finnhub_news",
        "get_insider_trades",
        "get_earnings_data",
        "get_sec_filings",
        "get_congress_trades",
        # Research
        "lazy_web_search",
        "scrape_url",
        "read_user_notes",
        "get_reddit_trending_stocks",
        # Named tool chains (bundle several tools into one call)
        "run_tool_chain",
        # Watch Desk background watches ("wake me if TSLA hits $300")
        "watch_ticker",
        "list_watches",
        "clear_watch",
        # Parameter governance (human-driven chat can view + adjust)
        "get_parameters",
        "propose_parameter_change",
        # Portfolio & trading
        "get_portfolio_state",
        "get_position_pnl",
        "calculate_position_size",
        "calculate_risk_reward",
        "calculate_stop_loss",
    ],
    # ── V3 Family Office Worker Agents ──
    # publish_event was whitelisted on every worker but never implemented as a
    # tool (app.telemetry.bus.publish_event is a Python function, not a
    # registry tool) — each worker errored on the very call it was told to
    # finish with. Workers signal completion via their artifacts instead.
    "v3_worker_quant": [
        "get_market_data",
        "get_technical_indicators",
        "get_polygon_price_history",
        "get_options_flow",
    ],
    "v3_worker_fundamental": [
        "get_market_data",
        "get_finviz_fundamentals",
        "get_sec_filings",
        "get_earnings_data",
    ],
    "v3_worker_news": [
        "get_finnhub_news",
        "lazy_web_search",
        "scrape_url",
    ],
    "v3_worker_insider": [
        "get_insider_trades",
        "get_congress_trades",
        "get_sec_filings",
    ],
    "ticker_validator": [],
    # ── V3 pipeline agents without a module in app/v3/agents/ ──
    # Bull-defense runs harness-side only; like bull/bear it argues purely
    # from the SharedDesk (see AGENT_BUDGET_OVERRIDES: "No tools").
    "v3_bull_defense": [],
    # ── Tournament Debate Agents ──
    "tournament_pitch": [
        # Core data
        "get_market_data",
        "get_technical_indicators",
        "get_polygon_price_history",
        "get_finviz_fundamentals",
        "get_options_flow",
        "get_finnhub_news",
        "get_sec_filings",
        "get_earnings_data",
        # Research
        "lazy_web_search",
        "scrape_url",
        # Quant tools
        "calculate_risk_reward",
        "calculate_stop_loss",
        "calculate_position_size",
        # Equation Library
        "search_equations",
        "save_equation",
        "run_equation",
        "run_backtest",
    ],
}


def _merge_v3_module_whitelists() -> None:
    """Merge each app/v3/agents module's TOOL_WHITELIST into the dict.

    The modules are the single source of truth for the Prism-registered V3
    pipeline agents (prism_registration reads module.TOOL_WHITELIST directly,
    and each SYSTEM_PROMPT is written against its own toolset). Deriving the
    dict entries here guarantees the harness path resolves the exact same
    tools as the Prism path — the two used to be hand-maintained copies and
    disagreed for 7 of the 9 agents.
    """
    import importlib
    import pkgutil

    import app.v3.agents as v3_agents_pkg

    for mod_info in pkgutil.iter_modules(v3_agents_pkg.__path__):
        try:
            module = importlib.import_module(f"app.v3.agents.{mod_info.name}")
        except Exception as e:
            logger.error(f"[ToolWhitelist] Failed to import v3 agent module '{mod_info.name}': {e}")
            continue
        agent_name = getattr(module, "AGENT_NAME", None)
        whitelist = getattr(module, "TOOL_WHITELIST", None)
        if agent_name and whitelist is not None:
            AGENT_TOOL_WHITELISTS[agent_name] = list(whitelist)


_merge_v3_module_whitelists()


def get_agent_tools(agent_name: str, domain_blocklist: list[str] | None = None) -> Optional[list[dict]]:
    """Resolve tool schemas for a given agent from the whitelist.

    Args:
        agent_name: The agent's name key in AGENT_TOOL_WHITELISTS or the persona store.
        domain_blocklist: Optional list of tool domains to exclude from
            the agent's available tools (e.g. ["Health", "Gaming"]).
            Only affects dynamically discovered tools — whitelisted tools
            are always included regardless of domain.

    Returns:
        A filtered list of tool schemas if the agent has a whitelist, or []
        (with an error log) for an unknown agent — never the full registry.
    """
    from app.tools.registry import registry
    from app.db.agent_persona_store import _load_store

    tool_names = None
    
    # 1. Try to get allowed_tools from the dynamic persona store (Agent Studio UI)
    try:
        store = _load_store()
        for p in store.values():
            if (p.get("role") == agent_name or p.get("name") == agent_name) and p.get("is_active", True):
                if p.get("allowed_tools"):
                    tool_names = p.get("allowed_tools")
                break
    except Exception as e:
        logger.warning(f"[ToolWhitelist] Failed to load dynamic tools for {agent_name}: {e}")

    # 2. Fallback to hardcoded whitelist
    if tool_names is None:
        if agent_name in AGENT_TOOL_WHITELISTS:
            tool_names = AGENT_TOOL_WHITELISTS[agent_name]
        else:
            logger.error(
                f"[ToolWhitelist] Agent '{agent_name}' has no whitelist entry and no "
                f"persona-store tools — running with ZERO tools. Add it to "
                f"AGENT_TOOL_WHITELISTS (or the Agent Studio persona store) if it needs any."
            )
            return []

    schemas = registry.get_schemas_by_names(tool_names)

    # Filter out blocked domains (only for non-whitelisted tools that
    # were dynamically discovered — whitelisted tools pass through)
    if domain_blocklist:
        whitelisted_set = set(tool_names)
        schemas = [
            s for s in schemas
            if s.get("name", s.get("function", {}).get("name", "")) in whitelisted_set
            or s.get("domain", "") not in domain_blocklist
        ]

    # Warn if any whitelisted tools don't exist in the registry
    found_names = {s.get("name", s.get("function", {}).get("name", "")) for s in schemas}
    missing = set(tool_names) - found_names
    if missing:
        logger.warning(
            "[ToolWhitelist] Agent '%s' references %d unregistered tools: %s",
            agent_name,
            len(missing),
            sorted(missing),
        )

    logger.debug(
        "[ToolWhitelist] Agent '%s' → %d/%d tools resolved (blocklist=%d domains)",
        agent_name,
        len(schemas),
        len(tool_names),
        len(domain_blocklist) if domain_blocklist else 0,
    )
    return schemas


def get_agent_enabled_tool_names(agent_name: str) -> list[str]:
    """Return the whitelist tool names for an agent, merged with Prism's
    dynamic tool discovery meta-tools.

    Used when building the ``enabledTools`` list for Prism /agent payloads.
    The meta-tools (``discover_and_enable_tools``, ``enable_tools``, etc.)
    are Prism-local tools that allow agents to dynamically expand their
    toolset mid-loop.

    Returns:
        A list of tool name strings. If the agent has no whitelist, returns
        [] (plus meta-tools for non-v3 agents) — never the full registry.
    """
    from app.db.agent_persona_store import _load_store
    
    base_names = None
    
    try:
        store = _load_store()
        for p in store.values():
            if (p.get("role") == agent_name or p.get("name") == agent_name) and p.get("is_active", True):
                if p.get("allowed_tools"):
                    base_names = list(p.get("allowed_tools"))
                break
    except Exception as e:
        logger.warning(f"[ToolWhitelist] Failed to load dynamic enabledTools for {agent_name}: {e}")

    if base_names is None:
        if agent_name in AGENT_TOOL_WHITELISTS:
            base_names = list(AGENT_TOOL_WHITELISTS[agent_name])
        else:
            # No whitelist → ZERO tools, same contract as get_agent_tools.
            # The registry now spans other apps' tools (html-notes,
            # treesearch), so an all-registry fallback would hand a typo'd
            # agent name every foreign tool in the system.
            logger.error(
                f"[ToolWhitelist] Agent '{agent_name}' has no whitelist entry and no "
                f"persona-store tools — enabledTools will be EMPTY (plus meta-tools "
                f"for non-v3 agents). Add it to AGENT_TOOL_WHITELISTS if it needs any."
            )
            base_names = []

    # V3 agents get ONLY their strict whitelists — no dynamic discovery.
    # discover_and_enable_tools caused agents to pull in 766 tools and
    # blow the 262k context limit.
    if not agent_name.startswith("v3_"):
        from app.agents.dynamic_tool_prompt import PRISM_DYNAMIC_META_TOOLS
        for meta_tool in PRISM_DYNAMIC_META_TOOLS:
            if meta_tool not in base_names:
                base_names.append(meta_tool)

    return base_names


"""
Deterministic budget overrides per agent role.

Data collector agents stay at 3 turns (they just fetch).
Risk/validation agents get 5 turns (need to call calculators AFTER getting data).
Audit agents get 10 turns (need to review multiple performance dimensions).
"""

AGENT_BUDGET_OVERRIDES: dict[str, int] = {
    # User chat — generous budget for interactive sessions
    "user_chat": 15,
    # ── V3 Pure Agentic Pipeline Agents (real limits, not V2's 9999) ──
    "v3_junior_analyst": 5,
    "v3_fundamental_analyst": 7,
    "v3_quant_analyst": 7,
    "v3_bull_agent": 3,          # Small verify toolset (web search + market data)
    "v3_bear_agent": 3,          # Small verify toolset (web search + market data)
    "v3_bull_defense": 3,        # No tools — pure reasoning
    "v3_debate_judge": 3,        # No tools — pure reasoning
    "v3_regime_engine": 5,
    "v3_board_of_directors": 5,  # No tools — reasoning from SharedDesk
    "v3_portfolio_manager": 5,   # Has a TOOL_WHITELIST; without an entry a
                                 # tool-enabled run inherits the 9999 default
    "v3_decision_synthesizer": 5,
}

# Default budget for agents not in the override dict
_DEFAULT_BUDGET = 9999


def get_agent_budget_turns(agent_name: str, enable_tools: bool) -> int:
    """Return the max_turns budget for a given agent.

    Args:
        agent_name: The name of the agent.
        enable_tools: Whether tools are enabled for this agent.

    Returns:
        Number of max turns for the agent's budget.
    """
    if not enable_tools:
        return 1  # No tools = single generation turn
    return AGENT_BUDGET_OVERRIDES.get(agent_name, _DEFAULT_BUDGET)
