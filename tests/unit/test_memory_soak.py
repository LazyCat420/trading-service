"""The memory soak instrument measures something, and says so when it does not.

WHAT WAS WRONG WITH THE THING THIS GUARDS
-----------------------------------------
`scripts/stress_tests/memory_soak.py` imported `execute_v2_pipeline` from
`app.cognition.orchestration.runner`, a module deleted on 2026-06-26 by commit
834c894. Every one of these tests except the negative controls fails on that
version at COLLECTION time -- the import raises ModuleNotFoundError -- which is
the plainest possible statement of the problem: the instrument that was
supposed to grade the 2026-08-19 cutover had not been able to start for two
months before the cutover happened.

THE TWO FAILURES A SOAK HAS
---------------------------
A soak reports a leak that is not there, or it reports health it did not
measure. The second is the dangerous one and the old version had four routes
to it, each pinned below:

  * the workload reads nothing, so nothing allocates    -> an empty probe
  * the workload raises every time, and the loop logs and continues, and the
    run still ends with "completed" and True             -> a raising battery
  * a leak IS detected, and the process exits 0 anyway   -> the exit code
  * the run is asked for zero iterations and reports success without opening
    a connection                                         -> `--iterations 0`

Plus the instrument's own instrument: `_rss_mb` is checked against a real
allocation, because a memory probe whose number never moves reports "no leak"
forever and looks exactly like a clean run.

WHY THE FIRST VERSION OF THIS FILE WAS NOT ENOUGH
-------------------------------------------------
Every test above monkeypatches `_read_battery` away, so all of them passed
against a battery that had been short-circuited to

    if True:
        return [("find_rows/pipeline_events", 200), ("count/news_articles", 1)], 310

-- a battery that reads Mongo NOT AT ALL and fabricates a plausible non-empty
result. So did restoring the historical `severity="ERROR"` filter that matched
nothing, dropping a filter, swapping a collection, flipping a sort and removing
a limit: 12 of 12 workload mutants survived, i.e. the entire ported workload
was unpinned by the suite that shipped with it.

`TestTheBatteryIsTheWorkloadItDocuments` is the answer, and it runs OFFLINE:
`mongo_query` is replaced by a recorder that binds each call against the REAL
seam signature, records the bound arguments, and hands back canned rows of
deliberately odd sizes (7, 5, 11, 3, 4, 13). A short-circuit records no calls;
a changed filter, collection, sort, limit or aggregate list records different
arguments; a hardcoded probe value disagrees with the canned sizes. None of it
needs a database, so it fires on every run.

What that CANNOT tell you is whether the pinned filters match anything in the
real store -- an offline test cannot tell a filter that runs from a filter that
is right. `TestAgainstTheLiveStore` does that half, read-only against
production through the `live_mongo` fixture, opt-in with
`TRADING_BOT_LIVE_AUDIT=1` (the repo convention; the fixture patches every
`mongo_store` writer to raise before it hands the connection over). Run it:

    TRADING_BOT_LIVE_AUDIT=1 .venv/bin/python -m pytest tests/unit/test_memory_soak.py

Recorded there on 2026-08-30: 200 / 100 / 36,020 / 326 / 10 / 81,970 / 1 / 1 /
1 with 312 documents materialized.
"""
from __future__ import annotations

import ast
import asyncio
import inspect
import pathlib

import pytest

from app.db import mongo_query as real_seam
from app.db import mongo_store as real_store

REPO = pathlib.Path(__file__).resolve().parents[2]
SOAK = REPO / "scripts" / "stress_tests" / "memory_soak.py"
SOURCE = SOAK.read_text(encoding="utf-8")

from scripts.stress_tests import memory_soak  # noqa: E402


# ── the port itself ──────────────────────────────────────────────────

def test_the_instrument_imports():
    """RED before the port: ModuleNotFoundError on app.cognition.orchestration."""
    assert callable(memory_soak.memory_soak_test)
    assert callable(memory_soak._read_battery)


def test_the_instrument_has_no_archive_coupling():
    """RED before the port: the archive connection import.

    Uses the repo's own AST scanner as well as the grep, because the two
    disagree by construction: the scanner reads code, the grep reads bytes.
    """
    from scripts.gate_zero_pg import scan

    result = scan(REPO, targets=("scripts/stress_tests/memory_soak.py",))
    assert result["errors"] == [], result["errors"]
    assert result["total"] == 0, result["findings"]


def test_the_instrument_left_the_archive_inventory():
    """The same claim through the other scanner, which strips prose differently.

    `classify` returning None is what "this file can no longer reach the
    archive" means to `docs/migration/pg_script_inventory.json`.
    """
    from scripts.pg_script_inventory import classify

    assert classify(SOAK) is None


#: The port check for this migration, verbatim, plus `-i`. Kept as data so the
#: assertion below cannot drift from the rule it enforces.
_ARCHIVE_TOKENS = ("psycopg", "DATABASE_URL", "pg_connection", "dbname=",
                   "postgres")


def test_the_file_spells_none_of_the_archive_tokens():
    """RED before this revision: three hits, all docstring prose.

    The rule is `grep -nE "psycopg|DATABASE_URL|pg_connection|dbname=|postgres"`
    comes back empty, and prose explaining a removed coupling is not an
    exemption -- it is a live hazard. `_strip_prose` in
    `scripts/pg_script_inventory.py` deletes line comments BEFORE it deletes
    docstrings by verbatim replace, so a single hash character anywhere in the
    module docstring truncates the line the replace is searching for, the
    replace no-ops, and the entire docstring is handed to the classifier as
    code. The previous revision named the DSN setting inside that docstring and
    guarded the hash with a test, which makes the trap survivable rather than
    absent. Checked case-insensitively so "Postgres" in prose cannot creep back
    in under a capital letter and re-arm it.
    """
    lower = SOURCE.lower()
    hits = sorted({t for t in _ARCHIVE_TOKENS if t.lower() in lower})
    assert not hits, (
        f"{hits} appear in the file. The strip-prose hazard means a docstring "
        "mention is not safe: say what was removed without spelling it.")


def test_the_docstring_carries_no_hash():
    """Belt to the braces above: with no token to re-expose, a hash in the
    docstring is harmless today, but the next paragraph somebody adds is not
    guaranteed to be. Cheap, so it stays."""
    doc = ast.get_docstring(ast.parse(SOURCE), clean=False)
    assert doc and "#" not in doc


# ── the workload is a READ-ONLY workload ─────────────────────────────

#: Both halves of the seam. A soak loops on production 100 times; a writer
#: reaching this file would run its write 100 times.
_SEAM_MODULES = {"app.db.mongo_query", "app.db.mongo_store"}

#: Everything in the seam that only reads.
_READERS = {"find_rows", "find_row", "find_dicts", "scalar", "agg_row",
            "group_rows", "join_rows", "left_join_rows", "anti_join_rows",
            "exists", "count", "find_docs", "aggregate", "count_docs",
            "distinct_values"}


def _seam_calls(source: str) -> list[tuple[str, ast.Call]]:
    """Every call into the Mongo seam, however the name reached the call site.

    The first version of this scanner matched `mongo_query.X()` and
    `mongo_store.X()` and nothing else, so

        from app.db.mongo_store import update_docs
        update_docs("positions", {}, {"$set": {...}})

    was invisible to it -- the write guard had a hole in exactly the shape a
    writer is most easily added in. It now resolves the four other spellings
    too: a renamed module (`from app.db import mongo_store as ms`), a renamed
    helper (`import update_docs as up`), a dotted import
    (`app.db.mongo_store.update_docs(...)`), and the bare import above. Each is
    a negative control in
    `test_the_write_scanner_sees_a_writer_however_it_is_spelled`.
    """
    tree = ast.parse(source)
    module_aliases: dict[str, str] = {"mongo_query": "app.db.mongo_query",
                                      "mongo_store": "app.db.mongo_store"}
    direct: dict[str, str] = {}

    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            for alias in node.names:
                if f"{mod}.{alias.name}" in _SEAM_MODULES:
                    module_aliases[alias.asname or alias.name] = f"{mod}.{alias.name}"
                elif mod in _SEAM_MODULES:
                    direct[alias.asname or alias.name] = alias.name
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name in _SEAM_MODULES and alias.asname:
                    module_aliases[alias.asname] = alias.name

    calls: list[tuple[str, ast.Call]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Attribute):
            base = (func.value.id if isinstance(func.value, ast.Name)
                    else ast.unparse(func.value))
            if base in module_aliases or base in _SEAM_MODULES:
                calls.append((func.attr, node))
        elif isinstance(func, ast.Name) and func.id in direct:
            calls.append((direct[func.id], node))
    return calls


_WRITER_SPELLINGS = {
    "attribute call": "from app.db import mongo_store\n"
                      "mongo_store.update_docs('positions', {}, {})\n",
    "bare import": "from app.db.mongo_store import update_docs\n"
                   "update_docs('positions', {}, {})\n",
    "renamed module": "from app.db import mongo_store as ms\n"
                      "ms.update_docs('positions', {}, {})\n",
    "renamed helper": "from app.db.mongo_store import update_docs as up\n"
                      "up('positions', {}, {})\n",
    "dotted import": "import app.db.mongo_store as store\n"
                     "app.db.mongo_store.update_docs('positions', {}, {})\n",
}


@pytest.mark.parametrize("spelling", sorted(_WRITER_SPELLINGS))
def test_the_write_scanner_sees_a_writer_however_it_is_spelled(spelling):
    """NEGATIVE CONTROL for the guard below, and the proof it has no hole.

    RED before this revision for four of these five: the old scanner required
    the literal name `mongo_store` or `mongo_query` as the call's base, so only
    "attribute call" was caught. A guard that only catches the spelling nobody
    would sneak a write in under is decoration.
    """
    names = {name for name, _ in _seam_calls(_WRITER_SPELLINGS[spelling])}
    assert "update_docs" in names, (
        f"a writer spelled as {spelling!r} is invisible to the scanner")


def test_the_battery_calls_only_read_helpers():
    calls = _seam_calls(SOURCE)
    assert calls, "scanner found no mongo calls at all -- it is broken"
    bad = sorted({name for name, _ in calls if name not in _READERS})
    assert not bad, f"the soak loops on the live store; these write: {bad}"


def test_nothing_writing_is_even_imported_from_the_seam():
    """One step earlier than the call: an imported writer is a writer one
    keystroke from being called, and this is the import the scanner above had
    to be taught to see."""
    tree = ast.parse(SOURCE)
    imported = [alias.name for node in ast.walk(tree)
                if isinstance(node, ast.ImportFrom)
                and (node.module or "") in _SEAM_MODULES
                for alias in node.names]
    assert all(name in _READERS for name in imported), imported


def test_the_battery_does_not_read_price_history():
    """One ticker-date carries several vendor prints that disagree by ~20%, so
    every read of it must pin a vendor. The soak has four other collections
    that exercise the same code path and no reason to take that on."""
    named = [call.args[0].value for _, call in _seam_calls(SOURCE)
             if call.args and isinstance(call.args[0], ast.Constant)]
    assert "price_history" not in named


def test_the_battery_passes_table_names_not_resolved_ones():
    """`collection_for(t)` as the first argument resolves the name twice."""
    for _, call in _seam_calls(SOURCE):
        if not call.args:
            continue
        first = call.args[0]
        assert not (isinstance(first, ast.Call)
                    and getattr(first.func, "id", getattr(first.func, "attr", ""))
                    in {"collection_for", "target_collection_for"}), \
            ast.unparse(call)[:120]


# ── the battery IS the workload it documents (offline) ───────────────

#: The reads the battery must issue, in order, with the arguments that make
#: each one a soak of the live read path rather than a shape that happens to
#: parse. Every element here is defended by a live test in
#: `TestAgainstTheLiveStore`: the filters select proper subsets, the limits
#: bind, the descending sorts pick the newest rows rather than the oldest, and
#: the two aggregates return numbers two orders of magnitude apart.
EXPECTED_READS: list[tuple[str, dict]] = [
    ("find_rows", {"collection": "pipeline_events",
                   "query": {"phase": "analyzing"},
                   "columns": ["cycle_id", "step", "status", "elapsed_ms"],
                   "sort": [("timestamp", -1)], "limit": 200}),
    ("find_dicts", {"collection": "agent_tool_telemetry",
                    "query": {"success": True},
                    "sort": [("created_at", -1)], "limit": 100}),
    ("agg_row", {"collection": "cycle_audit_log",
                 "query": {"event_type": "error"},
                 "aggs": [("count", None), ("count_distinct", "cycle_id")]}),
    ("group_rows", {"collection": "agent_tool_telemetry",
                    "query": {"success": True},
                    "keys": ["agent_name"], "aggs": [("count", None)],
                    "select": [("key", "agent_name"), ("agg", 0)],
                    "sort": [("a0", -1)], "limit": 10}),
    ("count", {"collection": "news_articles",
               "query": {"quality_status": "ok"}}),
    ("find_row", {"collection": "cycle_audit_log",
                  "query": {"event_type": "error"},
                  "columns": ["cycle_id", "message"],
                  "sort": [("timestamp", -1)]}),
    ("scalar", {"collection": "news_articles",
                "query": {"quality_status": "ok"}, "column": "title",
                "sort": [("published_at", -1)]}),
    ("exists", {"collection": "agent_tool_telemetry",
                "query": {"success": False}}),
]

#: Canned answers, in sizes no real collection returns and no two alike, so a
#: probe value that was hardcoded rather than measured cannot match.
_CANNED = {
    "find_rows": [("c", "s", "ok", 1)] * 7,
    "find_dicts": [{"agent_name": "a"}] * 5,
    "agg_row": (11, 3),
    "group_rows": [("a", 4)] * 4,
    "count": 13,
    "find_row": ("cycle-1", "boom"),
    "scalar": "a headline",
    "exists": True,
}

EXPECTED_PROBES = [
    ("find_rows/pipeline_events", 7),
    ("find_dicts/agent_tool_telemetry", 5),
    ("agg_row/cycle_audit_log", 11),
    ("agg_row/cycle_audit_log:distinct", 3),
    ("group_rows/agent_tool_telemetry", 4),
    ("count/news_articles", 13),
    ("find_row/cycle_audit_log", 1),
    ("scalar/news_articles", 1),
    ("exists/agent_tool_telemetry", 1),
]

#: Rows the process holds: 7 + 5 + 4 + 1 + 1. NOT the counts -- `count` and
#: `agg_row` report 13 and 11 while transferring one integer each, and adding
#: them would claim 36 documents of allocation for 18.
EXPECTED_MATERIALIZED = 18


class _RecordingSeam:
    """Stands in for `mongo_query`, binds each call against the real signature.

    Binding through `inspect.signature(...)` rather than storing `*args` means
    the pin is on the ARGUMENTS, not on whether they were passed positionally,
    so it survives a caller switching to keywords and still fails on a changed
    filter, sort or limit. `apply_defaults()` fills in `limit=0` for a call
    that dropped its limit, which is how a removed limit is caught.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    def __getattr__(self, name: str):
        if name.startswith("_"):
            raise AttributeError(name)
        if name not in _CANNED:
            raise AssertionError(
                f"the battery called mongo_query.{name}(), which this recorder "
                "does not stub. Add it to _CANNED and to EXPECTED_READS -- a "
                "new read is a new claim about the seam.")
        real = getattr(real_seam, name)

        def _record(*args, **kwargs):
            bound = inspect.signature(real).bind(*args, **kwargs)
            bound.apply_defaults()
            self.calls.append((name, dict(bound.arguments)))
            return _CANNED[name]

        return _record


class TestTheBatteryIsTheWorkloadItDocuments:
    """The pin the first version of this file was missing.

    Runs with no database: the failure it catches is a battery that does not
    read one.
    """

    def test_it_issues_exactly_these_reads_in_this_order(self, monkeypatch):
        """RED for every workload mutant.

        Short-circuiting `_read_battery` with a fabricated return records no
        calls at all and fails on the first assertion. Restoring the historical
        `severity="ERROR"` filter, dropping `phase`, swapping
        pipeline_events -> cycle_audit_log, flipping either date sort,
        removing `limit=200` and swapping the two aggregates each record
        different bound arguments and fail on the second.
        """
        seam = _RecordingSeam()
        monkeypatch.setattr(memory_soak, "mongo_query", seam)

        memory_soak._read_battery()

        assert [name for name, _ in seam.calls] == \
            [name for name, _ in EXPECTED_READS], \
            "the battery is not the sequence of seam calls it documents"
        for (helper, want), (_, got) in zip(EXPECTED_READS, seam.calls):
            for field, value in want.items():
                assert got[field] == value, (
                    f"mongo_query.{helper}(): {field} is {got[field]!r}, "
                    f"the soak documents {value!r}")

    def test_every_probe_is_measured_not_asserted(self, monkeypatch):
        """The probe values must come from what the seam returned.

        The canned sizes are 7/5/11/3/4/13 and all different, so a battery that
        reports plausible constants -- 200 rows, 100 docs -- rather than the
        length of what it read disagrees here.
        """
        seam = _RecordingSeam()
        monkeypatch.setattr(memory_soak, "mongo_query", seam)

        probes, _ = memory_soak._read_battery()

        assert probes == EXPECTED_PROBES

    def test_only_materialized_documents_are_counted_as_allocation(self, monkeypatch):
        """A count transfers one integer. Summing it into `materialized` would
        report ~118,000 documents of allocation for the 312 the live battery
        actually holds, and the number would then rise every time the news
        table grew rather than when the process held more."""
        seam = _RecordingSeam()
        monkeypatch.setattr(memory_soak, "mongo_query", seam)

        _, materialized = memory_soak._read_battery()

        assert materialized == EXPECTED_MATERIALIZED

    def test_a_seam_call_the_recorder_does_not_know_is_loud(self):
        """The recorder must refuse an unstubbed helper rather than returning a
        Mock that satisfies `len()`. Otherwise a battery rewritten onto other
        helpers would pass this class by accident."""
        with pytest.raises(AssertionError, match="does not stub"):
            _RecordingSeam().distinct_values


# ── the three routes to reporting health that was not measured ───────

def _run(monkeypatch, battery, iterations=3, leak_mb=500, rss=None):
    monkeypatch.setattr(memory_soak, "_read_battery", battery)
    if rss is not None:
        monkeypatch.setattr(memory_soak, "_rss_mb", rss)
    return asyncio.run(memory_soak.memory_soak_test(iterations=iterations,
                                                    leak_mb=leak_mb))


def test_an_empty_probe_fails_the_run(monkeypatch):
    """NEGATIVE CONTROL for the failure this whole effort exists to catch.

    A battery whose filters match nothing allocates nothing and therefore
    cannot leak. Two of the probes were written with filters that matched
    nothing on the first attempt, so this is the observed failure, not a
    theoretical one.
    """
    empty = lambda: ([("find_rows/pipeline_events", 0)], 0)  # noqa: E731
    assert _run(monkeypatch, empty) is False


def test_a_partly_empty_probe_still_fails_the_run(monkeypatch):
    """The interesting case: most probes work, one silently stopped matching."""
    mixed = lambda: ([("a", 200), ("b", 0), ("c", 10)], 210)  # noqa: E731
    assert _run(monkeypatch, mixed) is False


def test_a_raising_battery_fails_the_run(monkeypatch):
    """RED before the port: the old loop caught Exception per iteration, logged
    it, and still returned True after every iteration had failed."""
    def boom():
        raise RuntimeError("collection is gone")

    assert _run(monkeypatch, boom) is False


def test_a_healthy_battery_passes(monkeypatch):
    """The other direction -- the tests above must not pass by refusing
    everything."""
    ok = lambda: ([("a", 200), ("b", 100)], 300)  # noqa: E731
    assert _run(monkeypatch, ok) is True


def test_growth_past_the_threshold_fails_the_run(monkeypatch):
    ok = lambda: ([("a", 200)], 200)  # noqa: E731
    steps = iter([100.0, 150.0, 400.0, 900.0])
    assert _run(monkeypatch, ok, iterations=3, leak_mb=500,
                rss=lambda: next(steps)) is False


def test_growth_under_the_threshold_passes(monkeypatch):
    ok = lambda: ([("a", 200)], 200)  # noqa: E731
    steps = iter([100.0, 110.0, 120.0, 130.0])
    assert _run(monkeypatch, ok, iterations=3, leak_mb=500,
                rss=lambda: next(steps)) is True


def test_the_default_threshold_can_actually_be_crossed(monkeypatch):
    """RED with DEFAULT_LEAK_MB raised to 50000: a threshold above any RSS this
    process can reach is a gate that cannot fire, and it would look exactly
    like a clean soak forever. 600 MB of growth must fail at the default."""
    ok = lambda: ([("a", 200)], 200)  # noqa: E731
    steps = iter([100.0, 300.0, 700.0])
    assert _run(monkeypatch, ok, iterations=2,
                leak_mb=memory_soak.DEFAULT_LEAK_MB,
                rss=lambda: next(steps)) is False


def test_main_exits_nonzero_when_the_soak_fails(monkeypatch):
    """RED before the port: `asyncio.run(memory_soak_test())` discarded the
    return value, so a run that logged SEVERE MEMORY LEAK DETECTED still exited
    0 and could not gate anything."""
    monkeypatch.setattr(memory_soak, "_read_battery", lambda: ([("a", 0)], 0))
    assert memory_soak.main(["--iterations", "1"]) == 1


def test_main_exits_zero_when_the_soak_passes(monkeypatch):
    monkeypatch.setattr(memory_soak, "_read_battery", lambda: ([("a", 5)], 5))
    assert memory_soak.main(["--iterations", "1"]) == 0


def test_main_still_reads_sys_argv_when_given_nothing(monkeypatch):
    """`main()` takes argv for the tests above; the entry point passes none, so
    the parse must still come off the command line."""
    monkeypatch.setattr(memory_soak.sys, "argv",
                        ["memory_soak.py", "--iterations", "1"])
    monkeypatch.setattr(memory_soak, "_read_battery", lambda: ([("a", 5)], 5))
    assert memory_soak.main() == 0


# ── an empty run is not a clean run ──────────────────────────────────

@pytest.mark.parametrize("n", [0, -5])
def test_a_run_with_nothing_to_measure_is_refused(monkeypatch, n):
    """RED before this revision, in both halves.

    `--iterations 0` used to log "Memory soak test completed." and exit 0
    having opened no connection and read nothing -- the same silence as an
    empty probe, which this file's own doctrine says must abort. The previous
    test suite went further and asserted the fail-open was correct
    (`test_the_iteration_count_is_honoured[0]` expected True).
    """
    calls = []

    def battery():
        calls.append(1)
        return [("a", 3)], 3

    assert _run(monkeypatch, battery, iterations=n) is False
    assert calls == [], "it read something after refusing to run"
    assert memory_soak.main(["--iterations", str(n)]) == 1


@pytest.mark.parametrize("n", [1, 5])
def test_the_iteration_count_is_honoured(monkeypatch, n):
    calls = []

    def battery():
        calls.append(1)
        return [("a", 3)], 3

    assert _run(monkeypatch, battery, iterations=n) is True
    assert len(calls) == n


# ── the defaults are the contract for an unattended run ──────────────

class TestTheCommandLineDefaults:
    """RED before this revision: nothing read the flags at all, so deleting
    `--leak-mb`, changing the default iterations to 3 and raising the leak
    threshold to 50,000 MB all left the suite green."""

    def test_a_bare_run_is_a_hundred_iterations_at_five_hundred_mb(self):
        args = memory_soak._build_parser().parse_args([])
        assert args.iterations == 100
        assert args.leak_mb == 500

    def test_the_module_constants_are_those_defaults(self):
        assert memory_soak.DEFAULT_ITERATIONS == 100
        assert memory_soak.DEFAULT_LEAK_MB == 500
        assert inspect.signature(
            memory_soak.memory_soak_test).parameters["iterations"].default == 100
        assert inspect.signature(
            memory_soak.memory_soak_test).parameters["leak_mb"].default == 500

    def test_both_flags_are_accepted_and_typed(self):
        args = memory_soak._build_parser().parse_args(
            ["--iterations", "20", "--leak-mb", "250"])
        assert args.iterations == 20 and isinstance(args.iterations, int)
        assert args.leak_mb == 250.0 and isinstance(args.leak_mb, float)

    def test_the_usage_block_matches_the_parser(self):
        """The docstring is the only usage anyone reads. RED if a flag is
        deleted or renamed without the prose following it."""
        usage = memory_soak.__doc__.split("USAGE", 1)[1].split("\n\n", 1)[0]
        assert "--iterations 20 --leak-mb 250" in usage

        # Parse EVERY usage line's arguments with the real parser, not a slice
        # of one of them. `usage.split("--iterations", 1)[1].split()[-3:]`
        # dropped the flag it split on and handed argparse a bare `20`, so the
        # test failed on its own string surgery rather than on any mismatch
        # between the prose and the parser — which is the one thing it exists
        # to catch.
        lines = [l.strip() for l in usage.strip().split("\n") if l.strip()]
        assert lines, "the USAGE block is empty"
        for line in lines:
            # A trailing parenthetical aside — "(100 iterations)" — is prose,
            # and it is TWO tokens, so dropping only the one that starts with
            # "(" leaves "iterations)" behind for argparse to choke on. Cut
            # from the parenthesis to end of line.
            command = line.split("(", 1)[0].strip()
            argv = command.split()
            assert argv[0] == "python", line
            memory_soak._build_parser().parse_args(argv[2:])

        parsed = memory_soak._build_parser().parse_args(
            ["--iterations", "20", "--leak-mb", "250"])
        assert (parsed.iterations, parsed.leak_mb) == (20, 250.0)


# ── the instrument's own instrument ──────────────────────────────────

def test_the_rss_probe_responds_to_a_real_allocation():
    """A memory probe whose number never moves reports "no leak" forever.

    The live soak holds flat to 0.01 MB across every iteration, which is either
    a clean read path or a dead gauge. This is what tells them apart: touch
    120 MB and require the reading to follow.
    """
    before = memory_soak._rss_mb()
    blob = bytearray(120 * 1024 * 1024)
    for i in range(0, len(blob), 4096):
        blob[i] = 1
    during = memory_soak._rss_mb()
    del blob
    assert during - before > 100, (
        f"_rss_mb did not move for a 120 MB allocation: "
        f"{before:.1f} -> {during:.1f} MB")


def test_the_rss_probe_reads_current_not_peak_memory():
    """`ru_maxrss` is a high-water mark and never falls, so it cannot tell a
    leak from a transient peak that was freed. This pins that the reading comes
    back DOWN after the allocation is released."""
    baseline = memory_soak._rss_mb()
    blob = bytearray(120 * 1024 * 1024)
    for i in range(0, len(blob), 4096):
        blob[i] = 1
    peak = memory_soak._rss_mb()
    del blob
    after = memory_soak._rss_mb()
    assert peak > baseline + 100
    assert after < peak - 100, (
        f"reading never fell after the free: {baseline:.1f} -> {peak:.1f} -> "
        f"{after:.1f} MB -- that is a peak gauge, not a current one")


# ── against the live store ───────────────────────────────────────────

class TestAgainstTheLiveStore:
    """Read-only, against production Mongo -- `TRADING_BOT_LIVE_AUDIT=1`.

    The offline class pins WHICH reads the battery issues. This one is the
    other half of trap 7: an offline test cannot tell a filter that runs from a
    filter that matches something, and a soak whose battery matches nothing
    allocates nothing and reports "no leak" forever.

    The numbers in these docstrings were measured on 2026-08-30. The
    collections grow, so the assertions are on the properties that must hold --
    non-empty, a proper subset, the limit binding, the sort observable -- not
    on the counts.
    """

    def test_every_probe_reads_something(self, live_mongo):
        """The whole point. Measured: 200 / 100 / 36,020 / 326 / 10 / 81,970 /
        1 / 1 / 1, with 312 documents materialized (200 + 100 + 10 + 1 + 1)."""
        probes, materialized = memory_soak._read_battery()

        assert [name for name, _ in probes] == \
            [name for name, _ in EXPECTED_PROBES]
        empty = [name for name, value in probes if value == 0]
        assert not empty, f"probes matched nothing: {empty}"
        assert materialized == 312, (
            f"the battery materialized {materialized} documents, not the "
            "200 + 100 + 10 + 1 + 1 its limits ask for")

    def test_each_probe_agrees_with_an_independent_recount(self, live_mongo):
        """Recounted through `mongo_store`, not through the helper under test,
        so the check cannot agree with a broken helper by sharing its bug."""
        values = dict(memory_soak._read_battery()[0])

        assert values["count/news_articles"] == real_store.count_docs(
            "news_articles", {"quality_status": "ok"})
        assert values["agg_row/cycle_audit_log"] == real_store.count_docs(
            "cycle_audit_log", {"event_type": "error"})
        assert values["agg_row/cycle_audit_log:distinct"] == len(
            real_store.distinct_values("cycle_audit_log", "cycle_id",
                                       {"event_type": "error"}))
        assert values["group_rows/agent_tool_telemetry"] == min(
            10, len(real_store.distinct_values("agent_tool_telemetry",
                                               "agent_name", {"success": True})))
        assert values["exists/agent_tool_telemetry"] == int(
            real_store.count_docs("agent_tool_telemetry", {"success": False}) > 0)

    def test_the_limits_are_what_bounds_the_two_row_reads(self, live_mongo):
        """200 and 100 must be the limit biting, not the population running
        out -- otherwise the soak's allocation per iteration is whatever the
        store happens to hold that day."""
        analyzing = real_store.count_docs("pipeline_events",
                                          {"phase": "analyzing"})
        succeeded = real_store.count_docs("agent_tool_telemetry",
                                          {"success": True})
        assert analyzing > 200, analyzing      # 154,728
        assert succeeded > 100, succeeded      # 17,248

    def test_every_filter_selects_a_proper_subset(self, live_mongo):
        """A filter that matches everything cannot go to zero, so the non-empty
        guard behind it can never fire. The news probe used to read
        `{"quality_status": {"$exists": true}}`, which matched 116,354 of
        116,354 -- it would have survived the exact vocabulary change the guard
        exists to catch. `"ok"` matches 81,970 of 116,354.
        """
        for collection, query in (("pipeline_events", {"phase": "analyzing"}),
                                  ("agent_tool_telemetry", {"success": True}),
                                  ("agent_tool_telemetry", {"success": False}),
                                  ("cycle_audit_log", {"event_type": "error"}),
                                  ("news_articles", {"quality_status": "ok"})):
            selected = real_store.count_docs(collection, query)
            total = real_store.count_docs(collection)
            assert 0 < selected < total, (collection, query, selected, total)

        vacuous = real_store.count_docs("news_articles",
                                        {"quality_status": {"$exists": True}})
        assert vacuous == real_store.count_docs("news_articles"), (
            "the filter this probe was changed away from no longer matches "
            "everything -- re-check whether the change is still warranted")

    def test_the_historical_severity_filter_still_matches_nothing(self, live_mongo):
        """The bug the empty-probe guard was built for, still verifiable.

        `severity="ERROR"` was the first attempt at the audit-log probe. The
        stored severities are lowercase and never include "error" at all; the
        field is `event_type`. This is why the battery may not be judged by
        whether it parses.
        """
        assert real_store.count_docs("cycle_audit_log",
                                     {"severity": "ERROR"}) == 0
        assert real_store.count_docs("cycle_audit_log",
                                     {"event_type": "error"}) > 1000

    def test_the_two_aggregates_are_not_interchangeable(self, live_mongo):
        """Swapping COUNT(*) and COUNT(DISTINCT cycle_id) in the aggs list
        keeps the shape and reverses the meaning. Measured 36,020 error rows
        across 326 cycles -- two orders of magnitude apart, so a swap is a
        visible fault rather than a rounding one."""
        total, distinct = real_seam.agg_row(
            "cycle_audit_log", {"event_type": "error"},
            [("count", None), ("count_distinct", "cycle_id")])
        assert total > distinct * 10 > 0

    def test_the_date_sorts_read_the_newest_rows_not_the_oldest(self, live_mongo):
        """Trap 2, live. Natural order and an ascending date both hand back the
        OLDEST documents of a collection that grows all day. pipeline_events
        spans 115 days and agent_tool_telemetry 45, so the direction is not a
        detail: it decides whether the soak reads today's rows or May's.
        """
        for collection, query, field in (
                ("pipeline_events", {"phase": "analyzing"}, "timestamp"),
                ("agent_tool_telemetry", {"success": True}, "created_at")):
            newest = real_seam.scalar(collection, query, field,
                                      sort=[(field, -1)])
            oldest = real_seam.scalar(collection, query, field,
                                      sort=[(field, 1)])
            assert newest > oldest
            assert (newest - oldest).days > 30, (collection, oldest, newest)

    def test_the_grouped_sort_takes_the_busiest_agents(self, live_mongo):
        """`sort=[("a0", -1)]` ascending would return the ten quietest groups.
        13 agents have a successful call, so the two top-10s overlap by seven
        -- the pin is the ordering, not the membership. Measured: busiest
        v3_junior_analyst at 4,382, quietest test_agent at 1."""
        args = ("agent_tool_telemetry", {"success": True}, ["agent_name"],
                [("count", None)], [("key", "agent_name"), ("agg", 0)])
        busiest = real_seam.group_rows(*args, sort=[("a0", -1)], limit=10)
        quietest = real_seam.group_rows(*args, sort=[("a0", 1)], limit=10)
        assert busiest != quietest
        assert busiest[0][1] > quietest[0][1] * 10

    def test_no_field_the_battery_filters_on_can_be_missing(self, live_mongo):
        """Trap 3. The archive's column DEFAULTs did not survive the cutover,
        so a field the pre-cutover rows always carried can be absent on a
        post-cutover document -- and `{"$exists": false}` documents match
        neither an equality nor a range filter, so they leave a probe silently
        short."""
        for collection, field in (("pipeline_events", "phase"),
                                  ("agent_tool_telemetry", "success"),
                                  ("cycle_audit_log", "event_type"),
                                  ("news_articles", "quality_status")):
            missing = real_store.count_docs(collection,
                                            {field: {"$exists": False}})
            assert missing == 0, f"{collection}.{field}: {missing} documents"

    def test_every_sort_field_is_a_date_everywhere(self, live_mongo):
        """Trap 5. A string timestamp sorts ABOVE every BSON Date, so one
        string in `timestamp` would make the descending read return that row
        forever and the soak would re-read the same document 100 times."""
        for collection, field in (("pipeline_events", "timestamp"),
                                  ("agent_tool_telemetry", "created_at"),
                                  ("news_articles", "published_at"),
                                  ("cycle_audit_log", "timestamp")):
            strings = real_store.count_docs(collection,
                                            {field: {"$type": "string"}})
            assert strings == 0, f"{collection}.{field}: {strings} strings"
