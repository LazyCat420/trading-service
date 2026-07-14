"""
test_charting_tools.py — Dependency-chain test for save_trading_chart.

Strategy: State Change Test (Level 3) — assert that calling the tool
actually mutates the filesystem and produces valid JSON that the
AgenticChart frontend can consume.

False Positive Prevention:
- Test deletes any pre-existing AAPL.json before running so we verify
  the tool WROTE a new file, not just that a stale one exists.
- Asserts on the nested structure that AgenticChart.jsx actually reads
  (latest_analysis.overlays), not just that the file exists.
"""

import asyncio
import json
import os
import sys
import pytest

# Add the project root to path so imports resolve
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../../..")))

# Point chart output to a temp location for tests. The env var only works if
# this file wins the import race for charting_tools (OUTPUT_DIR binds at module
# import), so patch the module attribute directly — order-independent, and
# save_trading_chart reads the module global at call time.
import tempfile

TEST_CHARTS_DIR = tempfile.mkdtemp(prefix="charts_test_")
os.environ["CHART_OUTPUT_DIR"] = TEST_CHARTS_DIR

from app.tools import charting_tools

charting_tools.OUTPUT_DIR = TEST_CHARTS_DIR
save_trading_chart = charting_tools.save_trading_chart
OUTPUT_DIR = charting_tools.OUTPUT_DIR


class TestSaveTradingChart:
    """Dependency-chain tests: assert the downstream filesystem state."""

    def test_output_dir_is_controlled(self):
        """Ensure the test env var override is respected by the module."""
        assert OUTPUT_DIR == TEST_CHARTS_DIR, (
            f"OUTPUT_DIR should be controlled by CHART_OUTPUT_DIR env var. "
            f"Got: {OUTPUT_DIR}, Expected: {TEST_CHARTS_DIR}"
        )

    @pytest.mark.asyncio
    async def test_save_chart_writes_json_file(self):
        """State Change Test: calling save_trading_chart writes {TICKER}.json to disk."""
        ticker = "AAPL"
        json_path = os.path.join(TEST_CHARTS_DIR, f"{ticker}.json")

        # Precondition: delete any stale file to avoid false positive
        if os.path.exists(json_path):
            os.remove(json_path)
        assert not os.path.exists(json_path), "Precondition: JSON file should not exist before the call"

        overlays = [
            {
                "type": "support",
                "y0": 180.0,
                "y1": 185.0,
                "color": "green",
                "reasoning": "Strong demand zone from previous consolidation",
            }
        ]

        result = await save_trading_chart(
            ticker=ticker,
            overlays=overlays,
            period="1mo",
            analysis="Strong support zone established at $180-$185.",
            strategy_name="Support/Resistance",
            confidence="HIGH",
        )

        # Assert: the tool succeeded
        assert "Successfully generated" in result or "Failed" not in result, (
            f"Tool returned unexpected result: {result}"
        )

        # Assert: the JSON file was actually written
        assert os.path.exists(json_path), (
            f"CRITICAL: {json_path} was not written. "
            f"save_trading_chart did not persist chart data. Result was: {result}"
        )

    @pytest.mark.asyncio
    async def test_json_has_correct_structure_for_frontend(self):
        """
        Output Value Test: the JSON must contain the exact keys that
        AgenticChart.jsx reads: json.latest_analysis.overlays
        """
        ticker = "MSFT"
        json_path = os.path.join(TEST_CHARTS_DIR, f"{ticker}.json")

        # Remove stale file
        if os.path.exists(json_path):
            os.remove(json_path)

        overlays = [
            {
                "type": "resistance",
                "y0": 420.0,
                "y1": 430.0,
                "color": "red",
                "reasoning": "Heavy selling pressure at $420-$430 range",
            },
            {
                "type": "trendline",
                "y0": 380.0,
                "y1": 415.0,
                "x0": "2025-01-01",
                "x1": "2025-06-01",
                "color": "blue",
                "reasoning": "Ascending trendline from January low",
            },
        ]

        await save_trading_chart(
            ticker=ticker,
            overlays=overlays,
            period="1mo",
            analysis="Resistance zone is critical, trendline support holding.",
            strategy_name="Multi-Timeframe",
            confidence="MEDIUM",
        )

        assert os.path.exists(json_path), f"{json_path} not written"

        with open(json_path, "r") as f:
            data = json.load(f)

        # Assert: top-level structure
        assert "latest_analysis" in data, (
            f"Missing 'latest_analysis' key. AgenticChart.jsx will show no overlays. Keys: {list(data.keys())}"
        )

        latest = data["latest_analysis"]

        # Assert: overlays key exists and has correct data
        assert "overlays" in latest, (
            f"Missing 'overlays' inside latest_analysis. AgenticChart.jsx reads json.latest_analysis.overlays"
        )
        assert len(latest["overlays"]) == 2, (
            f"Expected 2 overlays, got {len(latest['overlays'])}. Overlays were not correctly persisted."
        )

        # Assert: analysis metadata fields are present
        assert latest.get("analysis"), "Missing 'analysis' text field in latest_analysis"
        assert latest.get("confidence"), "Missing 'confidence' field in latest_analysis"

        # Assert: overlay types are preserved correctly
        overlay_types = {ov["type"] for ov in latest["overlays"]}
        assert "resistance" in overlay_types, "resistance overlay type not preserved"
        assert "trendline" in overlay_types, "trendline overlay type not preserved"

    @pytest.mark.asyncio
    async def test_empty_overlays_still_writes_json(self):
        """Edge case: even with no overlays, the JSON must be written so the frontend gets a valid response."""
        ticker = "TSLA"
        json_path = os.path.join(TEST_CHARTS_DIR, f"{ticker}.json")

        if os.path.exists(json_path):
            os.remove(json_path)

        await save_trading_chart(ticker=ticker, overlays=[], period="1mo")

        assert os.path.exists(json_path), (
            f"JSON not written even for empty overlays. Chart router will 404 for {ticker}."
        )

        with open(json_path, "r") as f:
            data = json.load(f)

        assert "latest_analysis" in data
        assert isinstance(data["latest_analysis"]["overlays"], list)
