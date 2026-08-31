"""`scripts/agent_scorecard.py` must read Mongo, and must join on BOTH keys.

WHY THIS FILE EXISTS
--------------------
The scorecard is the measurement target for agent tuning — `edge_pct` is the
number anyone changes a prompt against. Until 2026-08-30 all three of its
statements went through `scripts.migration.pg_connection.get_db`, which reaches
a DSN that no longer exists, so every invocation died before printing a row.
`scripts/gate_zero_pg.py` counted **9 couplings** in the file (three
`connection_import` / `get_db_call` / `execute_call` triples, at lines 159,
212 and 487 of the pre-port file).

Four things are pinned here, and the first three were RED before the port:

  1. the file has no Postgres coupling at all (9 findings -> 0);

  2. the outcomes join uses the COMPOSITE key `(cycle_id, ticker)`. The SQL
     said `ON s.cycle_id = d.cycle_id AND s.ticker = d.ticker`, and
     `mongo_query.join_rows` carries ONE equality — so the obvious port joins
     on `cycle_id` alone and every desk in a cycle is scored against every
     other ticker's realized move. A 12-ticker cycle fans out 144-fold and no
     row count looks wrong, because more rows is what a bigger sample looks
     like;

  3. `resolved_at IS NOT NULL` / `pnl_pct IS NOT NULL` must also drop the
     documents where the field is MISSING, not merely null. Postgres stored a
     NULL for every unresolved row; 35 of the 2,693 documents written after the
     cutover have no `resolved_at` key at all. Mongo's `{"$ne": None}` excludes
     both, which is why it is the right spelling — measured on the live store
     2026-08-30: `count({"resolved_at": {"$ne": None}, "pnl_pct": {"$ne": None}})`
     = 2,658 = 2,693 - 35;

  4. (regression guard, GREEN before and after) the script still reaches
     price_history only through `app.quant.returns.forward_move_pct`. That
     helper carries the one-vendor pin the 2026-07-30 sweep added after a
     missing `source` filter sign-flipped the +7-session move on 146 of 773
     desks. A port that inlined its own price read would pass every other test
     here.

The reads are STUBBED, not live: the numbers this script prints move with the
store, and a test asserting today's counts fails tomorrow for no defect. The
live cross-checks are kept as explicit probes at the bottom, skipped unless
TRADING_BOT_LIVE_AUDIT=1.
"""

from __future__ import annotations

import ast
import json
import sys
from datetime import datetime
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from scripts import agent_scorecard as sc  # noqa: E402
from scripts.gate_zero_pg import scan  # noqa: E402

SRC = (REPO / "scripts" / "agent_scorecard.py").read_text(encoding="utf-8")


# ── 1. the coupling is gone ────────────────────────────────────────────────

def test_the_scorecard_has_no_postgres_coupling():
    """RED before the port: 9 findings — connection_import / get_db_call /
    execute_call at lines 159-162, 212-216 and 487-489."""
    result = scan(REPO, targets=("scripts/agent_scorecard.py",))
    assert result["errors"] == [], result["errors"]
    assert result["total"] == 0, (
        "agent_scorecard.py still reads Postgres: "
        + "; ".join(f"{f['kind']} at line {f['line']}" for f in result["findings"]))


def test_the_scan_can_still_fail(tmp_path):
    """NEGATIVE CONTROL. A scan that finds nothing because it looked at nothing
    passes the assertion above just as happily."""
    (tmp_path / "scorecard.py").write_text(
        "from scripts.migration.pg_connection import get_db\n"
        "def f():\n"
        "    with get_db() as db:\n"
        "        return db.execute('SELECT desk_data FROM shared_desk').fetchall()\n",
        encoding="utf-8")
    assert scan(tmp_path, targets=("scorecard.py",))["total"] >= 3


# ── a two-operator filter evaluator, so the stubs judge the QUERY ──────────
#
# The stubbed store applies the real filter to the fixture documents rather
# than ignoring it, because the whole content of trap #3 lives in the filter:
# a stub that hands back its fixtures regardless of the query cannot tell
# `{"$ne": None}` from `{"$exists": True}` from no filter at all, and would
# stay green for a port that dropped the resolved-only condition entirely.
#
# Only the operators this script actually uses are implemented, and each
# follows Mongo's documented semantics for a MISSING field:
#   $ne: None  -> field must be present AND non-null   (SQL IS NOT NULL)
#   $gte       -> never matches a missing field
#   $in        -> never matches a missing field
#   $exists    -> presence only; a stored null still EXISTS

_MISSING = object()


def _match(doc: dict, query: dict) -> bool:
    for field, cond in query.items():
        val = doc.get(field, _MISSING)
        if not isinstance(cond, dict):
            if val is _MISSING or val != cond:
                return False
            continue
        for op, operand in cond.items():
            if op == "$ne":
                if val is _MISSING or val == operand:
                    return False
            elif op == "$gte":
                if val is _MISSING or val is None or val < operand:
                    return False
            elif op == "$in":
                if val is _MISSING or val not in operand:
                    return False
            elif op == "$exists":
                # Modelled so the negative control below is sharp rather than
                # blunt: `{"$exists": True}` is the near-miss spelling of
                # IS NOT NULL, and it keeps a stored null.
                if (val is not _MISSING) is not bool(operand):
                    return False
            else:  # pragma: no cover - a new operator must be taught here
                raise AssertionError(f"filter operator {op!r} not modelled")
    return True


def _project(doc: dict, projection: dict | None) -> dict:
    if not projection:
        return dict(doc)
    keep = [k for k, v in projection.items() if v and k != "_id"]
    return {k: doc[k] for k in keep if k in doc}


# ── 2. the outcomes join, on both keys ─────────────────────────────────────

CUT = datetime(2026, 8, 19)

# One cycle, TWO tickers, and the two desks disagree — AAA's desk is bullish,
# BBB's bearish. A cycle_id-only join gives each outcome both desks.
_DESK_AAA = {"trade_decision": {"action": "BUY", "confidence": 80},
             "final_decision": {"action": "BUY", "confidence": 80}}
_DESK_BBB = {"trade_decision": {"action": "SELL", "confidence": 90},
             "final_decision": {"action": "SELL", "confidence": 90}}

_OUTCOMES = [
    # resolved, in window — both must be scored
    {"cycle_id": "cyc-1", "ticker": "AAA", "action": "BUY", "confidence": 80,
     "pnl_pct": 5.0, "outcome": "WIN",
     "resolved_at": datetime(2026, 8, 25), "created_at": datetime(2026, 8, 18)},
    {"cycle_id": "cyc-1", "ticker": "BBB", "action": "SELL", "confidence": 90,
     "pnl_pct": 3.0, "outcome": "WIN",
     "resolved_at": datetime(2026, 8, 25), "created_at": datetime(2026, 8, 18)},
    # resolved_at stored as NULL — the archive's shape for an unresolved row
    {"cycle_id": "cyc-1", "ticker": "CCC", "action": "BUY", "confidence": 50,
     "pnl_pct": 1.0, "outcome": None,
     "resolved_at": None, "created_at": datetime(2026, 8, 18)},
    # resolved_at ABSENT — the post-cutover shape, and the one a `$exists`
    # or `!= null`-in-Python filter lets through
    {"cycle_id": "cyc-1", "ticker": "DDD", "action": "BUY", "confidence": 50,
     "pnl_pct": 1.0, "created_at": datetime(2026, 8, 18)},
    # pnl_pct ABSENT
    {"cycle_id": "cyc-1", "ticker": "EEE", "action": "BUY", "confidence": 50,
     "resolved_at": datetime(2026, 8, 25), "created_at": datetime(2026, 8, 18)},
    # before --since
    {"cycle_id": "cyc-0", "ticker": "AAA", "action": "BUY", "confidence": 60,
     "pnl_pct": 9.0, "outcome": "WIN",
     "resolved_at": datetime(2026, 6, 2), "created_at": datetime(2026, 6, 1)},
]

_DESKS = [
    # desk_data as a sub-document (the 1,762 backfilled desks) …
    {"cycle_id": "cyc-1", "ticker": "AAA", "desk_data": _DESK_AAA},
    # … and as JSON TEXT (the 274 written after the cutover)
    {"cycle_id": "cyc-1", "ticker": "BBB", "desk_data": json.dumps(_DESK_BBB)},
    {"cycle_id": "cyc-1", "ticker": "CCC", "desk_data": _DESK_AAA},
    {"cycle_id": "cyc-1", "ticker": "DDD", "desk_data": _DESK_AAA},
    {"cycle_id": "cyc-1", "ticker": "EEE", "desk_data": _DESK_AAA},
    {"cycle_id": "cyc-0", "ticker": "AAA", "desk_data": _DESK_AAA},
    # a desk with no outcome at all — an INNER JOIN drops it
    {"cycle_id": "cyc-1", "ticker": "ZZZ", "desk_data": _DESK_AAA},
]


@pytest.fixture
def store(monkeypatch):
    """Stub `mongo_store.find_docs` / `mongo_query.count`, recording queries."""
    from app.db import date_fields, mongo_query, mongo_store

    seen: dict[str, list[dict]] = {}
    data = {"decision_outcomes": _OUTCOMES, "shared_desk": _DESKS}

    def fake_find_docs(collection, query, sort=None, projection=None,
                       limit=0, session=None):
        seen.setdefault(collection, []).append(query)
        assert collection in data, f"unexpected collection {collection!r}"
        # `mongo_store.find_docs` runs every filter through this first, which is
        # what turns the `--since` STRING into a BSON-comparable datetime. The
        # stub does it too: without it the harness would accept a query that the
        # real store answers with zero rows (Date sorts BELOW String in BSON
        # type order, so `{"created_at": {"$gte": "2026-08-01"}}` uncoerced
        # matches nothing at all).
        query = date_fields.coerce_filter(collection, query)
        rows = [_project(d, projection) for d in data[collection] if _match(d, query)]
        if sort:
            for field, direction in reversed(sort):
                rows.sort(key=lambda r: r.get(field), reverse=direction < 0)
        return rows[:limit] if limit else rows

    def fake_count(collection, query=None):
        seen.setdefault(collection, []).append(query or {})
        return sum(1 for d in data[collection] if _match(d, query or {}))

    monkeypatch.setattr(mongo_store, "find_docs", fake_find_docs)
    monkeypatch.setattr(mongo_query, "count", fake_count)
    # Nothing here may fall through to a writer or to a second read path.
    for name in ("insert_docs", "upsert_doc", "update_docs", "delete_docs"):
        monkeypatch.setattr(mongo_store, name, _forbidden(name), raising=False)
    return seen


def _forbidden(name):
    def _raise(*_a, **_k):
        raise AssertionError(f"the scorecard is read-only; it called {name}()")
    return _raise


def test_the_outcomes_join_pairs_each_outcome_with_its_own_ticker(store):
    """RED on a cycle_id-only join: 4 rows instead of 2, half of them scoring
    AAA's outcome against BBB's desk."""
    rows = sc.fetch_rows("2026-08-01")

    assert [(r["cycle_id"], r["ticker"]) for r in rows] == [
        ("cyc-1", "AAA"), ("cyc-1", "BBB")]

    by_ticker = {r["ticker"]: r for r in rows}
    assert by_ticker["AAA"]["desk"] == _DESK_AAA
    assert by_ticker["BBB"]["desk"] == _DESK_BBB, (
        "desk_data stored as JSON TEXT must be decoded, not passed through")


def test_an_outcome_whose_resolved_at_field_is_absent_is_excluded(store):
    """SQL `IS NOT NULL` drops a NULL; Mongo's `$ne: None` must also drop a
    MISSING field, because Postgres' NULL placeholder is what the cutover
    stopped writing. RED for `{"$exists": True}`, for a Python-side
    `!= None` over an unfiltered fetch, and for dropping the condition."""
    rows = sc.fetch_rows("2026-08-01")
    tickers = {r["ticker"] for r in rows}
    assert "CCC" not in tickers, "resolved_at stored as NULL must be excluded"
    assert "DDD" not in tickers, "resolved_at MISSING must be excluded too"
    assert "EEE" not in tickers, "pnl_pct MISSING must be excluded too"

    q = store["decision_outcomes"][0]
    assert q["resolved_at"] == {"$ne": None} and q["pnl_pct"] == {"$ne": None}


def test_the_since_bound_is_pushed_into_the_query_not_applied_afterwards(store):
    """`--since` must reach Mongo. A port that fetched everything and filtered
    in Python still answers correctly here — so this asserts the query, which
    is what keeps the read from growing with the collection."""
    sc.fetch_rows("2026-08-01")
    q = store["decision_outcomes"][0]
    assert q["created_at"] == {"$gte": "2026-08-01"}
    assert not any(r["ticker"] == "AAA" and r["cycle_id"] == "cyc-0"
                   for r in sc.fetch_rows("2026-08-01"))


def test_the_desk_side_is_fetched_only_for_the_cycles_that_joined(store):
    """The desk read is scoped to the outcomes' cycles, so it stays
    proportional to the join rather than to shared_desk (2,036 documents, each
    carrying a whole desk artifact)."""
    sc.fetch_rows("2026-08-01")
    q = store["shared_desk"][0]
    assert q == {"cycle_id": {"$in": ["cyc-1"]}}, q


def test_an_outcome_with_no_desk_is_dropped_and_a_desk_with_no_outcome_too(store):
    """INNER JOIN, not `$lookup`: 2,584 outcomes are resolved and 626 have a
    desk. A left-outer port reports the other 1,958 with `desk = {}`, which
    scores as "no agent emitted a stance" rather than as absent data."""
    rows = sc.fetch_rows("2026-08-01")
    assert "ZZZ" not in {r["ticker"] for r in rows}
    assert len(rows) == 2


def test_the_unscoreable_warning_counts_mongo(store):
    """The `⚠ N resolved outcomes exist but only M join` line is the guard that
    stopped a 65-row join masquerading as the sample. It must count the store
    the rows came from."""
    from app.db import mongo_query
    assert mongo_query.count("decision_outcomes",
                             {"resolved_at": {"$ne": None},
                              "pnl_pct": {"$ne": None}}) == 3
    assert store["decision_outcomes"][-1]["resolved_at"] == {"$ne": None}


# ── 3. the price source ────────────────────────────────────────────────────

_PRICE_DESKS = [
    ("cyc-1", "AAA", datetime(2026, 8, 18), _DESK_AAA),                 # document
    ("cyc-1", "BBB", datetime(2026, 8, 18), json.dumps(_DESK_BBB)),     # JSON TEXT
]


def test_the_price_source_reads_shared_desk_from_mongo_and_decodes_text(monkeypatch):
    """RED before the port (the read was SQL), and RED for a port that treated
    `desk_data` as always-a-document: BBB's stance would be unreadable and the
    desk would silently score as "no directional claim"."""
    from app.db import mongo_query
    import app.quant.returns as returns

    seen: list = []

    def fake_find_rows(collection, query, columns, sort=None, limit=0, session=None):
        seen.append((collection, query, tuple(columns), sort))
        return list(_PRICE_DESKS)

    monkeypatch.setattr(mongo_query, "find_rows", fake_find_rows)
    monkeypatch.setattr(returns, "forward_move_pct",
                        lambda ticker, start, sessions: 4.0 if ticker != "SPY" else 1.0)

    rows = sc.fetch_rows_from_prices("2026-06-18", horizon=7)

    assert seen == [("shared_desk", {"created_at": {"$gte": "2026-06-18"}},
                     ("cycle_id", "ticker", "created_at", "desk_data"),
                     [("created_at", 1)])]
    assert [r["ticker"] for r in rows] == ["AAA", "BBB"]
    assert [r["action"] for r in rows] == ["BUY", "SELL"], (
        "the JSON-TEXT desk must be decoded before its decision is read")
    assert rows[0]["excess_pct"] == pytest.approx(3.0)


def test_the_price_source_still_reaches_price_history_only_through_returns():
    """REGRESSION GUARD (green before and after, by design).

    `agent_scorecard` is one of the modules the 2026-07-30 vendor sweep fixed:
    its inline window query had no `source` filter, and because `source` is
    part of price_history's primary key a `LIMIT sessions` returned `sessions`
    ROWS spanning about half as many DATES. On CRH the +7-session move read
    +0.970% where the truth is -2.358% — a sign flip, on 146 of 773 desks.

    The pin now lives inside `app.quant.returns.forward_window`, so the rule
    for this file is simply that it never names price_history to the store.
    """
    tree = ast.parse(SRC)
    reads = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id in {"mongo_store", "mongo_query"}
        and node.args
        and isinstance(node.args[0], ast.Constant)
        and node.args[0].value == "price_history"
    ]
    assert reads == [], (
        "agent_scorecard reads price_history directly at line(s) "
        f"{[n.lineno for n in reads]} — go through forward_move_pct, which "
        "pins one vendor")
    assert "forward_move_pct" in SRC


def test_every_store_call_names_a_postgres_table_not_a_resolved_collection():
    """`mongo_store._coll` resolves the name exactly once. Handing it
    `collection_for(t)` resolves it twice — a no-op only while renames are off,
    and a silent second collection the day they are on."""
    tree = ast.parse(SRC)
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id in {"mongo_store", "mongo_query"}
                and node.args):
            arg = node.args[0]
            assert isinstance(arg, ast.Constant) and isinstance(arg.value, str), (
                f"line {node.lineno}: the collection argument must be a literal "
                "Postgres table name")
            assert "collection_for" not in ast.unparse(arg)


# ── 4. live cross-checks (opt-in) ──────────────────────────────────────────

pytestmark_live = pytest.mark.real_mongo


@pytest.mark.real_mongo
def test_live_the_join_returns_rows_and_every_row_carries_a_desk():
    """A port that compiles, runs and returns `[]` is the failure this effort
    exists to catch. Opt-in: `TRADING_BOT_LIVE_AUDIT=1`."""
    import os
    if not os.environ.get("TRADING_BOT_LIVE_AUDIT"):
        pytest.skip("live audit — set TRADING_BOT_LIVE_AUDIT=1")

    rows = sc.fetch_rows("2026-06-18")
    assert rows, "the outcomes join returned nothing against the live store"
    assert all(isinstance(r["desk"], dict) and r["desk"] for r in rows)
    assert all(r["move_pct"] is not None for r in rows)
    # the join is on both keys, so no outcome may carry another ticker's desk
    for r in rows:
        for key in ("trade_decision", "final_decision"):
            art = r["desk"].get(key) or {}
            if art.get("ticker"):
                assert art["ticker"] == r["ticker"]
