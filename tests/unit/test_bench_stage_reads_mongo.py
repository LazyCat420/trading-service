"""`bench_stage.py` is a SANDBOX, and both halves of the sandbox were dead.

The script asks one question of the database — "is a real cycle live right
now?" — and makes one promise about it: "the database session is READ ONLY".
The 2026-08-19 Mongo cutover killed both, silently, in opposite ways.

1. THE READ ANSWERED THE ARCHIVE.
   `live_cycle_id()` ran

       SELECT cycle_id, status FROM pipeline_state WHERE singleton_id='current'

   against Postgres. `pipeline_state` is a SINGLETON — one row, overwritten in
   place — so the archive copy is not merely stale, it is FROZEN on the last
   value the cutover saw and can never change again. Measured 2026-08-30:

       pg    cycle-v3-1787179210  status=done  updated_at 2026-08-19 22:55:05
       mongo cycle-v3-1788074145  status=done  updated_at 2026-08-30 07:21:56
       -> the archive is 10.4 days behind, permanently

   `cycle_benchmarks` says how many live windows that blinded: 0 rows in pg
   after the cutover against 95 in Mongo (pre-cutover the two agree, 175 pg
   ⊂ 177 mongo, the 2 extras being `cycle-v3-probe` and `test-cycle`). So the
   header's "⚠ A REAL CYCLE IS LIVE" and the refusal to add LLM load to a
   live cycle could not fire for any of 95 cycles, and every timing the tool
   printed was labelled clean.

2. THE ALLOWLIST HAD DRIFTED IN BOTH DIRECTIONS AND KEPT ITS SHAPE.
   The live test was a POSITIVE list, ("running", "starting", "collecting",
   "analyzing", "trading"). Three of those five are `pipeline_state.phase`
   values and are never written to `status` (zero sites in `app/`), while
   `stopping` (`pipeline_service.py:2705`) and `blocked`
   (`boot_service.py:240`) ARE live statuses and were both missed. Five
   entries, two of them real. The rest of the repo states the same test
   negatively and identically in five places; this file pins bench_stage to
   that one definition rather than to a hand-copied list.

3. THE WRITE GUARD PROTECTED A SEAM THE CODE HAD LEFT.
   `install_read_only_db()` wrapped `get_db` so the session issued
   `SET default_transaction_read_only = on`. After the cutover `app/` has
   ZERO live `get_db()` call sites and 312 `mongo_store` write calls, so the
   guard covered nothing while the header printed `db=READ-ONLY`. This is not
   theoretical: the first read-only run of the ported script blocked 8 real
   writes on a plain `--all-context --ticker AAPL` —
   `price_history.update_one`, `technicals.bulk_write`,
   `data_source_status.update_many`, `watch_events.find_one_and_update` and
   `v3_guardrail_firings.insert_many` x4 — every one of which used to land in
   production Mongo from a tool whose whole premise is that it does not write.

WHY EACH TEST HERE WOULD HAVE BEEN RED BEFORE THE PORT
------------------------------------------------------
Verified by running these assertions against `git show 77e6dc3:scripts/bench_stage.py`
loaded as a separate module, not by reasoning about it.

  test_the_script_has_no_postgres_coupling
        the pre-port file matched the coupling grep on 4 lines (27, 78, 82,
        98) — the psycopg exception name, the `pg_connection.get_db` patch
        target, its import, and the import inside `live_cycle_id`.
  test_live_cycle_id_reads_the_mongo_pipeline_state_singleton
        the old function never touched `mongo_query`, so the recorder below
        stays empty; it called `pg_connection.get_db()`, which today raises
        AttributeError on `settings.DATABASE_URL` inside the blanket `except`
        and returns None for every status.
  test_a_stopping_or_blocked_cycle_is_LIVE
        `stopping` and `blocked` were outside the positive allowlist, so the
        old code returned None for both — no warning, no `--force` gate.
  test_the_terminal_status_set_is_the_one_the_rest_of_the_repo_uses
        there was no such constant to compare, and the literal that existed
        disagreed with all five sites in both directions.
  test_the_guard_blocks_every_mongo_write_verb
  test_a_blocked_write_is_recorded_by_name
        the old `install_read_only_db()` left `pymongo.collection.Collection`
        completely untouched, so every one of these calls went to the server.
  test_the_guard_leaves_the_read_verbs_alone
        passes before AND after — it exists to stop the fix from being bought
        by breaking reads, which is the cheap way to make the guard "work".
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from scripts import bench_stage

REPO = Path(bench_stage.__file__).resolve().parents[1]
SOURCE = Path(bench_stage.__file__).read_text(encoding="utf-8")

# The exact grep the port is measured by.
_PG_COUPLING = re.compile(r"psycopg|DATABASE_URL|pg_connection|dbname=|postgres")


def test_the_script_has_no_postgres_coupling():
    hits = [
        f"{n}: {line.strip()}"
        for n, line in enumerate(SOURCE.splitlines(), 1)
        if _PG_COUPLING.search(line)
    ]
    assert hits == [], "bench_stage still reaches the frozen archive:\n" + "\n".join(hits)


# ── The live-cycle read ───────────────────────────────────────────────────

class _Recorder:
    """Stands in for `mongo_query.find_row` and remembers how it was called."""

    def __init__(self, row):
        self.row = row
        self.calls: list[tuple] = []

    def __call__(self, collection, query, columns, sort=None):
        self.calls.append((collection, query, columns, sort))
        return self.row


def test_live_cycle_id_reads_the_mongo_pipeline_state_singleton(monkeypatch):
    rec = _Recorder(("cycle-v3-1788074145", "running"))
    monkeypatch.setattr("app.db.mongo_query.find_row", rec)

    assert bench_stage.live_cycle_id() == "cycle-v3-1788074145 (running)"

    assert len(rec.calls) == 1, "the live-cycle check must be exactly one read"
    collection, query, columns, sort = rec.calls[0]
    # The POSTGRES TABLE NAME. `mongo_query` resolves it through
    # `collection_for()` exactly once; handing it an already-resolved name
    # resolves twice, and the day renames are switched on the read misses and
    # a write would create an invisible second collection.
    assert collection == "pipeline_state"
    assert query == {"singleton_id": "current"}
    assert list(columns) == ["cycle_id", "status"]
    # A singleton has nothing to sort and nothing to sample: one document,
    # overwritten in place. A sort here would be a sign the reader thinks this
    # is a history table.
    assert sort is None


@pytest.mark.parametrize("status", ["starting", "running", "stopping", "blocked"])
def test_a_stopping_or_blocked_cycle_is_LIVE(monkeypatch, status):
    monkeypatch.setattr("app.db.mongo_query.find_row", _Recorder(("cycle-x", status)))
    assert bench_stage.live_cycle_id() == f"cycle-x ({status})"


@pytest.mark.parametrize("status", ["idle", "done", "error", "stopped", "interrupted"])
def test_a_finished_cycle_is_not_live(monkeypatch, status):
    monkeypatch.setattr("app.db.mongo_query.find_row", _Recorder(("cycle-x", status)))
    assert bench_stage.live_cycle_id() is None


def test_no_pipeline_state_document_is_not_a_live_cycle(monkeypatch):
    monkeypatch.setattr("app.db.mongo_query.find_row", _Recorder(None))
    assert bench_stage.live_cycle_id() is None


def test_an_unreachable_store_never_raises_out_of_the_check(monkeypatch):
    """The header must degrade to "no cycle", not take the whole run down."""
    def _boom(*_a, **_kw):
        raise RuntimeError("mongo down")

    monkeypatch.setattr("app.db.mongo_query.find_row", _boom)
    assert bench_stage.live_cycle_id() is None


# ── The status vocabulary, pinned against its producers ───────────────────

# Every file that decides "a cycle is already running" from
# `pipeline_state.status`. Named explicitly so a site that DISAPPEARS is a
# failure here rather than a silently smaller comparison.
_GUARD_SITES = (
    "app/services/cycle_scheduler.py",
    "app/services/watch_desk.py",
    "scripts/smoke_test_cycle.py",
    "scripts/smoke_test_streaming.py",
)
_NOT_IN_TUPLE = re.compile(r"not in (\((?:\s*[\"'][a-z_]+[\"']\s*,?)+\))")


def test_the_terminal_status_set_is_the_one_the_rest_of_the_repo_uses():
    """Parse the producers; compare SETS, both directions, never lengths.

    An allowlist can drift both ways and keep its count — the pre-port list
    had five entries of which three were dead and two live statuses were
    missing, so any length check would have read as healthy.
    """
    mine = set(bench_stage.TERMINAL_CYCLE_STATUSES)
    found_in: dict[str, list[set]] = {}

    for rel in _GUARD_SITES:
        text = (REPO / rel).read_text(encoding="utf-8")
        tuples = [
            set(ast.literal_eval(m.group(1)))
            for m in _NOT_IN_TUPLE.finditer(text)
        ]
        # Only the cycle-status guards, identified by content rather than by
        # line number so a moved guard is still checked.
        tuples = [t for t in tuples if "idle" in t and "done" in t]
        if tuples:
            found_in[rel] = tuples

    assert set(found_in) == set(_GUARD_SITES), (
        "a cycle-status guard moved or vanished; bench_stage can no longer be "
        f"pinned against it. found: {sorted(found_in)}"
    )
    for rel, tuples in found_in.items():
        for t in tuples:
            assert t - mine == set(), f"{rel} treats {sorted(t - mine)} as over, bench_stage does not"
            assert mine - t == set(), f"bench_stage treats {sorted(mine - t)} as over, {rel} does not"


def test_the_dead_phase_values_are_not_treated_as_statuses():
    """`collecting`/`analyzing`/`trading` are `phase`, not `status`.

    They were three fifths of the old positive allowlist and matched nothing.
    Under the negative rule they now correctly read as LIVE if they ever DID
    appear in `status` — this asserts the repo still never writes them there,
    which is what makes the negative rule the right shape.
    """
    written = re.compile(
        r"""["']status["']\s*:\s*["'](collecting|analyzing|trading)["']"""
    )
    hits = []
    for path in (REPO / "app").rglob("*.py"):
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if written.search(line):
                hits.append(f"{path.relative_to(REPO)}:{n}")
    assert hits == [], f"a phase value is being written as a status: {hits}"


# ── The read-only guard ───────────────────────────────────────────────────

@pytest.fixture
def restored_pymongo():
    """Install/uninstall the class-level guard without leaking into the suite.

    The guard is deliberately process-wide — that is what makes it catch the
    `mongo_store._coll(...).bulk_write(...)` and `vector_store` routes as well
    as the helpers. A test that installs it must therefore put every method
    back, or every later test in the session inherits a store that refuses to
    write.
    """
    import pymongo

    from app.db import mongo_store

    saved = [
        (cls, name, getattr(cls, name))
        for cls, names in (
            (pymongo.collection.Collection, bench_stage._COLLECTION_WRITES),
            (pymongo.database.Database, bench_stage._DATABASE_WRITES),
            (pymongo.MongoClient, bench_stage._CLIENT_WRITES),
        )
        for name in names
        if hasattr(cls, name)
    ]
    saved_flag = getattr(pymongo.collection.Collection, "_bench_read_only", None)
    saved_indexes = mongo_store._indexes_ready
    saved_blocked = list(bench_stage.BLOCKED_WRITES)
    bench_stage.BLOCKED_WRITES.clear()
    try:
        yield pymongo
    finally:
        # The FLAG comes off first, and each step is independent.
        #
        # The first version of this teardown restored the methods, then tried
        # `Collection.__dict__.pop(...)` — a class `__dict__` is a read-only
        # mappingproxy, so that raised and `_bench_read_only` survived. Every
        # later test then got `install_read_only_db()` returning early on a
        # stale flag, so the guard was NOT installed, and the unconnected
        # client fell through to a real server-selection timeout. The run went
        # from `.E` to a 6-minute hang. An ordering bug in a cleanup is
        # indistinguishable from a slow test, so: flag first, no step able to
        # skip another.
        try:
            del pymongo.collection.Collection._bench_read_only
        except AttributeError:
            pass
        if saved_flag is not None:
            pymongo.collection.Collection._bench_read_only = saved_flag
        for cls, name, fn in saved:
            setattr(cls, name, fn)
        mongo_store._indexes_ready = saved_indexes
        bench_stage.BLOCKED_WRITES[:] = saved_blocked


def _unconnected_collection(pymongo, name="whiteboard_entries"):
    """A real `pymongo.collection.Collection`, bound to nothing.

    `connect=False` defers every socket, so this object exercises real method
    resolution on the real class without reaching a server. A hand-rolled fake
    would prove only that the fake was patched.

    `serverSelectionTimeoutMS=200` is the tripwire: if the guard is ever NOT
    installed, the call falls through to a real connection attempt, and the
    default 30s selection window would turn a broken guard into a slow suite
    rather than a red one.
    """
    client = pymongo.MongoClient(
        "mongodb://127.0.0.1:1/", connect=False, serverSelectionTimeoutMS=200)
    return client, client["trading_bot_never"][name]


@pytest.mark.parametrize("verb, args", [
    ("insert_one", ({"x": 1},)),
    ("insert_many", ([{"x": 1}],)),
    ("update_one", ({"x": 1}, {"$set": {"y": 2}})),
    ("update_many", ({"x": 1}, {"$set": {"y": 2}})),
    ("replace_one", ({"x": 1}, {"y": 2})),
    ("delete_one", ({"x": 1},)),
    ("delete_many", ({"x": 1},)),
    ("bulk_write", ([],)),
    ("find_one_and_update", ({"x": 1}, {"$set": {"y": 2}})),
    ("find_one_and_replace", ({"x": 1}, {"y": 2})),
    ("find_one_and_delete", ({"x": 1},)),
    ("drop", ()),
])
def test_the_guard_blocks_every_mongo_write_verb(restored_pymongo, verb, args):
    pymongo = restored_pymongo
    bench_stage.install_read_only_db()
    client, coll = _unconnected_collection(pymongo)
    try:
        with pytest.raises(bench_stage.SandboxWriteBlocked):
            getattr(coll, verb)(*args)
    finally:
        client.close()


def test_a_blocked_write_is_recorded_by_name(restored_pymongo):
    """The footer's count is the guard's only evidence; it has to move."""
    pymongo = restored_pymongo
    bench_stage.install_read_only_db()
    client, coll = _unconnected_collection(pymongo)
    try:
        for _ in range(2):
            with pytest.raises(bench_stage.SandboxWriteBlocked):
                coll.insert_many([{"x": 1}])
        with pytest.raises(bench_stage.SandboxWriteBlocked):
            coll.update_one({"x": 1}, {"$set": {"y": 2}})
    finally:
        client.close()
    assert bench_stage.BLOCKED_WRITES == [
        "whiteboard_entries.insert_many",
        "whiteboard_entries.insert_many",
        "whiteboard_entries.update_one",
    ]


def test_the_guard_leaves_the_read_verbs_alone(restored_pymongo):
    """A guard bought by breaking reads would make every stage FAIL, and the
    tool would still print `db=READ-ONLY`. Pin the reads by identity."""
    pymongo = restored_pymongo
    reads = ("find", "find_one", "aggregate", "count_documents", "distinct",
             "estimated_document_count", "list_indexes", "index_information")
    before = {n: getattr(pymongo.collection.Collection, n) for n in reads}
    bench_stage.install_read_only_db()
    for n in reads:
        assert getattr(pymongo.collection.Collection, n) is before[n], (
            f"the read-only guard replaced the READ method {n}"
        )


def test_the_guard_is_idempotent(restored_pymongo):
    """`install_read_only_db()` twice must not wrap a wrapper — the second
    layer's message and its BLOCKED_WRITES entry would double-count."""
    pymongo = restored_pymongo
    bench_stage.install_read_only_db()
    once = pymongo.collection.Collection.insert_one
    bench_stage.install_read_only_db()
    assert pymongo.collection.Collection.insert_one is once


def test_installing_the_guard_stops_the_index_ddl_path(restored_pymongo):
    """Every `mongo_store` write helper calls `ensure_indexes()` first.

    Left alone it would fire ~40 blocked `create_index` calls into
    BLOCKED_WRITES before the real write was even attempted, and the one
    number the footer prints would be catalog noise rather than the writes the
    sandbox actually stopped.
    """
    from app.db import mongo_store

    mongo_store._indexes_ready = False
    bench_stage.install_read_only_db()
    assert mongo_store._indexes_ready is True
