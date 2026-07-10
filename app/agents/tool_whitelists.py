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
# If an agent_name is NOT in this dict, it gets ALL tools (legacy behavior).

AGENT_TOOL_WHITELISTS: dict[str, list[str]] = {
    # ── V3 Gatekeeper ──
    "v3_portfolio_manager": [
        "get_finnhub_news",
        "lazy_web_search",
        "get_market_data",
        "whiteboard_write"
    ],
    # ── OmniAgent / User Chat ──
    # Curated set for interactive chat — keeps context budget lean
    # while covering all common user needs (market data, research,
    # portfolio, memory, database queries).
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
        "search_internal_database",
        "search_trading_skills",
        "youtube_transcript",
        # Portfolio & trading
        "get_portfolio_state",
        "get_position_pnl",
        "calculate_position_size",
        "calculate_risk_reward",
        "calculate_stop_loss",
        "calculate_portfolio_allocation",
        # Memory
        "write_memory_note",
        "read_memory_note",
        "upsert_memory",
        # Context & database
        "get_cycle_context",
        "run_sql_query",
        "check_hallucination",
        "query_brain_graph",
        "graph_learn",
        # Performance
        "get_performance_metrics",
        # Trading Cycle Control
        "start_trading_cycle",
    ],
    # ── V3 Family Office Worker Agents ──
    "v3_worker_quant": [
        "get_market_data",
        "get_technical_indicators",
        "get_polygon_price_history",
        "get_options_flow",
        "query_technical_indicator",
        "publish_event",
    ],
    "v3_worker_fundamental": [
        "get_market_data",
        "get_finviz_fundamentals",
        "get_sec_filings",
        "get_earnings_data",
        "query_financial_metrics",
        "publish_event",
    ],
    "v3_worker_news": [
        "get_finnhub_news",
        "lazy_web_search",
        "scrape_url",
        "search_internal_database",
        "publish_event",
    ],
    "v3_worker_insider": [
        "get_insider_trades",
        "get_congress_trades",
        "get_sec_filings",
        "publish_event",
    ],
    "ticker_validator": [],
    # ── V3 Pure Agentic Pipeline Agents ──
    "v3_junior_analyst": [
        "get_finnhub_news",
        "lazy_web_search",
        "scrape_url",
        "get_market_data",
        "search_internal_database",
        "post_finding",
        "whiteboard_write",
        "whiteboard_read",
        "whiteboard_summarize",
    ],
    "v3_fundamental_analyst": [
        "get_sec_filings",
        "get_finviz_fundamentals",
        "get_earnings_data",
        "query_financial_metrics",
        "lazy_web_search",
        "scrape_url",
        "get_market_data",
        "post_finding",
        "whiteboard_write",
        "whiteboard_read",
        "whiteboard_summarize",
    ],
    "v3_quant_analyst": [
        "get_market_data",
        "get_technical_indicators",
        "get_polygon_price_history",
        "get_options_flow",
        "query_technical_indicator",
        "calculate_risk_reward",
        "calculate_stop_loss",
        "calculate_position_size",
        "get_portfolio_state",
        "get_position_pnl",
        "post_finding",
        "whiteboard_write",
        "whiteboard_read",
        "whiteboard_summarize",
    ],
    "v3_bull_agent": [
        "whiteboard_read",
        "whiteboard_write",
        "whiteboard_annotate",
    ],
    "v3_bear_agent": [
        "whiteboard_read",
        "whiteboard_write",
        "whiteboard_annotate",
    ],
    "v3_bull_defense": [
        "whiteboard_read",
        "whiteboard_write",
        "whiteboard_annotate",
    ],
    "v3_debate_judge": [
        "whiteboard_read",
        "whiteboard_write",
        "whiteboard_annotate",
    ],
    "v3_regime_engine": [
        "get_market_data",
        "get_finnhub_news",
        "lazy_web_search",
        "scrape_url",
        "get_technical_indicators",
        "whiteboard_read",
        "whiteboard_write",
    ],
    "v3_board_of_directors": [
        "whiteboard_read",
        "whiteboard_write",
        "whiteboard_annotate",
        "whiteboard_summarize",
    ],
}


def get_agent_tools(agent_name: str, domain_blocklist: list[str] | None = None) -> Optional[list[dict]]:
    """Resolve tool schemas for a given agent from the whitelist.

    Args:
        agent_name: The agent's name key in AGENT_TOOL_WHITELISTS.
        domain_blocklist: Optional list of tool domains to exclude from
            the agent's available tools (e.g. ["Health", "Gaming"]).
            Only affects dynamically discovered tools — whitelisted tools
            are always included regardless of domain.

    Returns:
        A filtered list of tool schemas if the agent has a whitelist,
        or None if the agent should receive all tools (legacy behavior).
    """
    if agent_name not in AGENT_TOOL_WHITELISTS:
        return None

    from app.tools.registry import registry

    tool_names = AGENT_TOOL_WHITELISTS[agent_name]
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
        all registry tool names + meta-tools.
    """
    from app.agents.dynamic_tool_prompt import PRISM_DYNAMIC_META_TOOLS

    if agent_name in AGENT_TOOL_WHITELISTS:
        base_names = list(AGENT_TOOL_WHITELISTS[agent_name])
    else:
        # No whitelist — agent gets all registered tools
        from app.tools.registry import registry
        base_names = list(registry.tools.keys())

    # Merge Prism dynamic discovery meta-tools (deduplicated)
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
    "v3_bull_agent": 3,          # No tools — pure reasoning
    "v3_bear_agent": 3,          # No tools — pure reasoning
    "v3_bull_defense": 3,        # No tools — pure reasoning
    "v3_debate_judge": 3,        # No tools — pure reasoning
    "v3_regime_engine": 5,
    "v3_board_of_directors": 5,  # No tools — reasoning from SharedDesk
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
