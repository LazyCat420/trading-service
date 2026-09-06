"""A cycle's recovery summary must count the recoveries that happened.

MEASURED 2026-09-06 across **329 autoresearch reports in 40 days**:

    recovery_stats.total_failures :  0 in 247 rows, ABSENT in 82 — never once
                                     non-zero
    recovery_stats.cycle_id       : "" in 247 rows, missing in 82 — never once
                                     set

Today's report, on a cycle with a crashed agent, an exhausted 5-attempt
resilience budget, six 300 s stream stalls, two empty responses and 22 failed
tool calls, read:

    {"cycle_id": "", "total_failures": 0, "by_type": {}, "by_agent": {},
     "circuit_breakers_tripped": 0, "active_failure_counters": {},
     "recent_events": []}

The cause is structural, not a bug in the counting. `app/recovery/engine.py`
exposes a singleton with `reset_cycle()`, `get_stats()`, `get_history()` and a
`handle(FailureEvent)` entry point advertised in the package docstring.
Grepping the tree for `recovery_engine.` outside `app/recovery/` returns
exactly two lines, both in `_audit_recovery`, and both are READS. Nothing calls
`reset_cycle` — hence the empty cycle_id — and nothing ever records a failure
into it — hence the permanent zero. One consumer, zero producers.

That is worse than a missing field. A blank invites a question; a confident `0`
closes it. `audit_bundle["recovery"]` is fed to the reflection prompt, so the
model that writes the cycle's self-assessment is told it recovered from nothing.

The data has been there the whole time: every one of today's failures is a row
in `cycle_audit_log` or `pipeline_events`, both keyed by `cycle_id`. These tests
pin the rebuild from those two collections.
"""
from __future__ import annotations

from datetime import datetime
from unittest.mock import patch

import pytest

from app.autoresearch.auditors.performance_audit import _audit_recovery

CYCLE = "cycle-v3-1788646388"


def _log(message: str, severity: str = "error", ts: datetime | None = None) -> dict:
    return {
        "cycle_id": CYCLE,
        "severity": severity,
        "message": message,
        "timestamp": ts or datetime(2026, 9, 5, 23, 20, 53),
    }


def _event(step: str, detail: str = "", phase: str = "analyzing",
           status: str = "error", ts: datetime | None = None) -> dict:
    return {
        "cycle_id": CYCLE,
        "phase": phase,
        "step": step,
        "detail": detail,
        "status": status,
        "timestamp": ts or datetime(2026, 9, 5, 23, 20, 53),
    }


# The real rows from the acceptance cycle, trimmed to what the auditor reads.
GOOG_BULL_CRASH = [
    _log("[ERROR] Prism stream error: Provider stream stalled: no data received for 300s"),
    _log("[ERROR] Prism stream error: Provider stream stalled: no data received for 300s"),
    _log("[ERROR] [RESILIENCE] run_agent.<locals>._agent_llm_call: all 5 attempts failed (last: fatal)"),
    _log("[ERROR] [V3Runner] v3_bull_agent CRASHED for GOOG: ResilientCallError(...)"),
    _log("[ERROR] [BaseAgent] EMPTY RESPONSE from v3_decision_synthesizer (GOOG): raw=''"),
]
GOOG_BULL_EVENTS = [
    _event("retry_run_agent.<locals>._agent_llm_call",
           "Attempt 2/5 failed: RuntimeError: Prism harness error", phase="recovery"),
    _event("v3_v3_bull_agent_crash_GOOG",
           "GOOG: V3 v3_bull_agent CRASHED — ResilientCallError"),
]


def _store(logs, events):
    def find_docs(collection, query=None, **kwargs):
        if collection == "cycle_audit_log":
            return list(logs)
        if collection == "pipeline_events":
            return list(events)
        return []
    return find_docs


def _run(logs=(), events=(), cycle_id=CYCLE):
    with patch("app.autoresearch.auditors.performance_audit.mongo_store") as ms:
        ms.find_docs.side_effect = _store(logs, events)
        return _audit_recovery(cycle_id)


class TestTheAcceptanceCycle:
    def test_the_crashed_bull_agent_is_counted(self):
        stats = _run(GOOG_BULL_CRASH, GOOG_BULL_EVENTS)

        assert stats["cycle_id"] == CYCLE, "cycle_id is still not being set"
        assert stats["total_failures"] >= 2, (
            f"a cycle with a CRASHED agent and an exhausted retry budget "
            f"reported {stats['total_failures']} failures"
        )

    def test_the_failure_classes_are_named(self):
        stats = _run(GOOG_BULL_CRASH, GOOG_BULL_EVENTS)
        by_type = stats["by_type"]

        assert by_type.get("stream_stalled") == 2
        assert by_type.get("agent_crashed") >= 1
        assert by_type.get("resilience_exhausted") >= 1
        assert by_type.get("empty_response") >= 1

    def test_the_agent_that_died_is_named(self):
        stats = _run(GOOG_BULL_CRASH, GOOG_BULL_EVENTS)

        assert "v3_bull_agent" in stats["by_agent"], (
            f"by_agent does not name the crashed agent: {stats['by_agent']}"
        )

    def test_resilience_exhaustion_is_called_out_separately(self):
        """A run that burned its whole retry budget is the one an operator has
        to see; it is not just another failure in the pile."""
        stats = _run(GOOG_BULL_CRASH, GOOG_BULL_EVENTS)
        assert stats["resilience_exhausted"] >= 1

    def test_recent_events_carry_something_readable(self):
        stats = _run(GOOG_BULL_CRASH, GOOG_BULL_EVENTS)

        assert stats["recent_events"], "recent_events is still empty"
        assert len(stats["recent_events"]) <= 10
        assert all(isinstance(e, dict) for e in stats["recent_events"])
        assert any("CRASHED" in str(e) for e in stats["recent_events"])


class TestACleanCycle:
    def test_a_cycle_with_no_failures_reports_zero_but_names_itself(self):
        """The honest zero. It is distinguishable from the old one because the
        cycle_id is set — which is how a reader can tell 'nothing went wrong'
        from 'nobody looked'."""
        stats = _run([], [])

        assert stats["total_failures"] == 0
        assert stats["by_type"] == {}
        assert stats["cycle_id"] == CYCLE

    def test_an_informational_row_is_not_a_failure(self):
        """88% of the ERROR stream is a deliberate non-abort (Appendix I). The
        ManagerAgent soft-timer fires while an agent is working NORMALLY, and
        counting it would make every healthy cycle look like a disaster."""
        stats = _run([
            _log("[ERROR] [ManagerAgent] Agent v3_junior_analyst took too much "
                 "time (410.9s) over 5 tool turns without completing.")
        ], [])

        assert stats["total_failures"] == 0, (
            "a soft-timer warning was counted as a recovery event"
        )


class TestItCannotTakeDownAReport:
    def test_a_store_failure_degrades_to_a_labelled_unknown(self):
        """This runs inside the autoresearch report. A Mongo hiccup must not
        lose the report — but it must ALSO not report a clean zero, which is
        the exact failure this whole change exists to remove."""
        with patch("app.autoresearch.auditors.performance_audit.mongo_store") as ms:
            ms.find_docs.side_effect = RuntimeError("mongo is gone")
            stats = _audit_recovery(CYCLE)

        assert stats["total_failures"] is None, (
            "an unreadable store must not be reported as zero failures"
        )
        assert stats.get("error")

    def test_a_missing_cycle_id_is_not_an_empty_string(self):
        stats = _run([], [], cycle_id="")
        assert stats["cycle_id"] == ""
        assert stats["total_failures"] == 0


class TestTheDeadEngineIsNoLongerConsulted:
    def test_the_auditor_does_not_read_the_recovery_engine(self):
        """The engine has no producers. Reading it is what produced 329
        confident zeros; if a future edit reintroduces the read, this fails.

        Checked by AST, not by grepping for the name: the docstring in
        `_audit_recovery` explains WHY the engine is not consulted, and a
        substring check fails on its own explanation. (It did, first run.)
        """
        import ast
        import inspect
        import pathlib

        from app.autoresearch.auditors import performance_audit

        tree = ast.parse(
            pathlib.Path(inspect.getsourcefile(performance_audit)).read_text()
        )

        imports = [
            f"{node.module}:{node.lineno}"
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom)
            and (node.module or "").startswith("app.recovery")
        ]
        uses = [
            f"recovery_engine.{node.attr}:{node.lineno}"
            for node in ast.walk(tree)
            if isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id == "recovery_engine"
        ]

        assert not imports and not uses, (
            "performance_audit consults app.recovery.engine again, which has "
            f"one consumer and zero producers: {imports + uses}"
        )
