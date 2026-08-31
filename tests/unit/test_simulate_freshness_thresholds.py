"""`scripts/simulate_freshness_thresholds.py` reads MongoDB, and reads it right.

WHY THIS FILE EXISTS
--------------------
The script measures how many tickers each candidate freshness threshold would
gate. Until 2026-08-30 it measured that against PostgreSQL, which stopped
taking writes at the 2026-08-19 cutover and has answered every question with
July/August data ever since -- without erroring. Run on 2026-08-30 the old
version printed a healthy-looking

    population=active universe=61 tickers, 76 sessions 2026-05-01 .. 2026-08-19
    ... 0 tickers blocked by every candidate rule

for a store the pipeline had not read in eleven days. A freshness instrument
reading a frozen archive is the one failure it cannot survive, and nothing
about its output said so.

EVERY TEST HERE WOULD HAVE FAILED BEFORE THE PORT
-------------------------------------------------
* the module used to do `import psycopg` and `DSN = os.environ["SIM_DSN"]` at
  import time, so `test_imports_with_no_sql_dsn_in_the_environment` raised
  `KeyError: 'SIM_DSN'` before it could assert anything;
* `test_no_sql_coupling_remains` matched `psycopg` on line 26 of the old file;
* the read helpers this file drives (`load_sessions`, `load_bar_dates`) did not
  exist -- the reads were three SQL strings executed on a psycopg connection,
  so patching `mongo_store` left them pointed at Postgres and the assertions
  below could not even be expressed.

The store is patched, never contacted: `tests/conftest.py::block_production_mongo`
fails any test that opens the real client, and the fake below runs the
pipelines the script actually builds through the production
`date_fields.coerce_pipeline` seam.

THE TWO GUARDS ADDED 2026-08-30 AFTER REVIEW
--------------------------------------------
The first cut of this file was 9 tests and passed, but mutation testing
against a real-copy mirror of the tree showed two whole classes it could not
fail on, both of them the parts no tool checks either:

* THE POPULATION LOADERS. The fake `distinct_values` asserted the COLLECTION
  and threw the QUERY away, and only `active` was ever driven. Widening
  `active` to `status IN ('active','paused')` -- reporting 150 tickers as the
  63 a trade gate would act on -- stayed 9-green, and so did repointing
  `sp500` at `watchlist` with `{"sp500": {"$ne": None}}`. These three SELECTs
  are the only statements `sql_to_mongo` accepted AND the only ones
  `verify_translations.py` refuses to judge (every inventory site for this
  file is kind="dynamic"), so a test is the sole oracle they have.
  Now: `test_every_population_asks_the_question_the_sql_asked` pins all four
  (collection, field, query) triples and drives all four loaders.

* THE DOCUMENTED INVOCATION. The port introduced an `app.db` import without
  the `sys.path` bootstrap, so the docstring's own
  `python scripts/simulate_freshness_thresholds.py active` died at import from
  the repo root while every test here -- run under pytest, which puts the repo
  root on the path -- passed.
  Now: `test_the_docstring_invocation_runs_from_the_repo_root` runs the file
  by path in a subprocess with PYTHONPATH stripped.
"""

from __future__ import annotations

import io
import json
import os
import re
import subprocess
import sys
from contextlib import redirect_stdout
from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.db import date_fields
import scripts.simulate_freshness_thresholds as sim

SCRIPT = Path(sim.__file__)


# ── a very small aggregation engine ──────────────────────────────────
#
# Only the four stages the script uses, so the assertions below are about the
# PIPELINE the script builds rather than about a mock's return value. It runs
# the pipeline through the real `date_fields.coerce_pipeline` first, which is
# what turns the script's `datetime.date` bound into the naive-midnight
# datetime the collection stores -- the step that decides whether the leading
# `$match` matches anything at all.
#
# `test_the_fake_engine_can_tell_the_two_pipelines_apart` is the negative
# control: an engine that cannot distinguish "count rows" from "count distinct
# tickers" would pass every assertion here while checking nothing.

def _resolve(doc, expr):
    if isinstance(expr, str) and expr.startswith("$"):
        cur = doc
        for part in expr[1:].split("."):
            cur = cur.get(part) if isinstance(cur, dict) else None
        return cur
    if isinstance(expr, dict):
        return {k: _resolve(doc, v) for k, v in expr.items()}
    return expr


def _matches(doc, cond) -> bool:
    for field, want in cond.items():
        val = _resolve(doc, "$" + field)
        if isinstance(want, dict):
            for op, arg in want.items():
                if op == "$gte" and not (val is not None and val >= arg):
                    return False
                if op == "$lte" and not (val is not None and val <= arg):
                    return False
                if op == "$ne" and val == arg:
                    return False
                if op == "$in" and val not in arg:
                    return False
                if op not in ("$gte", "$lte", "$ne", "$in"):
                    raise AssertionError(f"fake engine cannot do {op}")
        elif val != want:
            return False
    return True


def _run_pipeline(docs, collection, pipeline):
    docs = list(docs)
    for stage in date_fields.coerce_pipeline(collection, pipeline):
        (op, spec), = stage.items()
        if op == "$match":
            docs = [d for d in docs if _matches(d, spec)]
        elif op == "$group":
            buckets = {}
            for d in docs:
                key = _resolve(d, spec["_id"])
                hkey = tuple(sorted(key.items())) if isinstance(key, dict) else key
                acc = buckets.setdefault(hkey, {"_id": key})
                for name, agg in spec.items():
                    if name == "_id":
                        continue
                    assert agg == {"$sum": 1}, f"fake engine cannot do {agg}"
                    acc[name] = acc.get(name, 0) + 1
            docs = list(buckets.values())
        elif op == "$sort":
            for field, direction in reversed(list(spec.items())):
                docs.sort(key=lambda d: _resolve(d, "$" + field),
                          reverse=direction < 0)
        else:
            raise AssertionError(f"fake engine cannot do {op}")
    return docs


# ── the store ────────────────────────────────────────────────────────
#
# One trading day the market was open with 100 tickers, one with only 60 --
# but printed by BOTH vendors, so it carries 120 ROWS. One weekend day that
# exists only because the synthetic `world_simulator` vendor wrote it. One
# ticker (STALE) whose newest real bar is a week behind everyone else's.

_D1 = datetime(2026, 6, 1)    # Monday, 100 tickers  -> a session
_D2 = datetime(2026, 6, 2)    # Tuesday,  60 tickers x 2 vendors = 120 rows
_D3 = datetime(2026, 6, 8)    # Monday, 100 tickers  -> a session
_WEEKEND = datetime(2026, 6, 6)   # Saturday, world_simulator only

_BROAD = [f"T{i:03d}" for i in range(100)]
_NARROW = _BROAD[:60]


def _price_history() -> list[dict]:
    docs = []
    for t in _BROAD:
        docs.append({"ticker": t, "date": _D1, "source": "yfinance"})
        docs.append({"ticker": t, "date": _D3, "source": "yfinance"})
    for t in _NARROW:                       # the dual-vendor, 60-ticker day
        docs.append({"ticker": t, "date": _D2, "source": "yfinance"})
        docs.append({"ticker": t, "date": _D2, "source": "polygon"})
    # STALE: real data stops at _D1; the only later "bar" is synthetic.
    docs.append({"ticker": "STALE", "date": _D1, "source": "yfinance"})
    docs.append({"ticker": "STALE", "date": _WEEKEND, "source": "world_simulator"})
    return docs


# ── the four population reads, keyed by the QUESTION they ask ────────
#
# The first version of this fixture faked `distinct_values` as
#
#     assert collection == "watchlist" and field == "ticker"
#     return ["T000", "T001", "STALE"]
#
# -- it threw the QUERY away, and only the `active` loader was ever driven.
# That made all four population loaders unfalsifiable, which matters more here
# than anywhere else in the file: these three SELECTs are the only statements
# `sql_to_mongo.translate` ACCEPTED, and `verify_translations.py` judges none
# of them (all three inventory sites for this script are kind="dynamic", so the
# oracle prints "nothing for this oracle to judge here"). Measured against a
# real-copy mirror of the tree on 2026-08-30, two mutations left the suite
# 9-green:
#
#   * `active` asking {"status": {"$in": ["active","paused"]}} -- reporting the
#     whole 150-ticker watchlist as the 63-ticker "active" population;
#   * `sp500` repointed at `watchlist` with {"sp500": {"$ne": None}} -- a
#     literal instance of trap 3, since $ne:None matches neither a null nor a
#     missing field.
#
# So the fake now answers the QUESTION, not the collection: a loader that asks
# anything but one of these four gets an AssertionError naming what it asked.
_POPULATION_READS = {
    ("watchlist", "ticker", '{"status": "active"}'):
        ["T000", "T001", "STALE"],
    ("watchlist", "ticker", '{"status": {"$in": ["active", "paused"]}}'):
        ["T000", "T001", "STALE", "PAUSED"],
    ("ticker_metadata", "ticker", '{"sp500": true}'):
        ["T000", "T001"],
    ("price_history", "ticker", '{"source": {"$ne": "world_simulator"}}'):
        _BROAD + ["STALE"],
}


@pytest.fixture
def store(monkeypatch):
    docs = _price_history()
    aggregates = []
    distincts = []

    def _aggregate(collection, pipeline, session=None):
        aggregates.append((collection, pipeline))
        assert collection == "price_history"
        return _run_pipeline(docs, collection, pipeline)

    def _distinct_values(collection, field, query=None):
        asked = (collection, field, json.dumps(query, sort_keys=True, default=str))
        distincts.append(asked)
        if asked not in _POPULATION_READS:
            raise AssertionError(
                "a population loader asked a question the SQL never asked:\n"
                f"  asked:  {asked}\n  allowed:\n    "
                + "\n    ".join(str(k) for k in _POPULATION_READS))
        return list(_POPULATION_READS[asked])

    monkeypatch.setattr(sim.mongo_store, "aggregate", _aggregate)
    monkeypatch.setattr(sim.mongo_store, "distinct_values", _distinct_values)
    return SimpleNamespace(aggregates=aggregates, distincts=distincts)


# ── the Postgres coupling is gone ────────────────────────────────────

def test_no_sql_coupling_remains():
    """The grep the port has to survive. Line 26 of the old file was
    `import psycopg`."""
    text = SCRIPT.read_text(encoding="utf-8")
    offenders = [
        (n, line) for n, line in enumerate(text.splitlines(), 1)
        if re.search(r"psycopg|DATABASE_URL|pg_connection|dbname=|postgres", line)
    ]
    assert offenders == [], offenders


def test_imports_with_no_sql_dsn_in_the_environment(monkeypatch):
    """`DSN = os.environ["SIM_DSN"]` used to run at import time."""
    monkeypatch.delenv("SIM_DSN", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    sys.modules.pop("scripts.simulate_freshness_thresholds", None)
    import importlib
    mod = importlib.import_module("scripts.simulate_freshness_thresholds")
    assert set(mod._POPULATIONS) == {"active", "watchlist", "sp500", "all"}


# ── the reads say what the SQL said ──────────────────────────────────

def test_a_session_needs_100_distinct_tickers_not_100_rows(store):
    """`COUNT(DISTINCT ticker) >= 100`, not `COUNT(*) >= 100`.

    price_history's key is (ticker, date, source), so the 60-ticker day here
    carries 120 rows. Counting rows would promote it to a trading session and
    silently add a day to every trading-day age measured across it.
    """
    sessions = sim.load_sessions(date(2026, 5, 1))
    assert sessions == [_D1.date(), _D3.date()]
    assert _D2.date() not in sessions


def test_sessions_come_back_as_dates_not_datetimes(store):
    """The column was a PG `date` and the report prints it in a 12-wide field.
    A `datetime` prints as '2026-06-01 00:00:00' and shifts the whole table."""
    for d in sim.load_sessions(date(2026, 5, 1)):
        assert type(d) is date


def test_the_synthetic_vendor_cannot_make_a_ticker_look_fresh(store):
    """`world_simulator` writes only days the market was shut -- measured
    2026-08-30, all 58 of its rows land on a Saturday, Sunday or holiday. An
    unfiltered read counts them as coverage, which is the one thing a
    freshness measurement must not do."""
    bars = sim.load_bar_dates(["STALE"], date(2026, 5, 1))
    assert bars["STALE"] == [_D1.date()]
    assert _WEEKEND.date() not in bars["STALE"]

    leading = [p[0]["$match"] for _, p in store.aggregates]
    assert all(m.get("source") == {"$ne": "world_simulator"} for m in leading), leading


def test_bar_dates_are_deduplicated_across_vendors(store):
    """Two vendor prints of one ticker-day are one day of coverage."""
    bars = sim.load_bar_dates(_NARROW[:1], date(2026, 5, 1))
    assert bars["T000"] == [_D1.date(), _D2.date(), _D3.date()]


def test_the_fake_engine_can_tell_the_two_pipelines_apart():
    """Negative control.

    If the engine above collapsed duplicates on its own, every assertion in
    this file would pass against a script that counted rows. Given the WRONG
    pipeline it must produce the WRONG answer.
    """
    row_counting = [
        {"$match": {"date": {"$gte": date(2026, 5, 1)}}},
        {"$group": {"_id": "$date", "n": {"$sum": 1}}},
        {"$match": {"n": {"$gte": 100}}},
        {"$sort": {"_id": 1}},
    ]
    got = [r["_id"] for r in _run_pipeline(_price_history(), "price_history", row_counting)]
    assert _D2 in got, "the fake engine cannot see the duplicate-row trap"
    assert got != [_D1, _D3]


# ── the four populations ─────────────────────────────────────────────

def test_every_population_asks_the_question_the_sql_asked(store):
    """The four population SELECTs, pinned by (collection, field, query).

    This is the ONLY oracle these statements have. `sql_to_mongo.translate`
    accepted all three of them and `verify_translations.py` judges none --
    every inventory site for this file is kind="dynamic", so the oracle
    reports "no mechanical SELECTs match". The old fixture discarded the query
    and drove only `active`, so a loader could quietly widen its filter or
    change collections and stay green.

    The SQL each line has to still mean:
        active     SELECT ticker FROM watchlist WHERE status = 'active'
        watchlist  SELECT ticker FROM watchlist WHERE status IN ('active','paused')
        sp500      SELECT ticker FROM ticker_metadata WHERE sp500 = TRUE
        all        SELECT DISTINCT ticker FROM price_history
                   ...plus the deliberate `source <> 'world_simulator'`, which
                   the SQL did not have. Measured 2026-08-30 it drops nothing
                   (2895 distinct tickers filtered and unfiltered alike) --
                   it is there so a store that later holds a synthetic-only
                   ticker cannot smuggle one into the universe.
    """
    assert sim.load_universe("active") == ["STALE", "T000", "T001"]
    assert sim.load_universe("watchlist") == ["PAUSED", "STALE", "T000", "T001"]
    assert sim.load_universe("sp500") == ["T000", "T001"]
    assert sim.load_universe("all") == sorted(set(_BROAD) | {"STALE"})

    assert store.distincts == [
        ("watchlist", "ticker", '{"status": "active"}'),
        ("watchlist", "ticker", '{"status": {"$in": ["active", "paused"]}}'),
        ("ticker_metadata", "ticker", '{"sp500": true}'),
        ("price_history", "ticker", '{"source": {"$ne": "world_simulator"}}'),
    ]


def test_active_is_narrower_than_watchlist(store):
    """`active` is the population a trade gate would act on; `watchlist` adds
    the paused names. Live on 2026-08-30 that is 63 vs 150 tickers -- a loader
    that reported 150 of them as tradeable would read as a healthy table."""
    active = sim.load_universe("active")
    watchlist = sim.load_universe("watchlist")
    assert set(active) < set(watchlist)


def test_the_population_argument_selects_the_loader(store):
    """The CLI arg has to reach the loader, not just the header string."""
    buf = io.StringIO()
    with redirect_stdout(buf):
        sim.main(["sp500"])
    assert "population=sp500 universe=2 tickers" in buf.getvalue()
    assert ("ticker_metadata", "ticker", '{"sp500": true}') in store.distincts


# ── the documented invocation actually runs ──────────────────────────

def test_the_docstring_invocation_runs_from_the_repo_root():
    """`python scripts/simulate_freshness_thresholds.py <population>`.

    The SQL version imported no `app` module, so its documented invocation ran
    standalone. The port added `from app.db import mongo_store`, and without
    the `sys.path.insert` bootstrap that 34 other scripts in scripts/ carry
    (and that all seven other files in this porting batch carry), running the
    file BY PATH from the repo root died at import with
    `ModuleNotFoundError: No module named 'app'`. Only `python -m scripts...`
    or an exported PYTHONPATH worked; the container hid it because the
    Dockerfile sets `ENV PYTHONPATH=/app`.

    Driven with a bogus population so the process exits at the argument check
    -- BEFORE any read -- which keeps this test off the network entirely.
    """
    repo_root = SCRIPT.parent.parent
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), "__not_a_population__"],
        cwd=repo_root, env=env, capture_output=True, text=True, timeout=120)

    assert "ModuleNotFoundError" not in proc.stderr, proc.stderr
    assert "population must be one of" in proc.stderr, proc.stderr
    assert proc.returncode == 1, (proc.returncode, proc.stdout, proc.stderr)


# ── the whole report ─────────────────────────────────────────────────

def test_main_reports_the_stale_ticker_against_the_real_sessions(store):
    """End to end on the fake store: STALE's newest real bar is _D1, and _D3 is
    the next session, so as of _D3 it is 1 trading day / 7 calendar days old --
    blocked by `>5cal` but not by `>1trd`. T000 is current on both."""
    buf = io.StringIO()
    with redirect_stdout(buf):
        sim.main(["active"])
    out = buf.getvalue()

    assert "population=active universe=3 tickers, 2 sessions 2026-06-01 .. 2026-06-08" in out
    body = [l for l in out.splitlines() if l.startswith("2026-06-")]
    assert len(body) == 2
    # as-of, dow, n, cal p50/p90, trd p50/p90, >1trd, >2trd, >5cal, >10trd, nodata
    assert body[0].split() == ["2026-06-01", "Mon", "3", "0/0", "0/0", "0", "0", "0", "0", "0"]
    # STALE is 7 CALENDAR days but only 1 TRADING day behind, because 2026-06-02
    # never became a session -- which is the distinction the whole script is for.
    assert body[1].split() == ["2026-06-08", "Mon", "3", "0/7", "0/1", "0", "0", "1", "0", "0"]


def test_an_empty_store_is_an_error_not_a_pretty_table(monkeypatch):
    """A script that compiles, runs and prints a well-formatted nothing is the
    exact failure the port exists to catch."""
    monkeypatch.setattr(sim.mongo_store, "aggregate", lambda *a, **k: [])
    monkeypatch.setattr(sim.mongo_store, "distinct_values", lambda *a, **k: ["T000"])
    with pytest.raises(SystemExit) as exc:
        sim.main(["active"])
    assert "no trading sessions" in str(exc.value)
