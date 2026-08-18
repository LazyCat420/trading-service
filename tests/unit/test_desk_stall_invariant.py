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
check sends to the database. That set is where the `INIT` exclusion lives, and
the `INIT` exclusion is the difference between a useful alert and one that
fires 22 times a week on healthy triage skips until somebody mutes it.

So `_FakeDB` applies the check's OWN filter to the roster, mimicking the
`{"phase": {"$nin": [...]}}` it passes to `mongo_store.find_docs`. If the check
stops excluding `INIT`, these tests fail.

Calibrated against 7 days of production (2026-07-30): PM_DONE 176 and INIT 22
are silent; DEBATE_DONE 5 and RESEARCH_DONE 1 fire. A live replay over 48
cycles fired on exactly the 3 that contained a stall. The live half is guarded
by an env var; the synthetic half never skips.

    python -m pytest tests/unit/test_desk_stall_invariant.py
    TRADING_BOT_LIVE_AUDIT=1 python -m pytest ... -k live
"""

from __future__ import annotations

import pytest

from app.v3 import invariants


# ── Test doubles ─────────────────────────────────────────────────────────


class _FakeDB:
    """Applies the check's own filter, so the phase filter is under test.

    `desks` holds `(ticker, phase)` or
    `(ticker, phase, work_landed, decided, explained)` — the extra columns
    default to the HOOD shape (research lost, undecided, unexplained),
    which is what this check was calibrated on.

    The check reads `shared_desk` through `mongo_store.find_docs` with
    `{"cycle_id": ..., "phase": {"$nin": [...]}}`, and decides `work_landed` /
    `decided` with `count_docs` against `analysis_results` / `trade_results`.
    `find_docs` here reproduces that `$nin` using the filter the check
    ACTUALLY passed — not a hardcoded expectation of it — so dropping the
    `INIT` exclusion still fails these tests, exactly as it did when the check
    spoke SQL.
    """

    def __init__(self, desks, cycle_id="cycle-test-1"):
        self.desks = [tuple(d) + (False, False, False)[len(d) - 2:] for d in desks]
        self.cycle_id = cycle_id
        self.filter_seen = None
        self.collections_written = []

    # ── reads ────────────────────────────────────────────────────────
    def find_docs(self, collection, filt, **kwargs):
        assert collection == "shared_desk", collection
        self.filter_seen = filt
        want_cycle = filt["cycle_id"]
        allowed = set(filt["phase"]["$nin"])
        rows = sorted(
            d for d in self.desks
            if want_cycle == self.cycle_id and d[1] not in allowed
        )
        return [
            {
                "ticker": t,
                "phase": p,
                # `explained` is carried in desk_data, the shape the check reads.
                "desk_data": (
                    {"cycle_metadata": {"pipeline_incomplete": "stamped"}}
                    if explained else {}
                ),
            }
            for t, p, _landed, _decided, explained in rows
        ]

    def count_docs(self, collection, filt, **kwargs):
        ticker = filt.get("ticker")
        if filt.get("cycle_id") != self.cycle_id:
            return 0
        for t, _p, landed, decided, _e in self.desks:
            if t == ticker:
                if collection == "analysis_results":
                    return 1 if landed else 0
                if collection == "trade_results":
                    return 1 if decided else 0
        return 0

    # ── writes (must never happen on this path) ──────────────────────
    def _write(self, collection, *a, **k):
        self.collections_written.append(collection)
        return None


@pytest.fixture
def recorded(monkeypatch):
    """Capture violations instead of writing them to Postgres."""
    calls = []

    def _rec(kind, **detail):
        calls.append({"kind": kind, **detail})
        return kind

    monkeypatch.setattr(invariants, "record_violation", _rec)
    return calls


def _install(db, monkeypatch):
    """Point the Mongo layer at `db`.

    The check imports `app.db.mongo_store` INSIDE the function, so setting an
    attribute on `invariants` would be a silent no-op — the module attributes
    themselves have to be replaced.
    """
    from app.db import mongo_store

    monkeypatch.setattr(mongo_store, "find_docs", db.find_docs)
    monkeypatch.setattr(mongo_store, "count_docs", db.count_docs)
    for name in ("insert_docs", "upsert_doc", "update_docs", "delete_docs",
                 "find_one_and_update"):
        monkeypatch.setattr(
            mongo_store, name,
            (lambda n: lambda *a, **k: db._write(n, *a, **k))(name),
        )


def _run(desks, recorded, monkeypatch, cycle_id="cycle-test-1"):
    db = _FakeDB(desks, cycle_id)
    _install(db, monkeypatch)
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
    assert recorded[0]["lost_research"] == 1
    assert recorded[0]["stalled"] == [
        {"ticker": "HOOD", "phase": phase, "work_landed": False,
         "decided": False, "explained": False}
    ]


# ── The two shapes must stay distinguishable ─────────────────────────────


def test_a_stall_that_kept_its_research_but_lost_its_decision(recorded, monkeypatch):
    """The live NVDA firing (2026-07-30 07:28) — and it was NOT benign.

    Its analysis and 7 telemetry rows survived, and it carried a
    `pipeline_incomplete` stamp that looked like the ORCL fix working as
    designed. The real cause was a same-day regression (`6a9bd82`): the
    DEBATE_ENGINE=3 branch skipped the `tournament_result` write, which is the
    Board's chain trigger, so no decision was ever produced.

    Counting this as "nothing lost" is what let a live regression read as
    healthy. The decision IS the product.
    """
    out, _ = _run([("NVDA", "RESEARCH_DONE", True, False, True)],
                  recorded, monkeypatch)

    assert out == [invariants.KIND_DESK_STALLED]
    assert recorded[0]["lost_research"] == 0, "the analysis did land"
    assert recorded[0]["undecided"] == 1, "but no decision was produced"
    assert recorded[0]["undecided_tickers"] == ["NVDA"]
    assert recorded[0]["stalled"][0]["decided"] is False


def test_a_stall_that_kept_both_is_neither(recorded, monkeypatch):
    """Research landed AND a decision exists — surfaced, but nothing lost."""
    _run([("CRH", "DEBATE_DONE", True, True, False)], recorded, monkeypatch)

    assert recorded[0]["lost_research"] == 0
    assert recorded[0]["undecided"] == 0


def test_the_two_losses_are_reported_separately(recorded, monkeypatch):
    """One number cannot carry both; flattening hid a live regression."""
    out, _ = _run([
        ("HOOD", "DEBATE_DONE", False, False, False),   # research lost
        ("NVDA", "RESEARCH_DONE", True, False, True),   # kept research, no decision
        ("CRH", "DEBATE_DONE", True, True, False),      # kept both
    ], recorded, monkeypatch)

    assert recorded[0]["count"] == 3
    assert recorded[0]["lost_research"] == 1
    assert recorded[0]["lost_research_tickers"] == ["HOOD"]
    assert recorded[0]["undecided"] == 1
    assert recorded[0]["undecided_tickers"] == ["NVDA"]


def test_an_explained_stall_with_no_research_is_still_lost(recorded, monkeypatch):
    """A `pipeline_incomplete` stamp explains the PHASE, never the outcome.

    NVDA proved the point: it was stamped, and it was still a regression.
    Treating "explained" as "fine" would re-hide the HOOD case the moment the
    ORCL handler starts stamping it.
    """
    _run([("HOOD", "DEBATE_DONE", False, False, True)], recorded, monkeypatch)

    assert recorded[0]["lost_research"] == 1


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
    _install(db, monkeypatch)
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
    from app.db import mongo_query, mongo_store

    def _boom(*a, **k):
        raise RuntimeError("no database")

    # The probe is Mongo now; patching `connection.get_db` here intercepted
    # nothing and left the whole test vacuous.
    for name in ("find_docs", "count_docs", "aggregate"):
        monkeypatch.setattr(mongo_store, name, _boom)
    for name in ("find_row", "find_rows", "scalar", "count"):
        monkeypatch.setattr(mongo_query, name, _boom)

    assert invariants.check_cycle_complete(cycle_id="cycle-test-1") == []
    assert recorded == []


# ── The crash recorder: name the cause, do not mute the detector ─────────


def _crash(recorded, monkeypatch, error, *, desks=(("HOOD", "DEBATE_DONE"),),
           ticker="HOOD", cycle_id="cycle-test-1"):
    """Drive record_ticker_crash against a fake desk table, capturing writes."""
    class _DB:
        def __init__(self):
            self.reads = []       # (collection, filter) of every read
            self.writes = []      # (helper, collection) of every write

        def find_row(self, collection, filt, columns, **kwargs):
            self.reads.append((collection, filt))
            assert columns == ["phase"], columns
            rows = [(p,) for t, p in desks
                    if t == filt.get("ticker") and filt.get("cycle_id") == cycle_id]
            return rows[0] if rows else None

        def write(self, helper, collection, *a, **k):
            self.writes.append((helper, collection))
            return None

    db = _DB()

    from app.db import mongo_query, mongo_store

    # Both halves are patched: stubbing only the read would leave any write the
    # recorder makes pointed at the real store.
    monkeypatch.setattr(mongo_query, "find_row", db.find_row)
    for name in ("insert_docs", "upsert_doc", "update_docs", "delete_docs",
                 "find_one_and_update"):
        monkeypatch.setattr(
            mongo_store, name,
            (lambda n: lambda coll, *a, **k: db.write(n, coll, *a, **k))(name),
        )

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

    # No write helper of any kind was reached — not an upsert, not an update,
    # not a delete, against `shared_desk` or anything else.
    assert db.writes == []
    # And the desk was only READ, exactly once, for its phase.
    assert db.reads == [("shared_desk", {"cycle_id": "cycle-test-1",
                                         "ticker": "HOOD"})]


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
    from app.db import mongo_query

    def _boom(*a, **k):
        raise RuntimeError("no database")

    monkeypatch.setattr(mongo_query, "find_row", _boom)
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
