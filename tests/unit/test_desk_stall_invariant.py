"""The sixth cycle invariant: no desk may be abandoned mid-pipeline.

WHY THESE TESTS LOOK LIKE THIS
------------------------------
The defect being detected is an ABSENCE — a desk that stopped at
`DEBATE_DONE`, leaving no `analysis_results` and no `trade_results` row. The
seven checks that existed could not see it, because they all keyed off
`analysis_results`: the table whose absence is the symptom.

The same trap applies to testing it. A fake database that returns a fixed row
list regardless of the query would test the reporting code and skip the part
that actually decides what counts as a stall — the `allowed` phase set the
check sends to SQL. That set is where the `INIT` exclusion lives, and the
`INIT` exclusion is the difference between a useful alert and one that fires
22 times a week on healthy triage skips until somebody mutes it.

So `_FakeDB` applies the check's OWN parameters to filter the roster, mimicking
`phase <> ALL(...)`. If the check stops excluding `INIT`, these tests fail.

Calibrated against 7 days of production (2026-07-30): PM_DONE 176 and INIT 22
are silent; DEBATE_DONE 5 and RESEARCH_DONE 1 fire. A live replay over 48
cycles fired on exactly the 3 that contained a stall. The live half is guarded
by an env var; the synthetic half never skips.

    python -m pytest tests/unit/test_desk_stall_invariant.py
    TRADING_BOT_LIVE_AUDIT=1 python -m pytest ... -k live
"""

from __future__ import annotations

from contextlib import contextmanager

import pytest

from app.v3 import invariants


# ── Test doubles ─────────────────────────────────────────────────────────


class _Result:
    def __init__(self, rows):
        self._rows = rows

    def fetchall(self):
        return self._rows

    def fetchone(self):
        return self._rows[0] if self._rows else None


class _FakeDB:
    """Applies the check's own params, so the phase filter is under test.

    `desks` is the full roster as (ticker, phase). `execute` reproduces
    `WHERE cycle_id = %s AND phase <> ALL(%s)` using the params the check
    actually passed — not a hardcoded expectation of them.
    """

    def __init__(self, desks, cycle_id="cycle-test-1"):
        self.desks = desks
        self.cycle_id = cycle_id
        self.params_seen = None

    def execute(self, sql, params=None):
        self.params_seen = params
        want_cycle, allowed = params[0], set(params[1])
        rows = [
            (t, p) for t, p in self.desks
            if want_cycle == self.cycle_id and p not in allowed
        ]
        return _Result(sorted(rows))


@pytest.fixture
def recorded(monkeypatch):
    """Capture violations instead of writing them to Postgres."""
    calls = []

    def _rec(kind, **detail):
        calls.append({"kind": kind, **detail})
        return kind

    monkeypatch.setattr(invariants, "record_violation", _rec)
    return calls


def _run(desks, recorded, monkeypatch, cycle_id="cycle-test-1"):
    db = _FakeDB(desks, cycle_id)

    @contextmanager
    def _get_db():
        yield db

    monkeypatch.setattr("app.db.connection.get_db", _get_db)
    out = invariants._check_desks_reached_terminal(cycle_id)
    return out, db


# ── The stalls it must catch ─────────────────────────────────────────────


@pytest.mark.parametrize("phase", ["DEBATE_DONE", "RESEARCH_DONE"])
def test_fires_on_a_desk_abandoned_mid_pipeline(phase, recorded, monkeypatch):
    """The HOOD case. Research was paid for and the desk went nowhere."""
    out, _ = _run([("HOOD", phase)], recorded, monkeypatch)

    assert out == [invariants.KIND_DESK_STALLED]
    assert len(recorded) == 1
    assert recorded[0]["count"] == 1
    assert recorded[0]["stalled"] == [{"ticker": "HOOD", "phase": phase}]


def test_a_single_stall_names_its_ticker(recorded, monkeypatch):
    """One stall must be queryable by ticker, not buried in a JSON blob."""
    _run([("HOOD", "DEBATE_DONE"), ("AAPL", "PM_DONE")], recorded, monkeypatch)

    assert recorded[0]["ticker"] == "HOOD"


def test_reports_only_the_stalled_desks_of_a_mixed_cycle(recorded, monkeypatch):
    """The real 07-27 cycle shape: 3 stalls beside healthy and skipped desks."""
    out, _ = _run([
        ("CARS", "RESEARCH_DONE"),   # stalled
        ("EXLS", "DEBATE_DONE"),     # stalled
        ("OWL", "DEBATE_DONE"),      # stalled
        ("AAPL", "PM_DONE"),         # healthy
        ("MSFT", "INIT"),            # triage skip
        ("TSLA", "ABORTED"),         # deliberate abort
    ], recorded, monkeypatch)

    assert out == [invariants.KIND_DESK_STALLED]
    assert recorded[0]["count"] == 3
    assert {s["ticker"] for s in recorded[0]["stalled"]} == {"CARS", "EXLS", "OWL"}
    # Ambiguous owner: the roster carries the tickers instead.
    assert recorded[0]["ticker"] == ""


# ── The silences it must keep (or it gets muted) ─────────────────────────


def test_silent_on_a_fully_completed_cycle(recorded, monkeypatch):
    out, _ = _run([("AAPL", "PM_DONE"), ("MSFT", "PM_DONE")], recorded, monkeypatch)

    assert out == []
    assert recorded == []


def test_silent_on_triage_skips(recorded, monkeypatch):
    """INIT is a legitimate Triage-Gate decline, not a stall.

    Production ran 22 of these in 7 days against 6 real stalls. Firing on them
    would make this check 79% false positives on week one.
    """
    out, _ = _run([("A", "INIT"), ("B", "INIT"), ("C", "INIT")],
                  recorded, monkeypatch)

    assert out == []
    assert recorded == []


def test_silent_on_deliberate_aborts(recorded, monkeypatch):
    """ABORTED is terminal — the desk stopped on purpose and said so."""
    out, _ = _run([("A", "ABORTED"), ("B", "PM_DONE")], recorded, monkeypatch)

    assert out == []


def test_silent_on_an_empty_cycle(recorded, monkeypatch):
    out, _ = _run([], recorded, monkeypatch)

    assert out == []


def test_scopes_to_the_cycle_it_was_asked_about(recorded, monkeypatch):
    """A stall in some other cycle is not this cycle's violation."""
    out, _ = _run([("HOOD", "DEBATE_DONE")], recorded, monkeypatch,
                  cycle_id="cycle-test-1")
    assert out == [invariants.KIND_DESK_STALLED]

    recorded.clear()
    db = _FakeDB([("HOOD", "DEBATE_DONE")], cycle_id="cycle-OTHER")

    @contextmanager
    def _get_db():
        yield db

    monkeypatch.setattr("app.db.connection.get_db", _get_db)
    assert invariants._check_desks_reached_terminal("cycle-test-1") == []


# ── Bounding a bad deploy ────────────────────────────────────────────────


def test_a_mass_stall_is_one_bounded_row(recorded, monkeypatch):
    """A deploy mid-cycle strands every live desk at once.

    One violation row per cycle with a capped roster, so a bad deploy cannot
    bury every other violation kind under hundreds of rows.
    """
    desks = [(f"T{i:03d}", "DEBATE_DONE") for i in range(80)]
    out, _ = _run(desks, recorded, monkeypatch)

    assert len(out) == 1
    assert len(recorded) == 1
    assert recorded[0]["count"] == 80, "the true count must survive the cap"
    assert len(recorded[0]["stalled"]) == invariants.STALL_ROSTER_CAP
    assert recorded[0]["truncated"] == 80 - invariants.STALL_ROSTER_CAP


def test_truncated_is_zero_when_nothing_was_dropped(recorded, monkeypatch):
    _run([("HOOD", "DEBATE_DONE")], recorded, monkeypatch)

    assert recorded[0]["truncated"] == 0


# ── The phase vocabulary must track the orchestrator ─────────────────────


def test_terminal_phases_come_from_the_real_transition_table():
    """Derived, not duplicated: a new DeskPhase cannot leave this stale."""
    from app.v3.shared_desk import _VALID_TRANSITIONS, DeskPhase

    terminal, skip = invariants._terminal_phases()

    assert terminal == frozenset(
        p.value for p, nxt in _VALID_TRANSITIONS.items() if not nxt
    )
    assert terminal == frozenset({"PM_DONE", "ABORTED"})
    assert skip == DeskPhase.INIT.value
    # Every phase is either allowed at rest or a stall — no third category.
    assert set(DeskPhase) - {DeskPhase(p) for p in terminal} - {DeskPhase(skip)} == {
        DeskPhase.RESEARCH_DONE, DeskPhase.DEBATE_DONE,
    }


def test_terminal_phases_falls_back_rather_than_raising(monkeypatch):
    """A check that raises here reports "no stalls" on every cycle forever."""
    import builtins

    real_import = builtins.__import__

    def _boom(name, *a, **kw):
        if name == "app.v3.shared_desk":
            raise ImportError("simulated")
        return real_import(name, *a, **kw)

    monkeypatch.setattr(builtins, "__import__", _boom)
    terminal, skip = invariants._terminal_phases()

    assert terminal == frozenset({"PM_DONE", "ABORTED"})
    assert skip == "INIT"


# ── Wiring ───────────────────────────────────────────────────────────────


def test_the_check_is_actually_registered(monkeypatch, recorded):
    """An unregistered check is a check that never runs.

    `check_cycle_complete` swallows per-check exceptions by design, so a
    missing registration is invisible — exactly the class of bug this module
    exists to catch.
    """
    seen = []
    monkeypatch.setattr(
        invariants, "_check_desks_reached_terminal",
        lambda cid: seen.append(cid) or ["sentinel"],
    )
    for name in ("_check_universe_coverage", "_check_tool_failure_rates",
                 "_check_decision_drift", "_check_agent_cost",
                 "_check_attribution"):
        monkeypatch.setattr(invariants, name, lambda cid: [])

    out = invariants.check_cycle_complete(cycle_id="cycle-test-1")

    assert seen == ["cycle-test-1"], "the stall check was not registered"
    assert "sentinel" in out


def test_a_probe_failure_is_not_a_violation(monkeypatch, recorded):
    """No database at all (the unit-test case) must stay silent, not fire."""
    @contextmanager
    def _boom():
        raise RuntimeError("no database")
        yield  # pragma: no cover

    monkeypatch.setattr("app.db.connection.get_db", _boom)

    assert invariants.check_cycle_complete(cycle_id="cycle-test-1") == []
    assert recorded == []


# ── The crash recorder: name the cause, do not mute the detector ─────────


def _crash(recorded, monkeypatch, error, *, desks=(("HOOD", "DEBATE_DONE"),),
           ticker="HOOD", cycle_id="cycle-test-1"):
    """Drive record_ticker_crash against a fake desk table, capturing writes."""
    class _DB:
        def __init__(self):
            self.statements = []

        def execute(self, sql, params=None):
            self.statements.append(sql)
            want_cycle, want_ticker = params[0], params[1]
            rows = [(p,) for t, p in desks
                    if t == want_ticker and want_cycle == cycle_id]
            return _Result(rows)

    db = _DB()

    @contextmanager
    def _get_db():
        yield db

    monkeypatch.setattr("app.db.connection.get_db", _get_db)
    out = invariants.record_ticker_crash(
        ticker=ticker, cycle_id=cycle_id, error=error,
    )
    return out, db


def test_records_the_exception_type_and_message(recorded, monkeypatch):
    out, _ = _crash(recorded, monkeypatch, RuntimeError("board leg exploded"))

    assert out == [invariants.KIND_DESK_ABANDONED]
    assert recorded[0]["error_type"] == "RuntimeError"
    assert recorded[0]["error"] == "board leg exploded"
    assert recorded[0]["phase_at_crash"] == "DEBATE_DONE"
    assert recorded[0]["ticker"] == "HOOD"


def test_a_timeout_is_still_identifiable(recorded, monkeypatch):
    """`asyncio.TimeoutError` stringifies to "" — the type is the only signal.

    Recording only str(error) would file the most likely cause of a stall as a
    blank.
    """
    import asyncio

    _crash(recorded, monkeypatch, asyncio.TimeoutError())

    assert recorded[0]["error"] == ""
    assert recorded[0]["error_type"] == "TimeoutError"


def test_records_no_desk_when_the_crash_beat_the_desk_row(recorded, monkeypatch):
    _crash(recorded, monkeypatch, RuntimeError("early"), desks=())

    assert recorded[0]["phase_at_crash"] == "NO_DESK"


def test_the_crash_recorder_never_writes_to_shared_desk(recorded, monkeypatch):
    """It must NOT stamp the desk terminal.

    Setting ABORTED would silence DESK_STALLED_MID_PIPELINE and hide the lost
    work behind a detector reporting health. The surviving phase is also the
    only record of where the pipeline stopped.
    """
    _out, db = _crash(recorded, monkeypatch, RuntimeError("boom"))

    joined = " ".join(db.statements).upper()
    assert "UPDATE" not in joined
    assert "INSERT" not in joined
    assert "DELETE" not in joined
    assert joined.count("SELECT") == 1


def test_a_crash_still_leaves_the_stall_check_firing(recorded, monkeypatch):
    """The two observers must be independent — neither mutes the other."""
    _crash(recorded, monkeypatch, RuntimeError("boom"))
    recorded.clear()

    out, _ = _run([("HOOD", "DEBATE_DONE")], recorded, monkeypatch)

    assert out == [invariants.KIND_DESK_STALLED]


@pytest.mark.parametrize("ticker,cycle", [("", "c"), ("T", ""), ("", "")])
def test_crash_recorder_needs_both_identifiers(ticker, cycle, recorded, monkeypatch):
    out = invariants.record_ticker_crash(
        ticker=ticker, cycle_id=cycle, error=RuntimeError("x"),
    )
    assert out == []
    assert recorded == []


def test_a_probe_failure_still_records_the_crash(recorded, monkeypatch):
    """The crash is the point; the phase is a nicety. Losing both would be worse."""
    @contextmanager
    def _boom():
        raise RuntimeError("no database")
        yield  # pragma: no cover

    monkeypatch.setattr("app.db.connection.get_db", _boom)
    out = invariants.record_ticker_crash(
        ticker="HOOD", cycle_id="cycle-test-1", error=ValueError("real cause"),
    )

    assert out == [invariants.KIND_DESK_ABANDONED]
    assert recorded[0]["phase_at_crash"] == "NO_DESK"
    assert recorded[0]["error_type"] == "ValueError"


def test_the_crash_recorder_is_wired_into_the_gather_loop():
    """Structural: the recorder is worthless if the crash path never calls it.

    The exception branch in pipeline_service is reached only when a real ticker
    pipeline raises inside `asyncio.gather`, which no unit test drives — so
    assert the call exists in the module that owns that branch.
    """
    import ast
    import inspect

    from app.services import pipeline_service

    tree = ast.parse(inspect.getsource(pipeline_service))
    called = {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert "record_ticker_crash" in called, (
        "pipeline_service never calls record_ticker_crash — a crashing ticker "
        "goes back to being log-only"
    )


# ── Live replay (the calibration this was built from) ────────────────────


def test_live_fires_only_on_cycles_that_contain_a_stall(live_db, monkeypatch):
    """Verify the detector FIRES, not merely that it is silent.

    Two of the previous five cycle checks passed a silence test and would have
    missed their own motivating defect. Over the 7 days to 2026-07-30 this
    fires on exactly the 3 cycles of 48 that hold a stalled desk.

    Takes `live_db` rather than importing `get_db`, because the autouse
    `patch_get_db` fixture would otherwise hand this test a MagicMock and every
    assertion below would pass against an empty result set.
    """
    monkeypatch.setattr(invariants, "record_violation", lambda kind, **d: kind)

    cycles = [r[0] for r in live_db.execute(
        "SELECT DISTINCT cycle_id FROM shared_desk "
        "WHERE created_at > NOW() - INTERVAL '7 days'"
    ).fetchall()]
    stalled = {r[0] for r in live_db.execute(
        "SELECT DISTINCT cycle_id FROM shared_desk "
        "WHERE created_at > NOW() - INTERVAL '7 days' "
        "AND phase IN ('RESEARCH_DONE','DEBATE_DONE')"
    ).fetchall()}

    # Guard first: an empty window would make every assertion below vacuous.
    assert cycles, "no cycles in window — this test proved nothing"
    assert stalled, "no stalls in window — this test proved nothing"

    fired = {c for c in cycles if invariants._check_desks_reached_terminal(c)}

    assert fired == stalled, f"expected {stalled}, fired on {fired}"
