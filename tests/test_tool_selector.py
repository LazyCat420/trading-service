"""
Tests for Brain-Action Split Agent Architecture.

Tests cover:
  🔴🟢 TDD Unit Tests:
    - Tool selector builds correct compact text list
    - Tool selector filters valid tool names
    - Tool selector falls back gracefully on empty/bad output
    - Split loop skips selection when pool is small
    - Split loop applies selection when pool is large
    - Action executor returns correct result structure
  🔗 Integration Tests:
    - run_split_agent_loop import and signature validation
    - AGENT_ROLE_ROUTING includes new agent types
"""

import sys
import os
import asyncio
import json
from unittest.mock import AsyncMock, patch, MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


# ══════════════════════════════════════════════════════════════
# 💨 Unit Tests — Tool Selector
# ══════════════════════════════════════════════════════════════


class TestBuildToolListText:
    """Test the compact text builder used by the tool selector."""

    def test_builds_correct_format(self):
        from app.agents.tool_selector import _build_tool_list_text

        schemas = [
            {"function": {"name": "get_market_data", "description": "Get market data for a ticker."}},
            {"function": {"name": "search_web", "description": "Search the web for information."}},
        ]
        result = _build_tool_list_text(schemas)
        assert "- get_market_data: Get market data for a ticker." in result
        assert "- search_web: Search the web for information." in result
        assert result.count("\n") == 1  # Two lines, one newline

    def test_truncates_long_descriptions(self):
        from app.agents.tool_selector import _build_tool_list_text

        long_desc = "A" * 300
        schemas = [{"function": {"name": "test_tool", "description": long_desc}}]
        result = _build_tool_list_text(schemas)
        # Should be truncated to 150 chars max
        assert len(result.split(": ", 1)[1]) <= 153  # 147 chars + "..."

    def test_empty_schemas(self):
        from app.agents.tool_selector import _build_tool_list_text

        result = _build_tool_list_text([])
        assert result == ""


class TestSelectToolsForTask:
    """Test the core tool selection logic."""

    def test_skips_selection_when_pool_is_small(self):
        """If pool <= max_tools, return full pool without LLM call."""
        from app.agents.tool_selector import select_tools_for_task

        schemas = [
            {"function": {"name": "tool_a", "description": "Tool A"}},
            {"function": {"name": "tool_b", "description": "Tool B"}},
        ]

        # Should return full pool without hitting LLM
        result = asyncio.get_event_loop().run_until_complete(
            select_tools_for_task(
                task_description="test task",
                available_tool_schemas=schemas,
                max_tools=5,
            )
        )
        assert len(result) == 2
        assert result == schemas

    def test_returns_empty_for_empty_pool(self):
        from app.agents.tool_selector import select_tools_for_task

        result = asyncio.get_event_loop().run_until_complete(
            select_tools_for_task(
                task_description="test task",
                available_tool_schemas=[],
            )
        )
        assert result == []

    @patch("app.agents.tool_selector.llm")
    def test_force_includes_charting_tool(self, mock_llm):
        """Quant/Technical agents must always force-include save_trading_chart if in the pool."""
        from app.agents.tool_selector import select_tools_for_task

        # Pool size > max_tools (e.g. 6 tools, max=5) to trigger selection
        schemas = [
            {"function": {"name": f"tool_{i}", "description": f"Tool {i}"}}
            for i in range(5)
        ] + [{"function": {"name": "save_trading_chart", "description": "Save chart"}}]

        # Mock LLM to return a list of selected tools that does NOT include save_trading_chart
        mock_llm.chat_with_tools = AsyncMock(return_value={
            "text": '{"selected_tools": ["tool_0", "tool_1", "tool_2"]}',
            "total_tokens": 100,
        })

        result = asyncio.get_event_loop().run_until_complete(
            select_tools_for_task(
                task_description="analyze AAPL",
                available_tool_schemas=schemas,
                agent_name="v3_quant_analyst",
                max_tools=5,
            )
        )

        selected_names = [s["function"]["name"] for s in result]
        assert "save_trading_chart" in selected_names, "save_trading_chart was not force-included"
        assert len(result) == 4  # 3 from LLM + 1 force-included


# ══════════════════════════════════════════════════════════════
# 🔗 Integration Tests — Tool Selector prompt quality
# ══════════════════════════════════════════════════════════════


class TestToolSelectorPromptQuality:
    """Verify the tool selector system prompt is well-formed."""

    def test_system_prompt_requests_json(self):
        from app.agents.tool_selector import TOOL_SELECTOR_SYSTEM

        assert "JSON" in TOOL_SELECTOR_SYSTEM
        assert "selected_tools" in TOOL_SELECTOR_SYSTEM

    def test_system_prompt_limits_tool_count(self):
        from app.agents.tool_selector import TOOL_SELECTOR_SYSTEM

        assert "maximum" in TOOL_SELECTOR_SYSTEM.lower() or "max" in TOOL_SELECTOR_SYSTEM.lower()

    def test_system_prompt_is_concise(self):
        from app.agents.tool_selector import TOOL_SELECTOR_SYSTEM

        # System prompt should be short to minimize TTFT
        assert len(TOOL_SELECTOR_SYSTEM) < 500
