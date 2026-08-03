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
        # Smart money (2026-08-03). These three were registered tools granted
        # to NOBODY — 79k trade scores and 1k actor track records recomputed
        # every morning that no LLM could reach. The desk path now gets the
        # actor-quality summary precomputed in alt_data_block (analysts rarely
        # spend a turn on an optional call), but an interactive user asks the
        # open-ended forms — "who's been buying NVDA", "which representatives
        # actually beat SPY" — which no fixed block can anticipate.
        "get_smart_money_signal",
        "get_smart_money_leads",
        "get_smart_money_leaderboard",
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
        # Portfolio-level math (2026-07-21)
        "get_portfolio_covariance",
        "calculate_hrp_allocation",
        "forecast_volatility_garch",
        "get_strategy_health",
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
    # "v3_bull_defense" removed 2026-07-29: no caller ever queued it. The
    # three-turn linear debate lost its third turn when the tournament took
    # over; bull_argument/bear_rebuttal survive as the tournament's fallback,
    # bull_defense does not. Historical artifacts stay readable (see
    # BULL_DEFENSE_SCHEMA) — this only drops the grant for an agent that runs.
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


def _resolve_tool_names(agent_name: str, *, caller: str) -> list[str]:
    """Resolve an agent's tool-name list. THE single source of that answer.

    Extracted 2026-07-29. ``get_agent_tools`` and ``get_agent_enabled_tool_names``
    each carried their own copy of this cascade — persona store first, then the
    static dict, then empty-for-unknown. Two copies of one lookup is the defect
    class that has recurred most often in this codebase (a fix lands on one of
    N carriers), and the module docstring above already records that the
    hand-maintained whitelist copies "disagreed for 7 of the 9 agents".

    The order is load-bearing and unchanged:

    1. The Agent Studio persona store, so a UI edit takes effect without a
       deploy. Only an ACTIVE persona with a non-empty ``allowed_tools`` wins;
       a persona that exists but grants nothing falls through rather than
       silently disarming the agent.
    2. ``AGENT_TOOL_WHITELISTS``. Present-but-empty is a real answer here —
       ``[]`` means "no tools" (v3_bull_defense was such an entry) and must not
       be confused with absent.
    3. Unknown agent -> ``[]`` plus an error log. NEVER the full registry: that
       fallback existed once, and since the registry now spans other apps'
       tools a typo'd agent name handed out every foreign tool in the system.
    """
    from app.db.agent_persona_store import _load_store

    try:
        store = _load_store()
        for p in store.values():
            if (p.get("role") == agent_name or p.get("name") == agent_name) and p.get("is_active", True):
                if p.get("allowed_tools"):
                    return list(p["allowed_tools"])
                break
    except Exception as e:
        logger.warning("[ToolWhitelist] Persona-store lookup failed for %s (%s) — "
                       "falling back to the static whitelist", agent_name, e)

    if agent_name in AGENT_TOOL_WHITELISTS:
        return list(AGENT_TOOL_WHITELISTS[agent_name])

    logger.error(
        "[ToolWhitelist] Agent '%s' has no whitelist entry and no persona-store "
        "tools — resolving to ZERO tools (caller=%s). Add it to "
        "AGENT_TOOL_WHITELISTS (or the Agent Studio persona store) if it needs any.",
        agent_name, caller,
    )
    return []


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

    tool_names = _resolve_tool_names(agent_name, caller="get_agent_tools")
    if not tool_names:
        # Empty resolves to no schemas either way — an unknown agent and an
        # agent whose whitelist is deliberately [] both get zero tools. Short-
        # circuiting here just skips a pointless registry round-trip.
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
    # Same resolution as get_agent_tools, by construction rather than by two
    # copies staying in step — see _resolve_tool_names.
    base_names = _resolve_tool_names(
        agent_name, caller="get_agent_enabled_tool_names")

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
    # 7 (from 5) on 2026-07-24: measured over 56 runs, 96% finished at the
    # 5-6 loop ceiling — the budget WAS the normal path, not an edge case. The
    # documented loop spends every turn on retrieval (news, holdings, search,
    # whiteboard_write), so step 3 "TRACE one lead depth-first" — the step that
    # produces a quantified finding instead of five headlines — was structurally
    # unreachable, and 34% of runs never got their mandatory whiteboard write in.
    # Removing the redundant step-1 whiteboard_read (the board is already in the
    # prompt) frees one turn; this adds the two the trace actually needs.
    "v3_junior_analyst": 7,
    # Raised from 7 on 2026-07-19: every *successful* run was landing on
    # exactly 7 loops, i.e. the ceiling was the normal path rather than an
    # edge case, and runs that hit it often emit a pseudo tool call instead
    # of the artifact (see the salvage pass in v3/agent_runner.py). These two
    # both do multi-source lookups before they can write their report.
    "v3_fundamental_analyst": 12,
    # 14 (from 12) on 2026-07-21: the portfolio-math wave added GARCH +
    # HRP/covariance calls to the quant's documented loop.
    "v3_quant_analyst": 14,
    # 6: the documented loop has ONE mandatory tool call (screener_query for
    # sector comps, which doctrine rule 7 depends on) plus a whiteboard
    # annotate, and everything else it needs is already precomputed into the
    # prompt by app/quant/valuation_block.py. Budget for the gap-fill calls
    # (finviz/earnings/filings) and the artifact turn, not for exploration.
    "v3_valuation_analyst": 6,
    "v3_bull_agent": 3,          # Small verify toolset (web search + market data)
    "v3_bear_agent": 3,          # Small verify toolset (web search + market data)
    # "v3_bull_defense" removed 2026-07-29 with its whitelist entry — no caller.
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
