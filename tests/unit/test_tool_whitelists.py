"""
Test: Tool Whitelists — Verify per-agent tool filtering.

Validates that:
1. Every tool name in every whitelist actually exists in the registry
2. No whitelist exceeds 20 tools (sanity cap)
3. Critical agent→tool mappings are present
4. get_agent_tools() returns filtered schemas
"""

import pytest


def test_all_whitelisted_tools_exist_in_registry():
    """Every tool name in AGENT_TOOL_WHITELISTS should exist in registry.tools.

    Note: In test environments without full dependencies (psycopg, aiohttp, etc.),
    the registry may only be partially populated. This test warns on missing tools
    but only fails if the registry has >30 tools registered (meaning deps are available).
    """
    from app.agents.tool_whitelists import AGENT_TOOL_WHITELISTS
    from app.tools.registry import registry

    registered_names = set(registry.tools.keys())

    # If registry is under-populated due to missing deps, skip gracefully
    if len(registered_names) < 30:
        pytest.skip(
            f"Registry only has {len(registered_names)} tools "
            f"(expected 60+). Missing runtime deps."
        )

    # Ignore remote tools not expected to be in the local registry
    remote_tools = {"youtube_transcript", "upsert_memory", "run_sql_query", "search_web"}

    missing = {
        agent: [t for t in tool_list if t not in registered_names and t not in remote_tools]
        for agent, tool_list in AGENT_TOOL_WHITELISTS.items()
        if any(t not in registered_names and t not in remote_tools for t in tool_list)
    }

    assert not missing, f"Unregistered tools in whitelists: {missing}"


def test_no_whitelist_exceeds_cap():
    """No agent whitelist should have more than 20 tools, except user_chat."""
    from app.agents.tool_whitelists import AGENT_TOOL_WHITELISTS

    MAX_CAP = 20
    for agent, tool_list in AGENT_TOOL_WHITELISTS.items():
        if agent == "user_chat":
            continue
        assert len(tool_list) <= MAX_CAP, (
            f"Agent '{agent}' has {len(tool_list)} tools (cap={MAX_CAP})"
        )



def test_quant_analyst_has_calculator_tools():
    """Quant Analyst agent MUST have the calculator tools nothing precomputes.

    This list has shrunk twice, both times because the answer moved INTO the
    prompt rather than out of the agent's reach:
      - calculate_risk_reward, dropped 2026-07-25 (zero calls in 60 days, and
        nothing in the prompt asked for it).
      - calculate_hrp_allocation and forecast_volatility_garch, dropped
        2026-07-28: app/quant/context_block.py computes the GARCH next-day vol
        forecast and this ticker's HRP target weight in code, and agent_runner
        injects them as quant_math_context into v3_quant_analyst's prompt. The
        agent was measured copying that block 127/127 faithfully — it already
        has the numbers, so the tools were a slower second route to them
        against a 7-turn budget.
    What remains is the set with NO precomputed equivalent.
    """
    from app.agents.tool_whitelists import AGENT_TOOL_WHITELISTS

    quant_tools = set(AGENT_TOOL_WHITELISTS.get("v3_quant_analyst", []))
    # 2026-07-21 portfolio-math wave: calculate_position_size (flat
    # cash-percent) was replaced by covariance-aware sizing.
    required = {
        "calculate_stop_loss",
        # Returns a MATRIX, not the single weight the block carries — step 5 of
        # the prompt still names it for correlation structure.
        "get_portfolio_covariance",
    }
    missing = required - quant_tools
    assert not missing, f"Quant Analyst agent missing calculator tools: {missing}"


def test_user_chat_has_buy_and_calculators():
    """user_chat agent MUST have calculator tools."""
    from app.agents.tool_whitelists import AGENT_TOOL_WHITELISTS

    chat_tools = set(AGENT_TOOL_WHITELISTS.get("user_chat", []))
    # calculate_portfolio_allocation was removed in the 2026-07-14
    # dead-tool purge (schema with no implementation).
    required = {
        "calculate_stop_loss",
        "calculate_position_size",
        "calculate_risk_reward",
    }
    missing = required - chat_tools
    assert not missing, f"user_chat agent missing required tools: {missing}"


def test_portfolio_manager_has_search_tools():
    """v3_portfolio_manager MUST have search and news tools."""
    from app.agents.tool_whitelists import AGENT_TOOL_WHITELISTS

    pm_tools = set(AGENT_TOOL_WHITELISTS.get("v3_portfolio_manager", []))
    required = {
        "get_finnhub_news",
        "lazy_web_search",
    }
    missing = required - pm_tools
    assert not missing, f"v3_portfolio_manager missing required tools: {missing}"


def test_sentiment_agent_does_not_have_calculator_tools():
    """Debate agents should NOT have calculator tools (irrelevant)."""
    from app.agents.tool_whitelists import AGENT_TOOL_WHITELISTS

    bull_tools = set(AGENT_TOOL_WHITELISTS.get("v3_bull_agent", []))
    calc_tools = {
        "calculate_stop_loss",
        "calculate_position_size",
        "calculate_risk_reward",
        "calculate_portfolio_allocation",
    }
    overlap = bull_tools & calc_tools
    assert not overlap, f"Debate agents should not have calculator tools: {overlap}"


def test_get_agent_tools_returns_filtered_schemas():
    """get_agent_tools() should return only the whitelisted schemas."""
    from app.agents.tool_whitelists import get_agent_tools, AGENT_TOOL_WHITELISTS

    schemas = get_agent_tools("v3_quant_analyst")
    assert schemas is not None, "v3_quant_analyst agent should have a whitelist"

    schema_names = {s["function"]["name"] for s in schemas}
    expected = set(AGENT_TOOL_WHITELISTS["v3_quant_analyst"])

    # Only registered tools should appear (some may be missing if unregistered)
    assert schema_names.issubset(expected), (
        f"Unexpected tools in quant analyst schemas: {schema_names - expected}"
    )


def test_get_agent_tools_fails_closed_for_unknown():
    """Unknown agents get ZERO tools (fail-closed since 2026-07-14) —
    the old None (= full registry) fallback let unregistered agents
    self-expand into everything."""
    from app.agents.tool_whitelists import get_agent_tools

    result = get_agent_tools("nonexistent_agent_xyz")
    assert result == [], "Unknown agents must get an empty tool list"


def test_no_duplicate_tools_in_whitelists():
    """No agent should have duplicate tool names in its whitelist."""
    from app.agents.tool_whitelists import AGENT_TOOL_WHITELISTS

    for agent, tool_list in AGENT_TOOL_WHITELISTS.items():
        dupes = [t for t in tool_list if tool_list.count(t) > 1]
        assert not dupes, f"Agent '{agent}' has duplicate tools: {set(dupes)}"


def test_graph_learn_purged_from_whitelists():
    """graph_learn was removed in the 2026-07-14 dead-tool purge —
    it must not reappear in any whitelist without an implementation."""
    from app.agents.tool_whitelists import AGENT_TOOL_WHITELISTS

    for agent, tool_list in AGENT_TOOL_WHITELISTS.items():
        assert "graph_learn" not in tool_list, (
            f"graph_learn is back in '{agent}' but has no registered implementation"
        )
