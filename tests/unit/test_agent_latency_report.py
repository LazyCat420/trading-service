"""The latency report has to read the store the agents still write to.

WHY THIS FILE EXISTS. `scripts/agent_latency_report.py` read
`v3_agent_telemetry` in Postgres, which stopped taking rows at the 2026-08-19
cutover. It did not break at the cutover — it kept printing a well-formatted
table of July cycles with today's date nowhere in it, `--days 2` answered "no
cycles matched" as though the fleet had gone quiet, and `--self-test` went on
PASSING, because chapter 34's control cycles are from August 7-9 and the frozen
archive still holds them perfectly. A self-test that a dead instrument passes is
the exact failure mode this suite is for, so the checks below are the ones the
self-test cannot make: where the read goes, and whether the arithmetic Postgres
used to do server-side survived the move to Python.

Every test here was RED before the port (measured 2026-08-30):
  * the coupling scan reported 1 finding (`driver_import` at line 34) for the
    original file, and the source carried
    `postgresql://trader:...@10.0.0.16:5433/trading_bot` as the os.getenv
    DEFAULT, so the connection happened whether or not the environment named it;
  * `pipeline`, `_row`, `_round1`, `_percentile_cont` and `_match` did not
    exist — the SQL was a format string.

The tests added 2026-08-30 were RED for a different reason: they were written
against surviving MUTANTS. With only the fourteen tests that were here before,
each of these one-line edits to the script left all fourteen GREEN — measured
in this tree, one mutant at a time, restoring the file in between:

  * `{"$sort": {"started": -1}}` -> `{"$sort": {"started": 1}}`   14 passed
  * `started: $min` / `ended: $max` swapped                       14 passed
  * `"runs": {"$sum": 1}` -> `{"$sum": 2}`                        14 passed
  * `def fetch(..., min_tickers=6)` -> `min_tickers=1`            14 passed

The first prints the forty OLDEST cycles — July at the top of a report whose
entire reason for being ported is that it used to print July at the top — and
`--self-test` passes anyway, because the control cycles are selected by id
under `limit=50` where order is irrelevant. The old
`test_the_width_floor_is_applied_before_the_limit` only read
`names.index("$sort")`, the stage's POSITION, and never its value; the old
`test_a_group_is_scored_exactly_as_postgres_scored_it` hands `_row()` a
hand-built doc, so no test here ever exercised an accumulator, a group key or a
percentile fraction.

A twenty-mutant sweep over the same file now kills 20/20, including all four
above plus `$addToSet` -> `$push`, `$gt` -> `$gte`, `_id: $cycle_id` ->
`$ticker`, `percentile_cont(0.5) -> 0.6`, `(0.9) -> 0.95`, `max -> min`, the
dropped `--days` clause, the dropped tz in `_utc`, half-even rounding, and
`collection_for(TABLE)` restored at the store call.

No test here touches a database: `mongo_store.aggregate` is patched, which is
also what keeps the autouse `block_production_mongo` guard satisfied.
"""
from __future__ import annotations

import datetime as dt
import sys
from decimal import Decimal
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

import scripts.agent_latency_report as alr  # noqa: E402
from app.db import collections as collections_mod  # noqa: E402
from app.db import date_fields  # noqa: E402
from scripts.gate_zero_pg import scan  # noqa: E402

REL = "scripts/agent_latency_report.py"


def test_the_report_has_no_postgres_coupling():
    result = scan(REPO, targets=(REL,))
    assert result["errors"] == [], result["errors"]
    assert result["total"] == 0, (
        "the latency report still reads Postgres: "
        + "; ".join(f"{f['kind']} at line {f['line']}" for f in result["findings"]))


def test_no_hardcoded_production_dsn():
    """The literal was an os.getenv DEFAULT, so an unset DATABASE_URL did not
    disable it — it selected it. Removing the driver is not enough on its own;
    the address has to go too, or the next person restores one line and gets a
    live connection to the frozen archive back."""
    src = (REPO / REL).read_text(encoding="utf-8")
    for needle in ("postgresql://", "psycopg", "DATABASE_URL", "10.0.0.16"):
        assert needle not in src, f"{REL} still carries {needle!r}"


def _capture(monkeypatch, docs=(), **kw):
    """Run `fetch` with the store stubbed out; return what it was handed."""
    seen = {}

    def fake_aggregate(collection, pipeline, **_):
        seen["collection"] = collection
        seen["pipeline"] = pipeline
        return list(docs)

    monkeypatch.setattr(alr.mongo_store, "aggregate", fake_aggregate)
    seen["rows"] = alr.fetch(**kw)
    return seen


def test_the_read_names_the_table_not_the_collection(monkeypatch):
    """`mongo_store.aggregate` takes a POSTGRES TABLE NAME, and resolving the
    collection before calling it is a defect even while the resolver is the
    identity function.

    `mongo_store._coll` says so in as many words -- "Never take a collection
    name from a caller ... a name that bypasses this function does not error;
    it silently starts a second, invisible collection" -- and every one of the
    30-odd `mongo_store.aggregate` call sites in app/ passes a bare table name,
    `app/v3/invariants.py` included, which reads THIS table as
    `mongo_store.aggregate("v3_agent_telemetry", pipeline)`.

    Asserting `== collection_for("v3_agent_telemetry")` instead would pin the
    defect rather than the contract: with the map inert both sides are the same
    string, so it cannot tell a correct call from a double-resolved one, and it
    goes RED on anyone who fixes the call once the renames land.
    """
    seen = _capture(monkeypatch)
    assert seen["rows"] == []
    assert seen["collection"] == "v3_agent_telemetry"


def test_the_days_bound_still_reaches_the_timestamp_registry(monkeypatch):
    """The consequence of double-resolving, and the reason it is not cosmetic.

    `mongo_store.aggregate` runs `date_fields.coerce_pipeline(<name>, ...)`,
    and that registry is keyed on the TABLE:

        TIMESTAMP_FIELDS['v3_agent_telemetry'] == {'created_at'}
        TIMESTAMP_FIELDS['log_agent_llm_calls'] == set()

    so a name that has already been through the resolver arrives with no
    timestamp fields and the `--days` bound stops being normalised. That
    registry is what stops a string bound being compared against BSON dates --
    Date sorts BELOW String, so such a comparison matches nothing and the
    report answers "no cycles matched" for a fleet that is running. Whatever
    this script hands the store has to be a name the registry knows.
    """
    seen = _capture(monkeypatch, days=2)
    assert "created_at" in date_fields.TIMESTAMP_FIELDS.get(seen["collection"], frozenset())


def test_the_table_name_survives_the_collection_rename(monkeypatch):
    """With the renames ACTIVE, `collection_for` stops being the identity.

    This is the test the old one could not be: pre-fix the script called
    `collection_for(TABLE)` itself, so under this monkeypatch the store was
    handed 'log_agent_llm_calls' and this assertion is RED. It is also the
    scenario the map is heading for -- see `renames_active`'s docstring, step 4.
    """
    monkeypatch.setattr(collections_mod, "renames_active", lambda: True)
    assert collections_mod.collection_for("v3_agent_telemetry") == "log_agent_llm_calls"
    seen = _capture(monkeypatch)
    assert seen["collection"] == "v3_agent_telemetry"


def test_the_width_floor_is_applied_before_the_limit():
    """HAVING count(DISTINCT ticker) >= n runs BEFORE ORDER BY ... LIMIT.

    Reordered — floor after `$limit` — the report takes the 40 most recent
    cycles and only then drops the narrow ones, so a run that used to show 40
    wide cycles shows a dozen, and the missing ones look like an outage rather
    than a stage in the wrong place. Nothing about the output says which
    happened, so the order is pinned here.
    """
    stages = alr.pipeline(min_tickers=6, limit=40)
    names = [next(iter(s)) for s in stages]
    having = next(i for i, s in enumerate(stages)
                  if s.get("$match", {}).get("tickers") == {"$gte": 6})
    assert having < names.index("$sort") < names.index("$limit")


def test_the_sort_is_newest_first():
    """ORDER BY min(created_at) DESC — the direction, not just the position.

    A `-1` flipped to `1` is the single most damaging one-character edit in
    this file, because it recreates the exact symptom the port was written to
    remove: the top of the default report becomes July 2026. It raises no
    error, changes no column, and `--self-test` still passes, because the
    control cycles are picked by id under `limit=50` where order is irrelevant.
    Nothing but this assertion can see it.
    """
    stages = alr.pipeline()
    sort = next(s["$sort"] for s in stages if "$sort" in s)
    assert sort == {"started": -1}, "the report must lead with the newest cycle"
    assert list(sort) == ["started"], "sorted on min(created_at), as the SQL was"


def test_the_default_width_floor_is_six(monkeypatch):
    """`min_tickers=6` is chapter 34's confound control.

    Chapter 34 scored every 1-ticker cycle at 0% over the watchdog and every
    6-to-9-ticker cycle at 34-62%, so a floor quietly lowered to 1 fills the
    report with rows that score 0 for a reason that has nothing to do with
    latency, and the headline column stops meaning what the docstring says it
    means. `fetch`'s default is the one that reaches the pipeline, so it is the
    one asserted here.
    """
    floors = [s["$match"]["tickers"]
              for s in _capture(monkeypatch)["pipeline"]
              if "tickers" in s.get("$match", {})]
    assert floors == [{"$gte": 6}]
    # `pipeline` carries the same default, so a direct caller cannot slip under it
    assert [s["$match"]["tickers"] for s in alr.pipeline()
            if "tickers" in s.get("$match", {})] == [{"$gte": 6}]


def test_a_named_cycle_lowers_the_floor_to_one(monkeypatch):
    """The one deliberate override: `--cycle X` must not answer "no cycles
    matched" merely because X happened to be narrow."""
    calls = {}
    monkeypatch.setattr(alr, "fetch", lambda **kw: calls.update(kw) or [])
    monkeypatch.setattr(sys, "argv", ["prog", "--cycle", "cycle-v3-1"])
    assert alr.main() == 0
    assert calls["min_tickers"] == 1 and calls["cycle"] == "cycle-v3-1"


# ── a very small aggregation engine, so the $group spec is EXECUTED ────────
# Only the operators this pipeline uses; anything else raises rather than
# quietly evaluating to None, so the test cannot go green on a stage it does
# not understand.

def _expr(e, doc, this=None):
    if isinstance(e, str) and e.startswith("$$"):
        assert e == "$$this", f"unsupported variable {e!r}"
        return this
    if isinstance(e, str) and e.startswith("$"):
        return doc.get(e[1:])
    if isinstance(e, dict):
        (op, arg), = e.items()
        if op == "$gt":
            return _expr(arg[0], doc, this) > _expr(arg[1], doc, this)
        if op == "$ne":
            return _expr(arg[0], doc, this) != _expr(arg[1], doc, this)
        if op == "$cond":
            c, t, f = arg
            return _expr(t, doc, this) if _expr(c, doc, this) else _expr(f, doc, this)
        if op == "$size":
            return len(_expr(arg, doc, this))
        if op == "$filter":
            return [v for v in _expr(arg["input"], doc, this)
                    if _expr(arg["cond"], doc, v)]
        raise AssertionError(f"the pipeline grew an operator this engine cannot run: {op!r}")
    return e


def _run_group_and_addfields(stages, docs):
    group = next(s["$group"] for s in stages if "$group" in s)
    add = next(s["$addFields"] for s in stages if "$addFields" in s)
    buckets = {}
    for d in docs:
        buckets.setdefault(_expr(group["_id"], d), []).append(d)
    out = []
    for key, members in buckets.items():
        row = {"_id": key}
        for field, acc in group.items():
            if field == "_id":
                continue
            (op, arg), = acc.items()
            vals = [_expr(arg, m) for m in members]
            if op == "$sum":
                row[field] = sum(int(v) for v in vals)
            elif op == "$min":
                row[field] = min(v for v in vals if v is not None)
            elif op == "$max":
                row[field] = max(v for v in vals if v is not None)
            elif op == "$push":
                row[field] = vals
            elif op == "$addToSet":
                uniq = []
                for v in vals:
                    if v not in uniq:
                        uniq.append(v)
                row[field] = uniq
            else:
                raise AssertionError(f"unsupported accumulator {op!r}")
        for field, e in add.items():
            row[field] = _expr(e, row)
        out.append(row)
    return out


def test_the_group_accumulators_do_what_the_sql_aggregates_did():
    """Runs the REAL `$group`/`$addFields` spec over documents.

    `test_a_group_is_scored_exactly_as_postgres_scored_it` hands `_row()` a
    doc that is already grouped, so it never touched an accumulator, and the
    mutants lived there: `$min`/`$max` swapped between `started` and `ended`
    (14 passed — every span_min negative and every `started` wrong), and
    `"runs": {"$sum": 1}` -> `{"$sum": 2}` (14 passed — count(*) was asserted
    nowhere at all).

    The fixture is built so that every accumulator has something to get wrong:
    it is asymmetric in time (a min/max swap changes two values), it repeats a
    ticker (COUNT DISTINCT 3 vs COUNT 4, so `$push` cannot pass for
    `$addToSet`), it carries a NULL ticker (which `$addToSet` keeps and SQL's
    COUNT does not), it puts one run at EXACTLY the watchdog (so `$gte` cannot
    pass for `$gt`), and its elapsed values are spaced so a slipped percentile
    fraction changes the printed second.
    """
    docs = [
        # cycle A: 5 runs, 2 over the 300 s watchdog, 3 DISTINCT tickers over 4
        # non-null rows (AAPL twice, so COUNT and COUNT DISTINCT differ), + a null
        {"cycle_id": "A", "ticker": "AAPL", "elapsed_ms": 900_000,
         "created_at": dt.datetime(2026, 8, 30, 10, 0, 0)},
        {"cycle_id": "A", "ticker": "AAPL", "elapsed_ms": 2_000,     # the repeat
         "created_at": dt.datetime(2026, 8, 30, 10, 15, 0)},
        {"cycle_id": "A", "ticker": "MSFT", "elapsed_ms": 300_000,   # == watchdog: NOT over
         "created_at": dt.datetime(2026, 8, 30, 10, 5, 0)},
        {"cycle_id": "A", "ticker": "NVDA", "elapsed_ms": 500_000,
         "created_at": dt.datetime(2026, 8, 30, 10, 30, 0)},
        {"cycle_id": "A", "ticker": None, "elapsed_ms": 1_000,
         "created_at": dt.datetime(2026, 8, 30, 9, 45, 0)},         # the EARLIEST
        # cycle B: one run, so a min/max swap cannot hide behind it
        {"cycle_id": "B", "ticker": "TSLA", "elapsed_ms": 5_000,
         "created_at": dt.datetime(2026, 8, 29, 8, 0, 0)},
    ]
    rows = {r["_id"]: r for r in
            _run_group_and_addfields(alr.pipeline(min_tickers=1, limit=0), docs)}

    a = rows["A"]
    assert a["runs"] == 5, "count(*) is one per document"
    assert a["over"] == 2, "count(*) FILTER (WHERE elapsed_ms > 300000), strictly over"
    assert a["tickers"] == 3, (
        "count(DISTINCT ticker): the NULL $addToSet kept comes out, and AAPL "
        "counts once — $push here would say 4")
    assert a["started"] == dt.datetime(2026, 8, 30, 9, 45, 0), "started is min(created_at)"
    assert a["ended"] == dt.datetime(2026, 8, 30, 10, 30, 0), "ended is max(created_at)"
    assert a["started"] < a["ended"], "a swap makes every span_min negative"
    assert sorted(a["elapsed"]) == [1_000, 2_000, 300_000, 500_000, 900_000]
    assert rows["B"]["runs"] == 1 and rows["B"]["tickers"] == 1

    # …and the same numbers, once through the Python arithmetic.
    scored = alr._row(a)
    assert (scored["runs"], scored["tickers"]) == (5, 3)
    assert scored["pct_over"] == 40.0                    # 2 of 5
    assert scored["span_min"] == 45.0                    # 09:45 -> 10:30
    assert scored["started"].isoformat() == "2026-08-30T09:45:00+00:00"
    # the two percentiles the SQL named, at the FRACTIONS it named them at.
    # The elapsed values are spaced so that a slipped fraction changes the
    # printed number: p50 = 300.0 where p60 would be 380.0, and p90 = 740.0
    # (pos 3.6 -> 500000 + 0.6*(900000-500000)) where p95 would be 820.0. So
    # this pins 0.5 and 0.9 themselves, not merely the interpolation rule.
    assert scored["median_s"] == 300.0
    assert scored["p90_s"] == 740.0
    assert scored["max_s"] == 900.0


def test_the_watchdog_filter_counts_runs_strictly_over_300s():
    stages = alr.pipeline()
    group = next(s["$group"] for s in stages if "$group" in s)
    assert group["over"] == {
        "$sum": {"$cond": [{"$gt": ["$elapsed_ms", 300_000]}, 1, 0]}}
    # count(DISTINCT ticker) drops NULLs; $addToSet keeps them.
    assert any("$filter" in str(s) and "tickers" in s.get("$addFields", {})
               for s in stages)


def test_elapsed_ms_is_not_null_excludes_missing_as_well_as_null():
    assert alr._match()["elapsed_ms"] == {"$ne": None}


def test_the_days_window_is_a_created_at_bound():
    """`--days 2` printing nothing is what the Postgres reader did once the
    archive froze. If the window silently stopped being applied the report
    would go the other way and pass off every July cycle as 'the last 2 days'."""
    clauses = alr._match(days=2)["$and"]
    bound = next(c["created_at"]["$gt"] for c in clauses if "created_at" in c)
    assert bound.tzinfo is not None, "an aware UTC bound, as NOW() was"
    age = dt.datetime.now(dt.timezone.utc) - bound
    assert dt.timedelta(days=2) - dt.timedelta(seconds=5) <= age <= dt.timedelta(days=2, seconds=5)


def test_round1_is_postgres_half_up_not_python_half_even():
    assert alr._round1(Decimal("36.25")) == 36.3
    assert round(36.25, 1) == 36.2          # what the built-in would have given
    assert alr._round1(Decimal("36.35")) == 36.4
    assert alr._round1(None) is None


@pytest.mark.parametrize("values,p,expected", [
    ([1, 2, 3, 4], 0.5, 2.5),               # interpolates, does not pick
    ([1, 2, 3], 0.5, 2.0),
    ([1, 2, 3, 4], 0.9, 3.7),               # pos = 2.7 -> 3 + 0.7*(4-3)
    ([5], 0.9, 5.0),
])
def test_percentile_cont_interpolates_like_postgres(values, p, expected):
    assert alr._percentile_cont(values, p) == pytest.approx(expected)


def test_a_group_is_scored_exactly_as_postgres_scored_it(monkeypatch):
    """The whole row, against numbers checked on the live PostgreSQL 16 archive.

    29 runs at 400 s and 51 at 100 s is 36.25% over the watchdog — the one
    value where Postgres' half-up `round(numeric, 1)` (36.3) and Python's
    half-even `round()` (36.2) disagree, and 36.2 vs 36.3 straddles nothing
    less than chapter 34's published band. Postgres was asked directly:

        SELECT round(100.0 * count(*) FILTER (WHERE e > 300000)
                     / nullif(count(*),0), 1), ...
        FROM unnest(ARRAY[400000 x29, 100000 x51]) AS e
        -> (80, 36.3, 100.0, 400.0, 400.0)
    """
    elapsed = [400_000] * 29 + [100_000] * 51
    doc = {
        "_id": "cycle-v3-fixture",
        "tickers": 7,
        "runs": len(elapsed),
        "over": sum(1 for e in elapsed if e > alr.WATCHDOG_MS),
        "elapsed": list(reversed(elapsed)),   # unsorted, as $push emits it
        "started": dt.datetime(2026, 8, 9, 18, 10, 0),
        "ended": dt.datetime(2026, 8, 9, 19, 40, 30),
    }
    monkeypatch.setattr(alr.mongo_store, "aggregate", lambda *a, **k: [doc])

    row = alr.fetch(min_tickers=6)[0]
    assert (row["runs"], row["pct_over"], row["median_s"], row["p90_s"],
            row["max_s"], row["span_min"]) == (80, 36.3, 100.0, 400.0, 400.0, 90.5)

    # BSON stores naive UTC; Postgres handed back an aware timestamp, and
    # to_json()'s "+00:00" suffix is part of the output contract.
    assert alr.to_json([row])[0]["started"] == "2026-08-09T18:10:00+00:00"


def test_limit_zero_returns_nothing_as_sql_did(monkeypatch):
    """`LIMIT 0` is a legal SQL query that returns no rows; `{"$limit": 0}` is a
    Mongo error, so the guard has to be here rather than in the pipeline."""
    monkeypatch.setattr(alr.mongo_store, "aggregate",
                        lambda *a, **k: pytest.fail("no query should be sent"))
    assert alr.fetch(limit=0) == []
    with pytest.raises(ValueError):
        alr.fetch(limit=-1)
