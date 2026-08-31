"""`grade_regime_calls.py` must read Mongo, read it ONE DAY AT A TIME, and
survive being run the way its own docstring says to run it.

WHY EACH TEST HERE WOULD HAVE BEEN RED BEFORE THE PORT
------------------------------------------------------
  test_the_script_has_no_postgres_coupling
        the pre-port file imported `scripts.migration.pg_connection.get_db`
        twice (lines 34 and 66) and ran both statements against the archive.
        Since `settings.DATABASE_URL` was removed it did not even answer stale
        numbers — `python scripts/grade_regime_calls.py` died with
        `AttributeError: 'Settings' object has no attribute 'DATABASE_URL'`.

  test_the_script_still_runs_the_way_its_docstring_says_to_run_it
        the port ADDED a module-scope `sys.path.insert(...)` above
        `from app.db import ...`; the original only had one inside the
        `__main__` block, below the (lazy, in-function) pg imports. Every
        other test in this file reaches the module through
        `importlib.import_module` with the repo root already on `sys.path`, so
        none of them can see the documented invocation. This one shells out:
        `app` is NOT installed in the venv (verified: `import app` from /tmp
        raises ModuleNotFoundError, and the only .pth is the editable
        `lazycat` SDK), so with the bootstrap moved back where the original
        had it the script exits 1 with zero stdout.

  test_load_closes_collapses_one_close_per_trading_day
  test_the_reader_and_not_the_store_puts_the_days_in_order
  test_the_collapse_keeps_the_first_print_the_archive_kept
  test_the_symbol_filter_keeps_the_volatility_tape_out_of_the_index_series
  test_five_rows_is_not_five_days   <-- the one that matters
        These are red against a MECHANICAL port, not just against the
        Postgres original. `SELECT date, close FROM asset_prices WHERE
        symbol=%s ORDER BY date` was one row per day in Postgres, which held
        the table under PRIMARY KEY (symbol, asset_class, date). Mongo's
        `natural_key` index on those same three fields is NOT unique, and
        `market_regime_collector` writes with `insert_docs` believing a
        duplicate key would be swallowed, so every cycle's re-fetch of the
        trailing window appends: measured 2026-08-30, GSPC is 4,203 documents
        over 203 days and VIX 4,262 over 207, with 33 documents on most days.
        `_move_after` steps five ROWS for five trading days, so the naive port
        lands back on the same afternoon and measures ~0%. It does not crash
        and it does not print an empty table — it prints a full one in which
        every call is graded against a flat tape.

  test_the_since_bound_is_a_datetime_not_a_string
        the original handed `--since` to Postgres as text and Postgres cast
        it. `{"created_at": {"$gte": "2026-07-24"}}` is not a narrower window
        in Mongo, it is an empty one — comparison operators do not cross BSON
        type brackets, and String never compares against Date.

  test_a_bson_date_is_turned_into_a_calendar_day
        `asset_prices.date` was a Postgres `date` and came back as
        `datetime.date`; BSON has no date type so it comes back as
        `datetime.datetime`. `_move_after` compares that element against
        `created_at.date()`, and `datetime >= date` raises TypeError.

  test_desk_data_is_read_in_both_of_the_two_shapes_it_has
        `shared_desk.desk_data` is JSONB in the archive, so the 1,762
        backfilled documents carry a real subdocument, while every write since
        the cutover stores JSON TEXT. In the default `--since 2026-07-24`
        window that is 610 dicts and 198 strings, so a reader that handles
        only one shape silently loses the other half.

WHY THE REST ARE HERE (unchanged behaviour that nothing was holding up)
-----------------------------------------------------------------------
The first version of this file passed while EIGHT mutants of the script
survived. A test that cannot go red is not evidence, so the properties those
mutants sat on are now pinned, and each is pinned NON-VACUOUSLY — every one
of them first shows, inside the test, that the un-pinned spelling produces a
DIFFERENT answer on this fixture:

  the harness itself      `_run_pipeline` used to be kinder than MongoDB: it
                          emitted `$group` results in insertion order and
                          leaned on CPython's stable `list.sort`. Mongo
                          guarantees NEITHER (`$group` output order is
                          undefined; `$sort` is not documented as stable), so
                          the evaluator now scrambles the documents before
                          every `$sort` and scrambles the `$group` output on
                          the way out, both from a fixed seed so a red is
                          reproducible rather than flaky. That is what makes
                          the final `{"$sort": {"_id": 1}}` and the `_id`
                          tie-break observable at all. Confirmed against the
                          live store: with the last stage deleted the real
                          aggregation comes back unsorted.

  the fixture             it used to be built so two of the bugs COULD NOT be
                          seen: the foreign-symbol document had `_id` 9001 and
                          so could never win a `$first` over GSPC's `_id` 1,
                          and there was no NaN close anywhere. The volatility
                          tape now owns the LOWEST `_id` on every shared day
                          (drop `symbol` from the `$match` and VIX wins the
                          index series outright — on the live store that turns
                          203 GSPC days into 230 mixed ones, of which only 4
                          still carry the GSPC close), and a NaN incumbent
                          owns one day.

  the columns             `mongo_query.find_rows` returns TUPLES IN SELECT
                          ORDER — that is the entire reason it exists. The
                          fake now BUILDS its rows from the `columns` argument
                          instead of hard-coding them, so the order is
                          load-bearing here exactly as it is in the store:
                          swap the first two and the dedupe keys on ticker,
                          which on live data reports 205 graded / 10 pending /
                          40 without a call instead of 193 / 13 / 220.

  the VIX deadband        no test ever produced a RISING or FALLING label, so
                          `VIX_DEADBAND_PCT` could be set to 0.0 unnoticed.
                          The fixture's volatility tape now realizes +2.0%
                          (STABLE, and RISING under a 0% band), +10.0%
                          (RISING) and -10.0% (FALLING).

  the exit codes          `return 1` on "No GSPC history" and `return 0` on
                          "no gradeable calls" are both asserted now.

  the dedupe              the old fixture's three cycle_ids were all distinct,
                          so `if cycle_id in seen: continue` was never
                          exercised. There is now a repeated cycle_id.

Numbers above measured against the live stores on 2026-08-30; the two-store
comparison over [2026-07-24, 2026-08-20) found 151 shared cycles with 0
field-by-field mismatches, and the collapsed close series is identical to the
archive on all 196 GSPC days and all 200 VIX days.
"""
from __future__ import annotations

import datetime as dt
import importlib
import io
import json
import os
import random
import re
import subprocess
import sys
from contextlib import redirect_stdout
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

grc = importlib.import_module("scripts.grade_regime_calls")

SOURCE = (REPO / "scripts" / "grade_regime_calls.py").read_text()

# One seed for the whole file: the evaluator below deliberately disorders its
# input, and a disorder that changes run to run turns a real red into a flake.
SEED = 20260830


# ── a pipeline evaluator, not a Mongo imitation ────────────────────────────
# Only the four stages this script uses. Anything else raises rather than
# being quietly ignored, so a pipeline that grows a stage cannot pass here by
# having it skipped.
#
# It is deliberately UNFRIENDLIER than pymongo in the two places where a
# hand-written evaluator is normally kinder:
#   * `$sort` scrambles before sorting. MongoDB does not promise a stable
#     sort, so a pipeline that needs `_id` to break a `date` tie must SAY so.
#   * `$group` scrambles its output. MongoDB does not promise any output
#     order for `$group` at all — verified on the live collection: delete the
#     trailing `{"$sort": {"_id": 1}}` and the result really does come back
#     out of order.
def _scrambled(rng, rows, key=None):
    """`rows` in an order the caller is not allowed to depend on."""
    out = list(rows)
    if len(out) > 1:
        rng.shuffle(out)
        if key is not None and [key(r) for r in out] == sorted(key(r) for r in out):
            out.reverse()          # never hand back an accidentally sorted list
    return out


def _run_pipeline(docs: list[dict], pipeline: list[dict]) -> list[dict]:
    rng = random.Random(SEED)      # per call: order cannot depend on test order
    out = [dict(d) for d in docs]
    for stage in pipeline:
        (op, spec), = stage.items()
        if op == "$match":
            def _ok(d, spec=spec):
                for field, cond in spec.items():
                    v = d.get(field)
                    if isinstance(cond, dict):
                        for c_op, c_val in cond.items():
                            if c_op == "$ne" and v == c_val:
                                return False
                            if c_op not in ("$ne",):
                                raise NotImplementedError(c_op)
                    elif v != cond:
                        return False
                return True
            out = [d for d in out if _ok(d)]
        elif op == "$sort":
            out = _scrambled(rng, out)
            for field, direction in reversed(list(spec.items())):
                out.sort(key=lambda d, f=field: d.get(f), reverse=direction < 0)
        elif op == "$group":
            key_expr = spec["_id"]
            assert key_expr.startswith("$")
            key = key_expr[1:]
            grouped: dict = {}
            for d in out:
                k = d.get(key)
                if k in grouped:
                    continue          # $first keeps the incumbent
                acc = {"_id": k}
                for name, agg in spec.items():
                    if name == "_id":
                        continue
                    (agg_op, agg_field), = agg.items()
                    if agg_op != "$first":
                        raise NotImplementedError(agg_op)
                    acc[name] = d.get(agg_field.lstrip("$"))
                grouped[k] = acc
            out = _scrambled(rng, grouped.values(), key=lambda r: r["_id"])
        else:
            raise NotImplementedError(op)
    return out


DAYS = [dt.datetime(2026, 7, 24) + dt.timedelta(days=i) for i in range(11)]
GSPC_DUPES_PER_DAY = 33
VIX_DUPES_PER_DAY = 3
# The volatility tape, first (archived) print per day. Chosen so the three
# realized labels all appear over a 5-trading-day horizon:
#   day 0 -> day 5   20.00 -> 20.40   = +2.0%   STABLE  (RISING under a 0% band)
#   day 1 -> day 6   20.00 -> 22.00   = +10.0%  RISING
#   day 2 -> day 7   20.00 -> 18.00   = -10.0%  FALLING
VIX_FIRST = [20.0, 20.0, 20.0, 20.0, 20.0, 20.4, 22.0, 18.0, 20.0, 20.0, 20.0]
NULL_DAY = DAYS[0] + dt.timedelta(days=100)
NAN_DAY = DAYS[0] + dt.timedelta(days=101)


def _asset_prices_docs() -> list[dict]:
    """The shape the live collection is in: one archived print per day plus
    later re-fetches that disagree with it, two symbols sharing every day, a
    NULL close and a NaN incumbent.

    VIX is written FIRST so the volatility tape owns the lowest `_id` on every
    shared day. That is what makes the `symbol` filter observable: a reader
    that forgets it does not return "a few extra rows", it returns the VIX
    tape labelled GSPC.
    """
    docs, oid = [], 0
    for i, day in enumerate(DAYS):
        for n in range(VIX_DUPES_PER_DAY):
            oid += 1
            docs.append({"_id": oid, "symbol": "VIX", "asset_class": "volatility",
                         "date": day,
                         "close": VIX_FIRST[i] if n == 0 else VIX_FIRST[i] + 3.0})
    for i, day in enumerate(DAYS):
        first = 100.0 + i                      # what the archive holds
        for n in range(GSPC_DUPES_PER_DAY):
            oid += 1
            docs.append({"_id": oid, "symbol": "GSPC", "asset_class": "index",
                         "date": day, "close": first if n == 0 else first + 0.5})
    # a NULL close the reader must drop, and a NaN incumbent that owns its day
    docs.append({"_id": oid + 1, "symbol": "GSPC", "asset_class": "index",
                 "date": NULL_DAY, "close": None})
    docs.append({"_id": oid + 2, "symbol": "GSPC", "asset_class": "index",
                 "date": NAN_DAY, "close": float("nan")})
    docs.append({"_id": oid + 3, "symbol": "GSPC", "asset_class": "index",
                 "date": NAN_DAY, "close": 999.0})
    # Documents do not reach an aggregation in insertion order, and neither
    # `$sort` nor `$group` here may lean on the order they arrive in.
    random.Random(SEED).shuffle(docs)
    return docs


N_DOCS = 11 * (GSPC_DUPES_PER_DAY + VIX_DUPES_PER_DAY) + 3


@pytest.fixture
def stub_prices(monkeypatch):
    docs = _asset_prices_docs()
    seen: list[tuple] = []

    def fake_aggregate(collection, pipeline, session=None):
        seen.append((collection, pipeline))
        return _run_pipeline(docs, pipeline)

    monkeypatch.setattr(grc.mongo_store, "aggregate", fake_aggregate)
    return docs, seen


def _without(pipeline: list[dict], *, drop_stage: int = None, drop_sort_key: str = None,
             drop_match_key: str = None) -> list[dict]:
    """`pipeline` with one thing taken out — how each test below shows that the
    thing it is pinning actually changes the answer."""
    out = [json.loads(json.dumps(s, default=str)) if False else dict(s) for s in pipeline]
    if drop_stage is not None:
        out.pop(drop_stage)
    if drop_sort_key is not None:
        out = [{"$sort": {k: v for k, v in s["$sort"].items() if k != drop_sort_key}}
               if "$sort" in s and drop_sort_key in s["$sort"] else s for s in out]
    if drop_match_key is not None:
        out = [{"$match": {k: v for k, v in s["$match"].items() if k != drop_match_key}}
               if "$match" in s else s for s in out]
    return out


# ── 1. the coupling is gone ────────────────────────────────────────────────
def test_the_script_has_no_postgres_coupling():
    hits = [(n, line) for n, line in enumerate(SOURCE.splitlines(), 1)
            if re.search(r"psycopg|DATABASE_URL|pg_connection|dbname=|postgres", line)]
    assert hits == [], f"Postgres coupling survives the port: {hits}"


# ── 2. it still runs as a script ───────────────────────────────────────────
def test_the_script_still_runs_the_way_its_docstring_says_to_run_it():
    """`python scripts/grade_regime_calls.py` from the repo root.

    `sys.path[0]` is then `scripts/`, NOT the repo root, and `app` is not
    installed in this venv — so the module-scope `from app.db import ...`
    raises ModuleNotFoundError before `main()` is ever reached and the script
    exits 1 having printed nothing. Every other test here imports the module
    with the repo root already on `sys.path`, so this is the only one that can
    see it. `--help` is enough: the imports run at module load, above argparse.
    """
    # the bootstrap must precede the app import, at module scope (column 0)
    lines = SOURCE.splitlines()
    boot = next(i for i, l in enumerate(lines) if l.startswith("sys.path.insert("))
    app_import = next(i for i, l in enumerate(lines) if l.startswith("from app."))
    assert boot < app_import, (
        "the sys.path bootstrap must run BEFORE `from app.db import ...`; "
        f"bootstrap at line {boot + 1}, import at line {app_import + 1}")

    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    proc = subprocess.run([sys.executable, "scripts/grade_regime_calls.py", "--help"],
                          cwd=str(REPO), env=env, capture_output=True, text=True,
                          timeout=180)
    assert "No module named 'app'" not in proc.stderr, (
        "the repo root is not on sys.path when the script is run as a script:\n"
        + proc.stderr)
    assert proc.returncode == 0, f"exit {proc.returncode}\n{proc.stderr}"
    assert "usage: grade_regime_calls.py" in proc.stdout, proc.stdout


# ── 3. one close per trading day ───────────────────────────────────────────
def test_load_closes_collapses_one_close_per_trading_day(stub_prices):
    docs, seen = stub_prices
    closes = grc._load_closes("GSPC")

    assert len(docs) == N_DOCS
    assert [d for d, _ in closes] == [d.date() for d in DAYS]
    assert len(closes) == 11, (
        f"{len(closes)} rows for 11 trading days — the duplicate prints were "
        "not collapsed, and _move_after counts rows")
    assert seen[0][0] == "asset_prices", (
        "pass the Postgres table name; every helper resolves it exactly once")


def test_the_reader_and_not_the_store_puts_the_days_in_order(stub_prices):
    """`_move_after` finds its start with a linear scan (`next(i for i,(d,_) in
    ... if d >= start_date)`), so an unordered series grades against the wrong
    window silently — no crash, a full and plausible table. `$group` output
    order is undefined in MongoDB, so the trailing `{"$sort": {"_id": 1}}` is
    the only thing making the series ascending."""
    docs, seen = stub_prices
    dates = [d for d, _ in grc._load_closes("GSPC")]
    assert dates == sorted(dates), dates

    # non-vacuity: without that last stage the same pipeline is NOT ascending,
    # here and (verified 2026-08-30) against the live collection too.
    pipeline = seen[0][1]
    assert pipeline[-1] == {"$sort": {"_id": 1}}, pipeline[-1]
    unsorted = [r["_id"] for r in _run_pipeline(docs, _without(pipeline, drop_stage=-1))]
    assert unsorted != sorted(unsorted), (
        "the evaluator handed back a sorted $group anyway — this test would "
        "pass with the $sort deleted, so it is proving nothing")


def test_the_collapse_keeps_the_first_print_the_archive_kept(stub_prices):
    docs, seen = stub_prices
    closes = dict(grc._load_closes("GSPC"))
    # first-by-_id, i.e. what `ON CONFLICT ... DO NOTHING` would have kept, and
    # what Postgres holds: 196/196 GSPC days and 200/200 VIX days on the live
    # stores, against 164 and 154 for last-by-_id.
    assert closes[DAYS[0].date()] == 100.0
    assert closes[DAYS[5].date()] == 105.0
    assert all(v == int(v) for v in closes.values()), (
        "a later intraday re-fetch won the day; the archive print is the "
        "whole-number one in this fixture")

    # non-vacuity: `date` alone does not decide which print wins. Mongo does
    # not promise a stable sort, so dropping the `_id` tie-break really does
    # change the answer — it is not covered by CPython's list.sort.
    pipeline = seen[0][1]
    assert pipeline[1] == {"$sort": {"date": 1, "_id": 1}}, pipeline[1]
    no_tiebreak = {r["_id"].date(): r["close"]
                   for r in _run_pipeline(docs, _without(pipeline, drop_sort_key="_id"))}
    assert no_tiebreak != closes, (
        "without the _id tie-break the same fixture produced the same series, "
        "so this test cannot see which print won")


def test_the_symbol_filter_keeps_the_volatility_tape_out_of_the_index_series(stub_prices):
    """VIX shares every day with GSPC and owns the lower `_id`, so a `$match`
    that forgets `symbol` does not widen the series — it REPLACES it. On the
    live store that is 203 GSPC days becoming 230 mixed ones, of which only 4
    still carry the GSPC close, and agreement with the Postgres archive going
    from 196/196 to 0/196."""
    docs, seen = stub_prices
    gspc = dict(grc._load_closes("GSPC"))
    vix = dict(grc._load_closes("VIX"))

    match = seen[0][1][0]["$match"]
    assert match.get("symbol") == "GSPC", match
    assert gspc[DAYS[0].date()] == 100.0 and vix[DAYS[0].date()] == 20.0

    unfiltered = {r["_id"].date(): r["close"]
                  for r in _run_pipeline(docs, _without(seen[0][1], drop_match_key="symbol"))}
    assert unfiltered != gspc and unfiltered != vix, (
        "dropping `symbol` changed nothing on this fixture — the two symbols "
        "must share days, and the foreign one must be able to win the $first")
    assert unfiltered[DAYS[0].date()] == 20.0


def test_a_null_close_is_dropped_like_the_sqls_is_not_null(stub_prices):
    closes = grc._load_closes("GSPC")
    assert NULL_DAY.date() not in {d for d, _ in closes}


def test_a_nan_incumbent_drops_its_whole_day(stub_prices):
    """NaN survives `close IS NOT NULL` and `{"$ne": None}` alike, and it is
    not a price. It is dropped AFTER the collapse, so the day a NaN incumbent
    owns stays dropped — which is what the archive row for that day did."""
    docs, _ = stub_prices
    nan_docs = [d for d in docs if d["date"] == NAN_DAY]
    assert len(nan_docs) == 2 and min(nan_docs, key=lambda d: d["_id"])["close"] != \
        min(nan_docs, key=lambda d: d["_id"])["close"], (
        "the NaN document must be the one that wins $first, or this test is "
        "about the fallback print instead of the guard")

    closes = grc._load_closes("GSPC")
    assert NAN_DAY.date() not in {d for d, _ in closes}
    assert all(c == c for _, c in closes), [c for _, c in closes if c != c]
    assert 999.0 not in {c for _, c in closes}, (
        "the later print won the day the NaN owned; DO NOTHING keeps the "
        "incumbent, and a NaN incumbent takes the day with it")


def test_a_bson_date_is_turned_into_a_calendar_day(stub_prices):
    closes = grc._load_closes("GSPC")
    assert all(type(d) is dt.date for d, _ in closes), \
        [type(d).__name__ for d, _ in closes][:3]

    # and the conversion is load-bearing, not cosmetic: the raw BSON value
    # cannot be compared against the `created_at.date()` _move_after uses.
    with pytest.raises(TypeError):
        assert DAYS[0] >= dt.date(2026, 7, 24)


# ── 4. the one that matters ────────────────────────────────────────────────
def test_five_rows_is_not_five_days(stub_prices):
    docs, _ = stub_prices
    start = DAYS[0].date()

    ported = grc._load_closes("GSPC")
    real_move = grc._move_after(ported, start, grc.HORIZON_DAYS)

    # exactly what a mechanical port of the SQL produces: every document,
    # ordered by date, no collapse.
    naive = [(d["date"].date(), d["close"])
             for d in sorted((x for x in docs
                              if x["symbol"] == "GSPC" and x["close"] is not None),
                             key=lambda x: (x["date"], x["_id"]))]
    naive_move = grc._move_after(naive, start, grc.HORIZON_DAYS)

    assert real_move == pytest.approx(5.0), real_move
    assert naive_move == pytest.approx(0.5), naive_move

    def label(m):
        return "UP" if m > grc.SPX_DEADBAND_PCT else ("DOWN" if m < -grc.SPX_DEADBAND_PCT else "FLAT")

    assert label(real_move) == "UP"
    assert label(naive_move) == "FLAT", (
        "the uncollapsed read must land inside the deadband — if it does not, "
        "this test is not proving anything about the collapse")


# ── 5. the window bound crosses a BSON type bracket ────────────────────────
def test_the_since_bound_is_a_datetime_not_a_string():
    assert grc._since_bound("2026-07-24") == dt.datetime(2026, 7, 24)
    with pytest.raises(SystemExit):
        grc._since_bound("last tuesday")


# ── 6. end to end ──────────────────────────────────────────────────────────
def _call(spx, vol, conv):
    return {"spx_direction": spx, "vol_direction": vol, "conviction": conv,
            "basis": "fixture"}


def _desk(spx, vol, conv, regime="CONTRADICTORY"):
    return {"regime_classification": {"regime": regime,
                                      "forward_call": _call(spx, vol, conv)}}


def _row(cycle_id, ticker, day_idx, desk, *, as_text=False):
    return {"cycle_id": cycle_id, "ticker": ticker,
            "created_at": DAYS[day_idx] + dt.timedelta(hours=20, minutes=18,
                                                       seconds=37, milliseconds=576),
            "desk_data": json.dumps(desk) if as_text else desk}


# cycle_id and ticker are deliberately NOT interchangeable here: one cycle_id
# repeats under two tickers, and one ticker repeats under two cycle_ids. A
# reader that unpacks `find_rows`' tuple in the wrong order dedupes on the
# wrong one and the counts move.
DESK_ROWS = [
    # backfilled JSONB -> subdocument. day 0: SPX +5.00% UP, VIX +2.00% STABLE
    _row("cycle-archive", "AAPL", 0, _desk("UP", "STABLE", 80.0)),
    # written after the cutover -> JSON TEXT
    _row("cycle-post", "MSFT", 0, _desk("DOWN", "RISING", 30.0), as_text=True),
    # no forward_call at all -> counted, not graded
    _row("cycle-nocall", "NVDA", 0, {"regime_classification": {}}, as_text=True),
    # same cycle_id as the first row, different ticker -> the dedupe drops it
    _row("cycle-archive", "MSFT", 0, _desk("DOWN", "RISING", 10.0)),
    # day 1: SPX +4.95% UP, VIX +10.00% RISING. Same ticker as the first row.
    _row("cycle-rising", "AAPL", 1, _desk("UP", "RISING", 55.0), as_text=True),
    # day 2: SPX +4.90% UP, VIX -10.00% FALLING
    _row("cycle-falling", "TSLA", 2, _desk("FLAT", "FALLING", 45.0)),
    # day 9: the 5-day window has not closed -> pending, never graded
    _row("cycle-pending", "AMZN", 9, _desk("UP", "STABLE", 50.0), as_text=True),
]


def _run_main(monkeypatch, rows, argv):
    captured: dict = {}

    def fake_find_rows(collection, query, columns, sort=None, limit=0, session=None):
        captured.update(collection=collection, query=query, columns=list(columns),
                        sort=sort, limit=limit)
        # BUILT FROM `columns`, never hard-coded: `mongo_query.find_rows`
        # returns tuples in SELECT order and that is the whole contract the
        # positional unpacking at the call site rests on.
        return [tuple(r[c] for c in columns) for r in rows]

    monkeypatch.setattr(grc.mongo_query, "find_rows", fake_find_rows)
    monkeypatch.setattr(sys, "argv", argv)
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = grc.main()
    return rc, buf.getvalue(), captured


def test_desk_data_is_read_in_both_of_the_two_shapes_it_has(stub_prices, monkeypatch, tmp_path):
    out_path = tmp_path / "report.json"
    rc, out, captured = _run_main(
        monkeypatch, DESK_ROWS,
        ["grade_regime_calls.py", "--since", "2026-07-24", "--json", str(out_path)])

    assert rc == 0
    assert captured["collection"] == "shared_desk"
    assert isinstance(captured["query"]["created_at"]["$gte"], dt.datetime), (
        "a string bound against a BSON Date matches nothing")
    assert captured["sort"] == [("created_at", 1)]
    # the tuple contract: the SELECT order IS the unpacking order
    assert captured["columns"] == ["cycle_id", "ticker", "created_at", "desk_data"], (
        "find_rows returns tuples in this order and main() unpacks them "
        "positionally; swap two and the dedupe keys on the wrong column")

    assert "4 graded since 2026-07-24" in out, out
    assert "1 pending, 1 without a call" in out, out
    # the realized 5-day move is +5.00% on day 0, so UP is right and DOWN is
    # wrong — one of each, one from a dict and one from a string.
    assert "SPX direction: 2/4 = 50%" in out, out
    assert "VIX direction: 3/4 = 75%" in out, out
    assert re.search(r"UP\s+UP\s+5\.00% ✓", out), out
    assert re.search(r"DOWN\s+UP\s+5\.00% ✗", out), out
    # every conviction bucket is populated, and one comes from each shape
    assert "high (>=70)    1/1 = 100%" in out, out
    assert "mid (40-69)    1/2 = 50%" in out, out
    assert "low (<40)      0/1 = 0%" in out, out

    report = json.loads(out_path.read_text())
    assert [g["cycle_id"] for g in report["graded"]] == [
        "cycle-archive", "cycle-post", "cycle-rising", "cycle-falling"], (
        "`cycle-archive` appears twice in the read and must be graded once")
    assert report["spx_hit_rate"] == pytest.approx(50.0)
    assert report["vol_hit_rate"] == pytest.approx(75.0)


def test_the_vix_deadband_is_five_percent_not_one(stub_prices, monkeypatch, tmp_path):
    """Volatility moves in percentage terms, so a 1% band would call
    everything a hit and a 0% band would make STABLE unreachable. The three
    graded days realize +2.0%, +10.0% and -10.0%, which is the only
    arrangement that can tell those bands apart."""
    out_path = tmp_path / "report.json"
    rc, _out, _ = _run_main(
        monkeypatch, DESK_ROWS,
        ["grade_regime_calls.py", "--since", "2026-07-24", "--json", str(out_path)])
    assert rc == 0

    graded = {g["cycle_id"]: g for g in json.loads(out_path.read_text())["graded"]}
    assert graded["cycle-archive"]["vix_move_pct"] == pytest.approx(2.0)
    assert graded["cycle-rising"]["vix_move_pct"] == pytest.approx(10.0)
    assert graded["cycle-falling"]["vix_move_pct"] == pytest.approx(-10.0)

    assert grc.VIX_DEADBAND_PCT == 5.0
    assert graded["cycle-archive"]["vol_realized"] == "STABLE", (
        "+2.0% is inside the ±5% volatility deadband; under a narrower band "
        "this is RISING and every VIX call scores against a different tape")
    assert graded["cycle-rising"]["vol_realized"] == "RISING"
    assert graded["cycle-falling"]["vol_realized"] == "FALLING"
    assert graded["cycle-archive"]["spx_realized"] == "UP", "the ±1% SPX band is separate"


def test_no_gspc_history_is_a_nonzero_exit(monkeypatch):
    """`asset_prices` empty (or the symbol filter matching nothing) is a
    failure, not an empty report: the caller has to be able to tell "the tape
    is missing" from "nothing was gradeable yet"."""
    monkeypatch.setattr(grc.mongo_store, "aggregate", lambda *a, **k: [])
    rc, out, _ = _run_main(monkeypatch, DESK_ROWS,
                           ["grade_regime_calls.py", "--since", "2026-07-24"])
    assert rc == 1, "an empty tape must exit non-zero"
    assert "No GSPC history in asset_prices" in out, out


def test_nothing_gradeable_yet_is_a_zero_exit(stub_prices, monkeypatch):
    """The other empty: the tape is fine, the windows just have not closed.
    That is a normal day, and it exits 0."""
    rows = [r for r in DESK_ROWS if r["cycle_id"] in ("cycle-pending", "cycle-nocall")]
    rc, out, _ = _run_main(monkeypatch, rows,
                           ["grade_regime_calls.py", "--since", "2026-07-24"])
    assert rc == 0
    assert "No gradeable regime calls since 2026-07-24" in out, out
    assert "1 still inside the 5-day window" in out, out
    assert "1 cycles with no forward_call" in out, out
