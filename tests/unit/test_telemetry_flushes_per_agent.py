"""An agent's cost must survive the desk it was spent on.

MEASURED 2026-09-05 on the GLM cycle `cycle-v3-1788642086`: after the junior,
fundamental and quant analysts had all COMPLETED across three tickers, with 35
tool calls spent, `v3_agent_telemetry` held **0 rows** and `shared_desk` held
**0 rows**. Every entry sat on the in-memory desk until the first `save_desk`,
which lands only after the whole analyst chain.

`flush_agent_telemetry` was written for exactly this — its docstring says the
desk "flushes as it progresses" — but its only trigger was `save_desk`, so the
real granularity was the PHASE, not the agent.

Confirmed over 16 days: 7 of 132 finished cycles carry zero telemetry rows, and
5 of the 7 are `stopped` — killed before any desk saved. `cycle-v3-1788630137`
was stopped with 9 tickers in flight and lost the cost of all nine.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from app.v3 import agent_runner


class _Desk:
    """Minimal stand-in with the two surfaces the recorder touches."""

    def __init__(self):
        self.ticker = "LULU"
        self.cycle_id = "cycle-test"
        self.agent_telemetry: list[dict] = []

        class _Phase:
            value = "RESEARCH_DONE"

        self.phase = _Phase()

    def record_agent_telemetry(self, entry):
        self.agent_telemetry.append(entry)


def _record(desk, **kw):
    agent_runner._record_telemetry(
        desk, kw.pop("agent_name", "v3_junior_analyst"),
        kw.pop("elapsed_ms", 1000), kw.pop("loops_used", 6),
        kw.pop("token_usage", 125_234), kw.pop("outcome", "SUCCESS"), **kw,
    )


def test_the_row_is_flushed_when_the_agent_finishes_not_when_the_desk_saves():
    """The defect, asserted: one agent completes, one row is written."""
    desk = _Desk()
    with patch("app.v3.telemetry.flush_agent_telemetry") as flush:
        _record(desk)
    assert flush.call_count == 1, "no flush after the agent completed"
    assert flush.call_args[0][0] is desk


def test_every_agent_flushes_not_just_the_first():
    desk = _Desk()
    with patch("app.v3.telemetry.flush_agent_telemetry") as flush:
        for name in ("v3_junior_analyst", "v3_fundamental_analyst", "v3_quant_analyst"):
            _record(desk, agent_name=name)
    assert flush.call_count == 3
    assert len(desk.agent_telemetry) == 3


def test_a_failed_agent_still_has_its_cost_recorded():
    """A crashed agent spent its tokens; losing the row is the original bug."""
    desk = _Desk()
    with patch("app.v3.telemetry.flush_agent_telemetry") as flush:
        _record(desk, outcome="AGENT_ERROR", failure_reason=None,
                error_message="boom")
    assert flush.call_count == 1
    assert desk.agent_telemetry[0]["outcome"] == "AGENT_ERROR"


def test_a_failing_flush_never_breaks_the_agent():
    """Cost accounting must not fail a run that succeeded.

    The row stays pending and the next save retries it — that is what makes
    the new call safe to add to the hot path of every agent.
    """
    desk = _Desk()
    with patch("app.v3.telemetry.flush_agent_telemetry",
               side_effect=RuntimeError("mongo down")):
        _record(desk)  # must not raise
    assert len(desk.agent_telemetry) == 1


def test_the_entry_is_recorded_before_the_flush_is_attempted():
    """Order matters: flushing an unrecorded entry writes nothing."""
    desk = _Desk()
    seen = {}

    def _capture(d):
        seen["n"] = len(d.agent_telemetry)

    with patch("app.v3.telemetry.flush_agent_telemetry", side_effect=_capture):
        _record(desk)
    assert seen["n"] == 1, "flush ran before the entry was on the desk"
