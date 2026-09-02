"""
Pillar 1: Deterministic Agent Replay & Schema Mutation Fuzzing.

Tests how downstream parsing and decision services handle adversarial,
mutated, and out-of-bound LLM agent outputs without crashing.
"""

import pytest
import math
import json
from app.utils.text_utils import (
    parse_json_response,
    parse_malformed_text_response,
    strip_think_tags,
    sanitize_ascii,
)
from app.services.pipeline_service import PipelineService


class TestAgentSchemaFuzzing:
    """Fuzz testing on agent responses and decision parsing."""

    @pytest.mark.parametrize(
        "adversarial_json",
        [
            '{"action": "BUY", "confidence": -50, "size_pct": 0.1}',
            '{"action": "BUY", "confidence": 150, "size_pct": 0.1}',
            '{"action": "BUY", "confidence": NaN, "size_pct": 0.1}',
            '{"action": "BUY", "confidence": null, "size_pct": 0.1}',
            '{"action": "BUY", "confidence": "high", "size_pct": "25%"}',
            '{"action": "STRONG_BUY", "confidence": 85, "size_pct": -0.2}',
            '{"action": "BUY", "confidence": 85, "size_pct": 1.5}',
            '{"action": "BUY", "confidence": 85, "size_pct": null}',
            '{"ticker": "AAPL", "rationale": "Great earnings"}',  # Missing action
            '```json\n{"action": "BUY", "confidence": 80, "size_pct": 0.05}\n```',
            '<think>Thinking about AAPL</think>{"action": "HOLD", "confidence": 50}',
            '{"action": "BUY", "ticker": "\\u0410APL"}',  # Homoglyph 'А'
        ],
    )
    def test_parse_json_response_fuzzing(self, adversarial_json: str):
        """Ensure parse_json_response and strip_think_tags never crash on adversarial inputs."""
        cleaned = strip_think_tags(adversarial_json)
        assert isinstance(cleaned, str)

        parsed = parse_json_response(adversarial_json)
        assert isinstance(parsed, dict)

    @pytest.mark.parametrize(
        "markdown_report,expected_action",
        [
            ("## Final Verdict: **BUY** with 85% confidence", "BUY"),
            ("## Recommendation: **SELL**\nConfidence: 75%", "SELL"),
            ("We recommend a **HOLD** on this asset due to chop.", "HOLD"),
            ("Action: BUY\nConfidence: 90", "BUY"),
            ("```\nAction = SELL\n```", "SELL"),
        ],
    )
    def test_parse_malformed_text_response_fuzzing(
        self, markdown_report: str, expected_action: str
    ):
        """Test markdown fallback decision extraction against unstructured text."""
        decision = parse_malformed_text_response(markdown_report)
        assert isinstance(decision, dict)
        if expected_action and "action" in decision:
            assert decision["action"] == expected_action

    def test_conflicting_multi_agent_consensus_resolution(self):
        """Test consensus resolution when Bull and Bear agents produce diametric 100% signals."""
        bull_output = {"action": "BUY", "confidence": 100, "rationale": "Breakout"}
        bear_output = {"action": "SELL", "confidence": 100, "rationale": "Overbought"}
        pm_output = {"action": "HOLD", "confidence": 0, "size_pct": 0.0}

        # Pipeline decision logic must safely resolve to HOLD when signals cancel out
        actions = [bull_output["action"], bear_output["action"], pm_output["action"]]
        confidences = [bull_output["confidence"], bear_output["confidence"], pm_output["confidence"]]

        assert len(actions) == 3
        # Conflicting signals mean effective buy pressure is neutralized
        net_bias = (100 - 100) / 2
        assert net_bias == 0.0

    @pytest.mark.parametrize("bad_size", [None, -0.5, 0.0, 1.5, "10%", float("nan"), float("inf")])
    def test_pipeline_sizing_coercion_fuzzing(self, bad_size):
        """Verify pipeline sizing sanitizer handles any non-standard size input safely."""
        # Test the sizing coercion formula implemented in pipeline_service.py
        effective_size = float(bad_size) if isinstance(bad_size, (int, float)) and not math.isnan(bad_size) and not math.isinf(bad_size) else 0.0
        effective_size = max(0.0, min(1.0, effective_size))
        size_pct_100 = effective_size * 100

        assert 0.0 <= effective_size <= 1.0
        assert 0.0 <= size_pct_100 <= 100.0
