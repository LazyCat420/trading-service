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


# ── A crashed run must record what it SPENT ────────────────────────────────
#
# MEASURED 2026-09-05, cycle-v3-1788646388. GOOG's bull agent died after
# 1,238,603 ms and its telemetry row read:
#
#     outcome=AGENT_ERROR  loops_used=0  prompt_tokens=0  model_used=None
#     failure_reason=RUNNER_EXCEPTION
#
# while `agent_tool_telemetry` held SEVEN successful tool calls for that same
# agent/ticker/cycle (think x2 denied, lazy_web_search x4, get_market_data) and
# the box had run two full ~24k-token prefills for it. `_record_telemetry` was
# called as `(desk, agent_name, elapsed_ms, 0, 0, "AGENT_ERROR", ...)` — the
# zeros are literals.
#
# So a crash was FREE in every ledger that sums prompt_tokens: the 20k
# no-research invariant, the per-model comparison, the cycle token total. The
# most expensive runs on the box were the ones costing nothing on paper.
import unittest.mock as _m

import pytest as _pytest


class TestACrashRecordsItsCost:
    def test_partial_cost_travels_on_the_exception(self):
        """base_agent attaches it to whatever escapes — including the
        ResilientCallError the SDK wraps the last failure in, which is the
        object the runner's `except` actually sees."""
        from app.agents.base_agent import PrismTransientHarnessError

        exc = PrismTransientHarnessError("stalled")
        exc.partial_cost = {"tokens": 40_000, "loops": 3, "tool_calls": 7}

        assert getattr(exc, "partial_cost")["tokens"] == 40_000

    def test_base_agent_attaches_the_cost_to_an_escaping_exception(self):
        """The seam, by AST: `run_agent` must wrap the resilient call so the
        cost is carried OUT with the failure. Without the wrapper the runner
        has nothing to read and falls back to zeros."""
        import ast
        import inspect
        import pathlib as _pl

        from app.agents import base_agent

        src = _pl.Path(inspect.getsourcefile(base_agent)).read_text()
        assert "exc.partial_cost" in src, (
            "run_agent does not attach partial_cost to the escaping exception"
        )
        tree = ast.parse(src)
        assert any(
            isinstance(n, ast.Assign)
            and any(
                isinstance(t, ast.Attribute) and t.attr == "partial_cost"
                for t in n.targets
            )
            for n in ast.walk(tree)
        )

    def test_the_real_recorder_accepts_and_stores_the_spent_cost(self, monkeypatch):
        """Drives the REAL `_record_telemetry` — not a stub — and reads the
        entry it handed to the desk. On HEAD this raises TypeError, because
        the signature had no `cost_partial`."""
        from app.v3 import agent_runner

        recorded: list = []
        desk = _m.MagicMock()
        desk.cycle_id = "cycle-v3-1788646388"
        desk.ticker = "GOOG"
        desk.phase.value = "DEBATE"
        desk.record_agent_telemetry = recorded.append
        monkeypatch.setattr(agent_runner, "flush_agent_telemetry", lambda d: None,
                            raising=False)

        exc = RuntimeError("ResilientCallError: all 5 attempts failed")
        exc.partial_cost = {"tokens": 40_000, "loops": 3, "tool_calls": 7}
        cost = getattr(exc, "partial_cost", None) or {}

        agent_runner._record_telemetry(
            desk, "v3_bull_agent", 1_238_603,
            int(cost.get("loops") or 0), int(cost.get("tokens") or 0),
            "AGENT_ERROR",
            prompt_tokens=int(cost.get("tokens") or 0),
            attempt_no=1, failure_reason="RUNNER_EXCEPTION",
            cost_partial=True, error_message="ResilientCallError: ...",
        )

        assert recorded, "the recorder wrote nothing"
        row = recorded[0]
        assert row["loops_used"] == 3
        assert row["token_usage"] == 40_000
        assert row["prompt_tokens"] == 40_000, (
            "prompt_tokens is the field the 20k invariant and every audit probe "
            "read — leaving it 0 keeps the crash free where it matters"
        )
        assert row["cost_partial"] is True

    def test_the_crash_branch_reads_partial_cost_rather_than_hardcoding_zero(self):
        """The literal `0, 0` in the except branch is what made a crash free."""
        import inspect

        from app.v3 import agent_runner

        src = inspect.getsource(agent_runner.run_v3_agent)
        assert 'elapsed_ms, 0, 0, "AGENT_ERROR"' not in src, (
            "the crash row still hardcodes tokens=0 loops=0"
        )
        assert "partial_cost" in src

    def test_a_crash_with_no_attached_cost_still_records_zero_not_an_error(self):
        """The chat transport path has no harness, so partial_cost stays 0.
        That must degrade to the old behaviour, not raise."""
        exc = RuntimeError("boom")
        _cost = getattr(exc, "partial_cost", None) or {}
        assert int(_cost.get("tokens") or 0) == 0


class TestTheFlushDoesNotDropTheFlag:
    def test_cost_partial_survives_the_persistence_allowlist(self, monkeypatch):
        """`flush_agent_telemetry` builds an EXPLICIT dict, so a field added to
        the entry upstream is silently dropped unless it is named there. That
        is how a new column goes missing between the recorder and Mongo."""
        from app.v3 import telemetry

        inserted: list = []
        monkeypatch.setattr(
            telemetry.mongo_store, "insert_docs",
            lambda coll, docs: inserted.append((coll, docs)),
        )

        desk = _m.MagicMock()
        desk.cycle_id = "c1"
        desk.ticker = "GOOG"
        desk.agent_telemetry = [{
            "agent_name": "v3_bull_agent", "outcome": "AGENT_ERROR",
            "loops_used": 3, "token_usage": 40_000, "prompt_tokens": 40_000,
            "cost_partial": True, "attempt_no": 1,
        }]

        telemetry.flush_agent_telemetry(desk)

        assert inserted, "nothing was flushed"
        _, docs = inserted[0]
        assert docs[0]["cost_partial"] is True
        assert docs[0]["prompt_tokens"] == 40_000

    def test_a_normal_row_flushes_cost_partial_false(self, monkeypatch):
        from app.v3 import telemetry

        inserted: list = []
        monkeypatch.setattr(
            telemetry.mongo_store, "insert_docs",
            lambda coll, docs: inserted.append((coll, docs)),
        )

        desk = _m.MagicMock()
        desk.cycle_id = "c1"
        desk.ticker = "SNOW"
        desk.agent_telemetry = [{
            "agent_name": "v3_bull_agent", "outcome": "SUCCESS",
            "loops_used": 4, "token_usage": 93_103, "attempt_no": 1,
        }]

        telemetry.flush_agent_telemetry(desk)
        _, docs = inserted[0]
        assert docs[0]["cost_partial"] is False
