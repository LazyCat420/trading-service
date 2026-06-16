"""
Unit Tests for LLM Auditor — Drift and Degradation Detection.
"""

import os
import sys
import pytest
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from app.autoresearch.auditors.llm_audit import _audit_llm_traces


class TestLLMAuditor:
    """Test the _audit_llm_traces function's happy path and drift warnings."""

    @patch("app.monitoring.llm_tracker.tracker.get_stats")
    @patch("app.pipeline.subsystem_benchmarks.get_trends")
    def test_no_history_happy_path(self, mock_get_trends, mock_get_stats):
        # Setup: Current stats show 0 calls
        mock_get_stats.return_value = {"total_calls": 0, "failed_calls": 0}
        mock_get_trends.return_value = []

        res = _audit_llm_traces("cycle-123")
        assert res["score"] == 1.0
        assert res["total_calls"] == 0
        assert res["issues"] == []

    @patch("app.monitoring.llm_tracker.tracker.get_stats")
    @patch("app.pipeline.subsystem_benchmarks.get_trends")
    def test_happy_path_with_good_calls(self, mock_get_trends, mock_get_stats):
        # Setup: Current stats show 10 calls, 0 failures
        mock_get_stats.return_value = {"total_calls": 10, "failed_calls": 0}
        mock_get_trends.return_value = []

        res = _audit_llm_traces("cycle-123")
        assert res["score"] == 1.0
        assert res["total_calls"] == 10
        assert res["issues"] == []

    @patch("app.monitoring.llm_tracker.tracker.get_stats")
    @patch("app.pipeline.subsystem_benchmarks.get_trends")
    def test_drift_degradation_detected(self, mock_get_trends, mock_get_stats):
        # Setup: Rolling historical average is 95% (0.95 score)
        # Current run has 5 calls, 1 failed (20% failure, score = max(0, 1.0 - 0.2*2) = 0.6)
        mock_get_stats.return_value = {"total_calls": 5, "failed_calls": 1}
        mock_get_trends.return_value = [
            {"cycle_id": "c1", "metrics": {"llm_performance_score": 95.0}},
            {"cycle_id": "c2", "metrics": {"llm_performance_score": 96.0}},
            {"cycle_id": "c3", "metrics": {"llm_performance_score": 94.0}},
        ]

        res = _audit_llm_traces("cycle-123")
        assert res["score"] == 0.6
        assert res["failed_calls"] == 1
        assert res["historical_average"] == 0.95
        
        # Check if the degradation warning is returned
        has_degrad_warn = any("degraded" in issue["issue"] for issue in res["issues"])
        assert has_degrad_warn is True

    @patch("app.monitoring.llm_tracker.tracker.get_stats")
    @patch("app.pipeline.subsystem_benchmarks.get_trends")
    def test_no_degradation_when_stable(self, mock_get_trends, mock_get_stats):
        # Setup: Rolling historical average is 70% (0.7 score)
        # Current run has 5 calls, 0 failed (100% success, score = 1.0)
        mock_get_stats.return_value = {"total_calls": 5, "failed_calls": 0}
        mock_get_trends.return_value = [
            {"cycle_id": "c1", "metrics": {"llm_performance_score": 70.0}},
            {"cycle_id": "c2", "metrics": {"llm_performance_score": 72.0}},
            {"cycle_id": "c3", "metrics": {"llm_performance_score": 68.0}},
        ]

        res = _audit_llm_traces("cycle-123")
        assert res["score"] == 1.0
        # Score improved/stable, so no degradation warning
        has_degrad_warn = any("degraded" in issue["issue"] for issue in res["issues"])
        assert has_degrad_warn is False
