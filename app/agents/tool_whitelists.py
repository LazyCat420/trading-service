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
    # ── V1 Specialist Agents ──
    "sentiment": [
        "get_market_data",
        "get_finnhub_news",
        
        "scrape_url",
        "search_internal_database",
        "search_trading_skills",
        "post_finding",
        "read_team_findings",
        "write_memory_note",
        "read_memory_note",
        "request_data_collection",
        "search_database_facts",
    ],
    "technical": [
        "get_market_data",
        "get_technical_indicators",
        "get_polygon_price_history",
        "get_options_flow",
        "search_trading_skills",
        "post_finding",
        "read_team_findings",
        "query_technical_indicator",

    ],
    "fundamental": [
        "get_market_data",
        "get_finviz_fundamentals",
        "get_sec_filings",
        "get_earnings_data",
        "search_trading_skills",
        "post_finding",
        "read_team_findings",
        "request_data_collection",
        "query_financial_metrics",
        "search_database_facts",
    ],
    "risk": [
        "get_market_data",
        "get_technical_indicators",
        "calculate_stop_loss",
        "calculate_position_size",
        "calculate_risk_reward",
        "calculate_portfolio_allocation",
        "get_portfolio_state",
        "get_position_pnl",
        "get_options_flow",
        # Coordination — risk MUST be able to communicate with other agents
        "post_finding",
        "read_team_findings",
        "request_investigation",
        "check_open_investigations",
        # Memory — risk needs to track historical risk assessments
        "write_memory_note",
        "read_memory_note",
    ],
    "fund_flow": [
        "get_sec_filings",
        "get_congress_trades",
        "get_insider_trades",
        "search_internal_database",
        "post_finding",
        "read_team_findings",
        "search_database_facts",
    ],
    "comparative": [
        "get_market_data",
        "get_technical_indicators",
        "get_polygon_price_history",
    ],
    # ── V2 Role-Based Agents ──
    "retriever": [
        "get_market_data",
        "get_finnhub_news",
        "get_technical_indicators",
        "get_finviz_fundamentals",
        "get_polygon_price_history",
        
        "scrape_url",
        "get_sec_filings",
        "get_options_flow",
        "get_insider_trades",
        "get_earnings_data",
        "get_congress_trades",
        "search_internal_database",
        "search_trading_skills",
        "request_data_collection",
    ],
    "verifier": [
        "check_hallucination",
        
        "get_market_data",
        "get_cycle_context",
    ],
    "synthesizer": [
        "get_cycle_context",
        "get_cycle_context_all",
        "execute_momentum_strategy",
        "execute_value_strategy",
        "execute_python",
    ],
    # ── Pre-Trade Execution Agent ──
    "pre_trade": [
        "calculate_portfolio_allocation",
        "calculate_stop_loss",
        "calculate_position_size",
        "calculate_risk_reward",
        "set_price_trigger",
        "get_portfolio_state",
        "get_market_data",
        "get_technical_indicators",
        "buy_stock",
        "sell_stock",
        "get_cycle_context",
        "get_cycle_context_all",
        # Coordination — pre_trade needs to see team findings before executing
        "post_finding",
        "read_team_findings",
        "request_investigation",
        "check_open_investigations",
        # Memory — track execution decisions
        "write_memory_note",
        "read_memory_note",
    ],
    # ── Meta Audit Agent ──
    "meta_audit": [
        "get_performance_metrics",
        "audit_decision_quality",
        "read_profile",
        "get_portfolio_state",
        "propose_constitution_amendment",
        "write_memory_note",
        "list_active_schedules",
        "list_active_triggers",
        "add_agent_note",
        "get_agent_activity_log",
        "delete_data_item",
        # Coordination — meta_audit reviews team performance
        "post_finding",
        "read_team_findings",
        "request_investigation",
        "check_open_investigations",
    ],
    # ── Quant Research Agent ──
    "quant_research": [
        "search_web",
        "scrape_url",
        "search_wiki",
        "write_memory_note",
        "read_memory_note",
        "execute_python",
    ],
    # ── Portfolio Sizing Agent ──
    "portfolio_allocator": [
        "assess_risk_environment",
        "get_market_regime",
        "query_brain_graph",
        "calculate_portfolio_allocation",
        "calculate_stop_loss",
        "calculate_position_size",
        "calculate_risk_reward",
        "get_portfolio_state",
        "get_market_data",
        "get_technical_indicators",
    ],
    # ── Execution Agent ──
    "execution": [
        "get_portfolio_state",
        "get_market_data",
        "buy_stock",
        "sell_stock",
    ],
    # ── Post-Mortem Auditor Agent ──
    "post_mortem": [
        
        "scrape_url",
        "get_market_data",
        "get_finnhub_news",
        "write_memory_note",
        "read_memory_note",
    ],
    # ── Data Janitor Agent ──
    "data_janitor": [
        "search_internal_database",
        "trigger_database_cleanup",
        "get_latest_janitor_run_log",
        "get_agent_activity_log",
        "delete_data_item",
        "write_memory_note",
        "read_memory_note",
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
        "search_database_facts",
        "run_sql_query",
        "check_hallucination",
        # Performance
        "get_performance_metrics",
        "audit_decision_quality",
    ],
    # ── V3 Family Office Worker Agents ──
    "v3_worker_quant": [
        "get_market_data",
        "get_technical_indicators",
        "get_polygon_price_history",
        "get_options_flow",
        "query_technical_indicator",
    ],
    "v3_worker_fundamental": [
        "get_market_data",
        "get_finviz_fundamentals",
        "get_sec_filings",
        "get_earnings_data",
        "query_financial_metrics",
        "search_database_facts",
    ],
    "v3_worker_news": [
        "get_finnhub_news",
        "search_web",
        "search_database_facts",
        "search_internal_database",
    ],
    "v3_worker_insider": [
        "get_insider_trades",
        "get_congress_trades",
        "get_sec_filings",
        "search_database_facts",
    ],
}


def get_agent_tools(agent_name: str) -> Optional[list[dict]]:
    """Resolve tool schemas for a given agent from the whitelist.

    Returns:
        A filtered list of tool schemas if the agent has a whitelist,
        or None if the agent should receive all tools (legacy behavior).
    """
    if agent_name not in AGENT_TOOL_WHITELISTS:
        return None

    from app.tools.registry import registry

    tool_names = AGENT_TOOL_WHITELISTS[agent_name]
    schemas = registry.get_schemas_by_names(tool_names)

    # Warn if any whitelisted tools don't exist in the registry
    found_names = {s["function"]["name"] for s in schemas}
    missing = set(tool_names) - found_names
    if missing:
        logger.warning(
            "[ToolWhitelist] Agent '%s' references %d unregistered tools: %s",
            agent_name,
            len(missing),
            sorted(missing),
        )

    logger.debug(
        "[ToolWhitelist] Agent '%s' → %d/%d tools resolved",
        agent_name,
        len(schemas),
        len(tool_names),
    )
    return schemas if schemas else None


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
    # Data collectors — fetch and format, no deep reasoning needed
    "retriever": 5,
    "data_janitor": 5,
    "comparative": 3,
    # Specialist analysts — need tool calls + reasoning
    "sentiment": 8,
    "technical": 6,
    "fundamental": 8,
    "risk": 8,
    "fund_flow": 6,
    "quant_research": 8,
    # Debate agents — need multiple rounds of tool calls for evidence
    "bull_agent": 10,
    "bear_agent": 10,
    # Decision / synthesis agents — need to review everything
    "synthesizer": 8,
    "pre_trade": 8,
    "portfolio_allocator": 8,
    "execution": 5,
    # Audit / meta agents — review multiple dimensions
    "meta_audit": 12,
    "post_mortem": 10,
    "verifier": 5,
    # User chat — generous budget for interactive sessions
    "user_chat": 15,
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
