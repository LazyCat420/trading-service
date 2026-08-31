"""`poll_pipeline_state.py` must wait on the store the pipeline WRITES.

WHAT THE SCRIPT IS FOR
----------------------
It blocks until `pipeline_state` leaves a baseline and reports the answer as an
exit code: 0 STATE_CHANGED, 2 POLL_TIMEOUT, 3 POLL_UNREADABLE. That contract is
the whole reason it exists beside `check_pipeline_state.py`, which prints the
state once and always exits 0.

Until the 2026-08-30 port the loop read the SQL archive, where `pipeline_state`
froze at the 2026-08-19 cutover. Re-measured 2026-08-30, the singleton in each
store:

    archive  cycle-v3-1787179210  analyzing  done   updated_at 2026-08-19 22:55:05.087554
    mongo    cycle-v3-1788074145  analyzing  done   updated_at 2026-08-30 07:21:56.598000

A row that cannot change is a watch that can only lie: given a live baseline it
returned STATE_CHANGED at once naming an eleven-day-old cycle, and given the
archive's own values it sat out the full window and exited 2 however the
pipeline actually moved. Both halves were reproduced by running the pre-port
file — it CONNECTS today (the pool fault was fixed in 94c6602, an ancestor of
this commit) and answers with the frozen row, which is worse than an error.

WHAT THIS FILE HAD TO LEARN
---------------------------
The first version of this file passed 9/9 against a port that was correct, and
also passed 9/9 against four mutants of it, one of which inverted the script's
answer on its commonest real use. Every test below therefore names the mutant
it kills. Baseline for the table: unmutated = all green; each mutant was loaded
in place of `scripts.poll_pipeline_state` through a plugin that rebinds
`sys.modules`, leaving the worktree untouched.

    m5   `state != baseline` -> `state[0] != baseline[0]`
         killed by test_a_change_in_one_field_alone_is_a_state_change[phase]
         and [status].  Live proof it matters: baseline
         (cycle-v3-1788074145, collecting, done) against the real singleton
         gave rc=0 STATE_CHANGED from the shipped script and rc=2 POLL_TIMEOUT
         from the mutant.
    m6   `_text` loses its `.strip()`
         killed by test_whitespace_around_a_value_is_not_a_state_change
    m17  `if reads == 0 and failures:` -> `if failures:`
         killed by test_one_blip_in_a_readable_window_is_still_a_timeout
    m3   `TABLE = collection_for("pipeline_state")` (a real double resolution)
         killed by test_the_table_constant_is_a_plain_string_literal and
         test_the_table_name_is_not_resolved_at_import_time
    m19  phase/status swapped in the STATE_CHANGED line
         killed by test_the_watch_reads_pipeline_state_by_its_table_name

WHY EACH TEST HERE WOULD HAVE BEEN RED BEFORE THE PORT
------------------------------------------------------
Read `git show 77e6dc3:scripts/poll_pipeline_state.py` alongside this list; every
claim below is a property of that file, not a recollection.

  test_the_script_has_no_postgres_coupling
        the pre-port file's line 20 was
        `from scripts.migration.pg_connection import get_db`, with a second
        copy in the ImportError fallback at line 24.
  test_the_detector_sees_a_planted_coupling
        the negative control for the test above: it runs the same detector
        over the pre-port import and requires a hit, so a detector that
        stopped matching cannot pass the file by default.
  test_the_old_store_is_not_named_anywhere_in_the_file
        the brief's literal rule -- `grep -nE "psycopg|DATABASE_URL|
        pg_connection|dbname=|postgres"` over the file must be empty. This was
        RED against the first ported draft too, which carried 4 hits (7 with
        -i) in its own docstring, and the prose it carried there was itself
        wrong. The file still explains what it left; it does so in words that
        are not the driver's.
  test_the_watch_reads_pipeline_state_by_its_table_name
        the pre-port loop never touched `mongo_query`, so the stub below
        records ZERO calls; it opened a pooled SQL cursor instead.
  test_a_change_in_one_field_alone_is_a_state_change
        the pre-port loop compared all three fields and so would PASS the
        phase-only and status-only cases -- but not the fixture: it never
        calls `mongo_query`, and `poll_state` raised SystemExit instead of
        returning, so `rc = pps.poll_state(...)` never binds. This test is
        aimed at the port's regressions, not the archive's.
  test_a_state_that_matches_the_baseline_times_out
        red before for two independent reasons: pre-port `poll_state` ends in
        `sys.exit(2)`, so it raises SystemExit rather than returning 2; and
        with the LIVE baseline the archive's frozen row DIFFERS, so it would
        have taken the `sys.exit(0)` branch on the first read instead.
  test_whitespace_around_a_value_is_not_a_state_change
        pre-port this held, via `(v or "").strip()` on both the flags and the
        row; it is here because nothing in the ported file held it.
  test_an_unreadable_store_is_not_reported_as_no_change
        the pre-port file has no exit code 3 anywhere: its `except` printed
        `Error polling database:` and fell through to the same `sys.exit(2)`
        a quiet window took. A watch that never read the store reported
        "nothing changed".
  test_one_blip_in_a_readable_window_is_still_a_timeout
        the other edge of that split. Exit 3 is a WIDENING of an interface
        whose exit codes are the whole interface, so its boundary -- not one
        failure, but no successful read at all -- is pinned from both sides.
  test_a_missing_document_is_an_absence_not_a_failure
        pins the third direction, so the fix above cannot be "call everything
        unreadable".
  test_the_final_sleep_cannot_overshoot_the_window
        pre-port the sleep was a hard-coded `time.sleep(10)`, so
        `--timeout 12` returned at 20s. The port caps it; nothing held it
        there.
  test_a_non_string_state_value_does_not_break_the_watch
        the archive typed these columns TEXT and Mongo types nothing; the old
        `(row[0] or "").strip()` raised AttributeError on an int, which the
        `except` swallowed into the poll-error path -- the transition would
        have been missed for the entire window.
  test_a_null_field_compares_equal_to_an_empty_baseline
        an idle row must not read as a transition off the default empty flags.
  test_the_table_constant_is_a_plain_string_literal
  test_the_table_name_is_not_resolved_at_import_time
        pre-port there was no TABLE and no resolver. They are here because
        tests/unit/test_no_double_collection_resolution.py cannot see a double
        resolution bound to a module constant -- its AST scan matches the
        resolver only as a DIRECT argument of a `mongo_store`/`mongo_query`
        call -- and because `collection_for` is the identity today, so a
        runtime `== "pipeline_state"` proves nothing. The second test imports
        the file again with a RENAMING `collection_for` in place, which is the
        only form that fails while renames are off.
"""
from __future__ import annotations

import ast
import importlib.util
import pathlib
import re

import pytest

import scripts.poll_pipeline_state as pps


# ── the static half ────────────────────────────────────────────────────────

_PG = re.compile(r"psycopg|DATABASE_URL|pg_connection|dbname=|postgres", re.I)


def _strip_prose(src: str) -> str:
    """Drop comments and docstrings, the way `pg_script_inventory` does.

    Used by the CODE-level detector below. The file-level rule
    (`test_the_old_store_is_not_named_anywhere_in_the_file`) deliberately does
    NOT strip prose: a false sentence about the old store is exactly what this
    port shipped the first time, and the cheapest way to keep it out is to
    forbid the vocabulary.
    """
    tree = ast.parse(src)
    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                             ast.AsyncFunctionDef)):
            doc = ast.get_docstring(node, clean=False)
            if doc:
                docstrings.add(doc)
    out = "\n".join(line.split("#", 1)[0] for line in src.splitlines())
    for doc in docstrings:
        out = out.replace(doc, "")
    return out


def _couplings(src: str) -> list[str]:
    """Postgres reached in CODE: an import of a driver/pool, or a DSN name."""
    tree = ast.parse(src)
    hits = []
    for node in ast.walk(tree):
        names = []
        if isinstance(node, ast.Import):
            names = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module:
            names = [node.module]
        for name in names:
            if name.startswith("psycopg") or "pg_connection" in name:
                hits.append(f"line {node.lineno}: imports {name}")
    for i, line in enumerate(_strip_prose(src).splitlines(), start=1):
        if _PG.search(line):
            hits.append(f"line {i}: {line.strip()}")
    return hits


def _source() -> str:
    """The source of the module under test — the mutation harness can point
    `scripts.poll_pipeline_state` at a different file, and this must follow it
    rather than re-deriving the path from the repo layout."""
    return pathlib.Path(pps.__file__).read_text(encoding="utf-8")


def test_the_script_has_no_postgres_coupling():
    assert _couplings(_source()) == []


def test_the_detector_sees_a_planted_coupling():
    """NEGATIVE CONTROL. The pre-port import, verbatim."""
    planted = (
        '"""Docstring naming psycopg and DATABASE_URL, which is prose."""\n'
        "import time\n"
        "from scripts.migration.pg_connection import get_db\n"
        "def f():\n"
        "    with get_db() as db:\n"
        "        db.execute('SELECT cycle_id FROM pipeline_state')\n"
    )
    hits = _couplings(planted)
    assert any("pg_connection" in h for h in hits), hits


def test_prose_about_the_old_store_is_not_a_coupling():
    """The CODE detector must not fire on an explanation."""
    assert _couplings(
        '"""It used to read postgres through pg_connection/DATABASE_URL."""\n'
        "# psycopg is gone\n"
        "x = 1\n"
    ) == []


def test_the_old_store_is_not_named_anywhere_in_the_file():
    """The brief's rule 3, applied literally, prose included.

    `grep -nE "psycopg|DATABASE_URL|pg_connection|dbname=|postgres" <file>`
    must come back empty. The first ported draft failed this with 4 hits (7
    case-insensitively), all in its module docstring, and two of those lines
    asserted something that had stopped being true — the file could not have
    been trusted to say WHY it moved while it was also wrong about it.
    """
    hits = [f"line {i}: {ln.strip()}"
            for i, ln in enumerate(_source().splitlines(), start=1)
            if _PG.search(ln)]
    assert hits == [], hits


def test_the_table_constant_is_a_plain_string_literal():
    """`TABLE` must be the SQL table name, not a resolver's result.

    Kills mutant m3. `mongo_query` resolves the table internally, exactly
    once; `TABLE = collection_for("pipeline_state")` resolves it twice, and
    tests/unit/test_no_double_collection_resolution.py cannot see that form
    because it matches the resolver only as a direct call argument.
    """
    assigned = [node.value
                for node in ast.walk(ast.parse(_source()))
                if isinstance(node, ast.Assign)
                for t in node.targets
                if isinstance(t, ast.Name) and t.id == "TABLE"]

    assert len(assigned) == 1, f"expected one TABLE assignment, got {len(assigned)}"
    value = assigned[0]
    assert isinstance(value, ast.Constant) and value.value == "pipeline_state", (
        "TABLE is computed rather than written out; a resolved collection name "
        f"here would be resolved a second time inside mongo_query: {ast.dump(value)}"
    )


def test_the_table_name_is_not_resolved_at_import_time():
    """The behavioural half of the test above, with renames switched ON.

    `collection_for` is the identity today, so asserting `TABLE ==
    "pipeline_state"` at runtime passes for a file that resolves it. This
    re-imports the module under test with a RENAMING resolver installed: a
    file that calls it produces `renamed_pipeline_state` and fails here, while
    the shipped literal is unaffected.
    """
    from app.db import collections as collections_mod

    original = collections_mod.collection_for
    collections_mod.collection_for = lambda table: f"renamed_{table}"
    try:
        spec = importlib.util.spec_from_file_location(
            "_poll_pipeline_state_under_renames", pps.__file__)
        fresh = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(fresh)
    finally:
        collections_mod.collection_for = original

    assert fresh.TABLE == "pipeline_state", (
        "the table name was resolved through collection_for at import time; "
        "mongo_query would then resolve it a second time")


# ── the behavioural half ───────────────────────────────────────────────────

LIVE = ("cycle-v3-1788074145", "analyzing", "done")
FROZEN = ("cycle-v3-1787179210", "analyzing", "done")   # the SQL archive


@pytest.fixture
def store(monkeypatch):
    """A stand-in `mongo_query.find_row`, recording how it was called.

    `block_production_mongo` (autouse, tests/conftest.py) already fails a test
    that reaches the real client; this replaces the read so the watch can be
    driven deterministically.

    `raises_once` is a queue of exceptions for the FIRST reads only, so a
    transient blip can be told apart from a store that is down.
    """
    state = {"row": LIVE, "raises": None, "raises_once": [], "calls": []}

    def find_row(collection, query, columns, sort=None):
        state["calls"].append((collection, query, tuple(columns), sort))
        if state["raises_once"]:
            raise state["raises_once"].pop(0)
        if state["raises"] is not None:
            raise state["raises"]
        return state["row"]

    monkeypatch.setattr(pps.mongo_query, "find_row", find_row)
    return state


@pytest.fixture
def clock(monkeypatch):
    """A fake `time` for the module under test: only a sleep advances it.

    Patched on `pps`, not on the stdlib module, so nothing else in the process
    sees it. It makes the two timing-shaped tests exact instead of racing a
    loaded box — `a probe whose number does not move is about the probe`.
    """
    class _Clock:
        def __init__(self):
            self.t = 0.0
            self.sleeps: list[float] = []

        def monotonic(self):
            return self.t

        def sleep(self, seconds):
            self.sleeps.append(seconds)
            self.t += seconds

    fake = _Clock()
    monkeypatch.setattr(pps, "time", fake)
    return fake


def test_the_watch_reads_pipeline_state_by_its_table_name(store, capsys):
    """The read goes to the live collection, named as a SQL TABLE.

    `mongo_query` resolves the table exactly once, internally; handing it
    `collection_for("pipeline_state")` would resolve it twice and, the day
    renames are switched on, read a collection that does not exist. See
    tests/unit/test_no_double_collection_resolution.py.
    """
    rc = pps.poll_state(*FROZEN, timeout_seconds=5, interval=0)

    assert rc == 0
    assert store["calls"], "the watch never read Mongo"
    collection, query, columns, _sort = store["calls"][0]
    assert collection == "pipeline_state"
    assert query == {"singleton_id": "current"}
    assert columns == ("cycle_id", "phase", "status")
    out = capsys.readouterr().out
    assert "STATE_CHANGED: cycle_id='cycle-v3-1788074145', " \
           "phase='analyzing', status='done'" in out


@pytest.mark.parametrize("field,index,moved", [
    ("cycle_id", 0, "cycle-v3-1787179210"),
    ("phase", 1, "collecting"),
    ("status", 2, "running"),
])
def test_a_change_in_one_field_alone_is_a_state_change(store, capsys,
                                                       field, index, moved):
    """All THREE fields, separately. This is the script's entire contract.

    The SQL fired on `cycle_id != ... or phase != ... or status != ...`, and
    the commonest real wait moves exactly one of them ("block until this cycle
    leaves `collecting`"). Kills mutant m5: narrowing the compare to
    `state[0] != baseline[0]` left all nine of the earlier tests green, because
    every behavioural case there either differed in `cycle_id` or was equal in
    all three — yet against the live singleton it turned a real phase-only
    transition from rc=0 into rc=2 POLL_TIMEOUT.
    """
    baseline = list(LIVE)
    baseline[index] = moved

    rc = pps.poll_state(*baseline, timeout_seconds=5, interval=0)

    assert rc == 0, f"a {field}-only transition was not seen as a change"
    out = capsys.readouterr().out
    assert "STATE_CHANGED: cycle_id='cycle-v3-1788074145', " \
           "phase='analyzing', status='done'" in out


def test_a_state_that_matches_the_baseline_times_out(store, capsys):
    rc = pps.poll_state(*LIVE, timeout_seconds=0.05, interval=0.02)

    assert rc == 2
    out = capsys.readouterr().out
    assert "POLL_TIMEOUT: No state change detected within 0.05 seconds." in out
    assert "STATE_CHANGED" not in out


def test_whitespace_around_a_value_is_not_a_state_change(store, capsys):
    """`--phase ' analyzing '` is the same state as a stored `'analyzing'`.

    Kills mutant m6. The pre-port code stripped BOTH the baseline flags and
    every row value; `_text` is now the only place that happens, on both
    sides, so dropping its `.strip()` silently turns a shell that quoted its
    arguments loosely into a watch that reports a transition immediately.
    """
    store["row"] = ("  cycle-v3-1788074145\n", "analyzing  ", " done")

    rc = pps.poll_state(" cycle-v3-1788074145 ", "  analyzing", "done\t",
                        timeout_seconds=0.05, interval=0.02)

    assert rc == 2, "whitespace alone was reported as a state change"
    assert "POLL_TIMEOUT" in capsys.readouterr().out


def test_an_unreadable_store_is_not_reported_as_no_change(store, capsys):
    """A read that never happened has not observed "no change".

    Same distinction `app/services/pipeline_state.py` draws between `idle` (a
    real answer) and `unknown` (a read that failed) — collapsing them is what
    lets a fault look like a quiet pipeline. The pre-port file collapsed them:
    it had no third exit code, so every read raising still ended in the same
    `sys.exit(2)` a genuinely quiet window took.
    """
    store["raises"] = RuntimeError("mongo is down")

    rc = pps.poll_state(*LIVE, timeout_seconds=0.05, interval=0.02)

    captured = capsys.readouterr()
    assert rc == 3, "an unreadable store came back as a clean timeout"
    assert "POLL_UNREADABLE" in captured.out
    assert "POLL_TIMEOUT" not in captured.out
    assert "Error polling database: mongo is down" in captured.err


def test_one_blip_in_a_readable_window_is_still_a_timeout(store, clock, capsys):
    """The OTHER edge of exit 3: `reads == 0`, not `failures > 0`.

    Kills mutant m17. Relaxing the guard to `if failures:` leaves every other
    test green — the unreadable test above fails EVERY read, so it cannot see
    the boundary — while a single transient blip in an otherwise clean 900s
    window would start returning the new code 3 to a caller that has a
    perfectly good answer. Exit 3 widens this script's only interface; the
    widening has to stop exactly where it was argued to stop.
    """
    store["raises_once"] = [RuntimeError("transient blip")]

    rc = pps.poll_state(*LIVE, timeout_seconds=1.0, interval=0.25)

    captured = capsys.readouterr()
    assert len(store["calls"]) > 1, "the watch gave up after the blip"
    assert rc == 2, "one blip in a window that WAS read came back as unreadable"
    assert "POLL_TIMEOUT" in captured.out
    assert "POLL_UNREADABLE" not in captured.out
    assert "Error polling database: transient blip" in captured.err


def test_a_missing_document_is_an_absence_not_a_failure(store, capsys):
    """No `current` document means the pipeline has never written state.

    That is a real answer — nothing has changed — and must stay exit 2, or the
    fix above degenerates into calling every quiet window unreadable.
    """
    store["row"] = None

    rc = pps.poll_state(*LIVE, timeout_seconds=0.05, interval=0.02)

    assert rc == 2
    out = capsys.readouterr().out
    assert "POLL_TIMEOUT" in out
    assert "POLL_UNREADABLE" not in out


def test_the_final_sleep_cannot_overshoot_the_window(store, clock, capsys):
    """`--timeout 12` returns at 12s, not at the end of a full 10s sleep.

    Pre-port the sleep was a hard-coded `time.sleep(10)` with no reference to
    the deadline, so a 12s window ran 20s. The port caps the last sleep at the
    time remaining; with the fake clock the sleeps are exact rather than
    approximately timed on a loaded box.
    """
    rc = pps.poll_state(*LIVE, timeout_seconds=12, interval=10)

    assert rc == 2
    assert clock.sleeps == [10, 2], (
        f"the last sleep was not capped at the remaining window: {clock.sleeps}")
    assert clock.t == 12
    assert len(store["calls"]) == 2, "a poll instant was lost to the cap"
    assert "POLL_TIMEOUT: No state change detected within 12 seconds." in \
        capsys.readouterr().out


def test_a_non_string_state_value_does_not_break_the_watch(store, capsys):
    """Mongo has no column types; the watch must survive one that is not TEXT."""
    store["row"] = (1788074145, "analyzing", "done")

    rc = pps.poll_state(*FROZEN, timeout_seconds=5, interval=0)

    assert rc == 0
    captured = capsys.readouterr()
    assert "STATE_CHANGED: cycle_id='1788074145'" in captured.out
    assert "Error polling database" not in captured.err


def test_a_null_field_compares_equal_to_an_empty_baseline(store, capsys):
    """`(NULL, '', '')` is what an idle row looked like in the archive, and the
    default flags are empty strings — so an idle pipeline must NOT read as a
    transition. `None` and a missing field both flatten to ''."""
    store["row"] = (None, "", "")

    rc = pps.poll_state("", "", "", timeout_seconds=0.05, interval=0.02)

    assert rc == 2, "an idle state was reported as a change off the empty baseline"
    assert "POLL_TIMEOUT" in capsys.readouterr().out
