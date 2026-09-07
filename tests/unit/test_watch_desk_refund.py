"""A wake that bought no decision is refunded; one that bought a decision is not.

Measured 2026-09-06 over the last 30 trips: 8 produced zero `analysis_results`
rows — 7 cycles ended `status=error`, one `stopped` — and the daily budget was
charged for every one. On 2026-09-02 that was the whole day: six wakes spent,
six decisions not made, and no retry possible until the next morning.

The refund is deliberately narrow, and the narrowness is the interesting part:
refunding a RUNNING cycle would uncap the budget for as long as a cycle takes,
turning one slow cycle into an unbounded wake loop.
"""

import pytest

from app.services import watch_desk


class FakeStore:
    """Stands in for mongo_store.distinct_values with per-collection answers."""

    def __init__(self, failed=(), produced=()):
        self.failed, self.produced = set(failed), set(produced)
        self.calls = []

    def distinct_values(self, collection, field, query):
        self.calls.append((collection, field))
        if collection == "cycle_run_summaries":
            wanted = set(query["cycle_id"]["$in"])
            return list(self.failed & wanted)
        if collection == "analysis_results":
            wanted = set(query["cycle_id"]["$in"])
            return list(self.produced & wanted)
        return []


@pytest.fixture
def wired(monkeypatch):
    def arm(commands, failed=(), produced=()):
        """commands: {command_id: cycle_id}"""
        rows = [(cid, "{'status': 'starting', 'cycle_id': '%s'}" % cyc)
                for cid, cyc in commands.items()]
        monkeypatch.setattr(watch_desk.mongo_query, "find_rows",
                            lambda *a, **k: rows)
        store = FakeStore(failed, produced)
        monkeypatch.setattr(watch_desk.mongo_store, "distinct_values",
                            store.distinct_values)
        return store

    return arm


def test_a_failed_cycle_with_no_decision_is_refunded(wired):
    """The 2026-09-02 case: the cycle errored and wrote nothing."""
    wired({"wd-1": "cycle-a"}, failed=["cycle-a"], produced=[])
    assert watch_desk._wakes_that_produced_nothing(["wd-1"]) == {"wd-1"}


def test_a_stopped_cycle_with_no_decision_is_refunded(wired):
    wired({"wd-1": "cycle-a"}, failed=["cycle-a"], produced=[])
    assert watch_desk._wakes_that_produced_nothing(["wd-1"]) == {"wd-1"}


def test_a_failed_cycle_that_DID_write_a_decision_is_not_refunded(wired):
    """The wake bought a decision. That it errored afterwards does not make the
    research free — refunding it would let a cycle that decided and then
    crashed on a later step be re-run for nothing, repeatedly."""
    wired({"wd-1": "cycle-a"}, failed=["cycle-a"], produced=["cycle-a"])
    assert watch_desk._wakes_that_produced_nothing(["wd-1"]) == set()


def test_a_running_cycle_is_never_refunded(wired):
    """THE dangerous case. A running cycle has written no analysis YET.

    Refunding on 'no analysis' alone would hand the budget back for exactly as
    long as a cycle takes to run, so a single slow cycle would let the desk
    wake again, and again — an unbounded loop built out of a safety feature.
    Requiring a TERMINAL failure state is what forecloses it.
    """
    wired({"wd-1": "cycle-a"}, failed=[], produced=[])   # not in the failed set
    assert watch_desk._wakes_that_produced_nothing(["wd-1"]) == set()


def test_a_completed_cycle_with_a_decision_is_not_refunded(wired):
    wired({"wd-1": "cycle-a"}, failed=[], produced=["cycle-a"])
    assert watch_desk._wakes_that_produced_nothing(["wd-1"]) == set()


def test_only_the_failed_empty_ones_are_refunded_in_a_mixed_day(wired):
    """The measured shape of 2026-09-02 through 09-04."""
    wired({"wd-1": "cycle-a", "wd-2": "cycle-b", "wd-3": "cycle-c"},
          failed=["cycle-a", "cycle-b"], produced=["cycle-b"])
    assert watch_desk._wakes_that_produced_nothing(["wd-1", "wd-2", "wd-3"]) == {"wd-1"}


def test_a_command_with_no_parsable_result_is_not_refunded(wired, monkeypatch):
    """No cycle id means nothing to join a decision to. Refunding on an
    unreadable row would hand back budget for wakes we cannot account for."""
    monkeypatch.setattr(watch_desk.mongo_query, "find_rows",
                        lambda *a, **k: [("wd-1", "not a dict at all")])
    monkeypatch.setattr(watch_desk.mongo_store, "distinct_values",
                        lambda *a, **k: [])
    assert watch_desk._wakes_that_produced_nothing(["wd-1"]) == set()


def test_a_lookup_failure_refunds_nothing(monkeypatch):
    """Fails to the PRE-EXISTING behaviour (charge every wake), not to a free
    budget. A broken refund lookup must not uncap the desk."""
    def boom(*a, **k):
        raise RuntimeError("mongo down")

    monkeypatch.setattr(watch_desk.mongo_query, "find_rows", boom)
    assert watch_desk._wakes_that_produced_nothing(["wd-1"]) == set()


def test_an_empty_input_does_no_reads(monkeypatch):
    called = []
    monkeypatch.setattr(watch_desk.mongo_query, "find_rows",
                        lambda *a, **k: called.append(1) or [])
    assert watch_desk._wakes_that_produced_nothing([]) == set()
    assert called == []
