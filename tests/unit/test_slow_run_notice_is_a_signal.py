"""31 of a cycle's 40 error rows were one warning re-firing, and the real slow
tools never appeared.

MEASURED 2026-09-06, cycle-v3-1788660665. `cycle_audit_log` held 31 rows of
"[ManagerAgent] Agent <x> took too much time (<N>s) over <k> tool turns", severity
`critical`: fundamental analyst 20× (191–963 s, 5–14 turns), junior 8×
(323–422 s), valuation 2×, bull 1× — and three of the four agents SUCCEEDED. The
check was a level (`elapsed_s > 180 and tool_call_count > 4`) evaluated after
EVERY tool result, at `logger.error`, with a bare literal that matches no real
deadline (read-through 12 s, bridge 30/40 s, MCP 55 s, prism watchdog 300 s,
runner 600–1800 s), and it named no tool.

Meanwhile `agent_tool_telemetry` for the same cycle: get_finnhub_news 50%
success with p95 49,753 ms; screener_query, get_institutional_holdings and
get_earnings_data pinned at 49.7 s; `think` 11 calls at 0% success. That is the
slow-tool signal, and nothing logged it.

So: the run notice fires ONCE, at WARNING, past a deadline DERIVED from the
runner's timeout, and names the last tool; and a tool call that dies at its
bridge deadline logs one line that names the tool.
"""
from __future__ import annotations

import logging

import pytest

from app.agents.base_agent import slow_run_notice
from app.v3 import tool_telemetry
from app.v3.tool_telemetry import BRIDGE_TOOL_DEADLINE_MS, record_tool_call

# verbatim from the first and last fundamental-analyst rows of the cycle
FIRST_FIRE = dict(elapsed_s=191.0, tool_call_count=5)
LAST_FIRE = dict(elapsed_s=963.0, tool_call_count=14)
RUNNER_TIMEOUT_S = 1800.0  # ANALYSIS_WORKER_TIMEOUT_SECONDS in production (deploy.sh)


class TestTheRunNotice:
    def test_the_old_literal_no_longer_fires_at_191s_under_a_1800s_budget(self):
        assert slow_run_notice(**FIRST_FIRE, soft_deadline_s=RUNNER_TIMEOUT_S * 0.5,
                               last_tool="screener_query", already_warned=False) is None

    def test_it_fires_once_past_half_the_budget_and_names_the_tool(self):
        msg = slow_run_notice(**LAST_FIRE, soft_deadline_s=RUNNER_TIMEOUT_S * 0.5,
                              last_tool="screener_query", already_warned=False)
        assert msg and "took too much time" in msg and "screener_query" in msg and "963" in msg

    def test_it_never_fires_twice(self):
        assert slow_run_notice(**LAST_FIRE, soft_deadline_s=900.0,
                               last_tool="screener_query", already_warned=True) is None

    def test_no_deadline_means_no_notice(self):
        assert slow_run_notice(**LAST_FIRE, soft_deadline_s=None,
                               last_tool="x", already_warned=False) is None

    def test_the_phrase_the_recovery_audit_excludes_is_kept(self):
        """performance_audit deliberately excludes this phrase from the recovery
        histogram; renaming it would make every healthy long run a 'failure'."""
        msg = slow_run_notice(**LAST_FIRE, soft_deadline_s=1.0, last_tool="t", already_warned=False)
        assert "took too much time" in msg


class TestTheToolDeadlineSignal:
    def _record(self, caplog, **kw):
        caplog.set_level(logging.WARNING, logger=tool_telemetry.__name__)
        from unittest.mock import patch
        with patch.object(tool_telemetry, "mongo_store"), patch.object(tool_telemetry, "_canary_check"):
            record_tool_call("cycle-v3-1788660665", "v3_fundamental_analyst", **kw)
        return [r for r in caplog.records if "[ToolDeadline]" in r.getMessage()]

    def test_a_call_that_died_at_the_deadline_is_named_once(self, caplog):
        # verbatim: get_finnhub_news, 49,753 ms, success False
        recs = self._record(caplog, tool_name="mcp__lazy-agent-service__get_finnhub_news",
                            success=False, elapsed_ms=49_753, ticker="ABT")
        assert len(recs) == 1 and recs[0].levelno == logging.WARNING
        assert "get_finnhub_news" in recs[0].getMessage() and "49" in recs[0].getMessage()

    def test_a_fast_failure_is_not_a_deadline_hit(self, caplog):
        assert self._record(caplog, tool_name="think", success=False, elapsed_ms=988, error_message="POLICY_DENIED") == []

    def test_a_slow_success_is_not_a_deadline_hit(self, caplog):
        assert self._record(caplog, tool_name="mcp__lazy-agent-service__get_institutional_holdings",
                            success=True, elapsed_ms=49_726) == []

    def test_the_deadline_is_the_bridges_not_a_guess(self):
        """55 s is MCP_TOOL_DEADLINE_MS in lazy-agent-service/config.ts, the
        hard per-call cap every measured 49.7 s call died under."""
        assert BRIDGE_TOOL_DEADLINE_MS == 55_000
        assert 49_753 >= 0.9 * BRIDGE_TOOL_DEADLINE_MS
