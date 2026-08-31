"""`scripts/decision_score_report.py` reads MongoDB, and its join is a LEFT
JOIN on a COMPOSITE key.

WHY THIS FILE EXISTS
--------------------
Both halves of the reporter reached the frozen Postgres archive through the
migration pool, which since the 2026-08-19 cutover raises AttributeError on a
settings attribute that no longer exists. The script has not produced a number
since — which is the whole reason `decision_scores` reads as "write-only" in
the ch.104 audit: the collection has a writer and exactly ONE reader, and the
reader was dead. The port closes that finding from the reader's end.

WHAT THE ASSERTIONS PIN, AND WHY EACH ONE CAN FAIL
--------------------------------------------------
`mongo_query` has `left_join_rows`, but it joins on ONE equality and this SQL
joins on TWO (`cycle_id` AND `ticker`), so the stitch is hand-written in the
script and every semantic it has to preserve is a thing a hand-written stitch
can get wrong. Each test below was run against a deliberately broken copy of
`_shadow_rows` and observed to FAIL; the mutation that kills it is named in
the test.

The row TUPLES matter as much as the row set: the reporter indexes them
positionally (`r[4]` board_action, `r[5]` board_confidence, `r[6]` pnl_pct),
so a port that returns dicts, or drops the selected-but-unread `risk_reward`
at index 3, reads a different column and still runs.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from scripts import decision_score_report as dsr

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts" / "decision_score_report.py"


# ── fixtures: what the two collections hand back ────────────────────────────
# DOCUMENTS, not tuples, and the stub projects them in the order the caller
# asks for — which is what `find_rows` does. A stub that returns fixed tuples
# whatever the column list says cannot see a SELECT list in the wrong ORDER,
# and swapping `board_action` with `board_confidence` was the one mutation of
# thirteen that survived the first version of this file. A symmetric fixture
# hides a swapped pairing; the projection has to be real.
#
# `board_action`/`board_confidence` are ABSENT on the TSM score, not null —
# that is what a desk that never reached a verdict looks like since the cutover
# dropped the column defaults (129 of 508 rows today, against 44 explicit nulls
# inherited from the archive). `find_rows` returns None for a missing field
# exactly as Postgres returned NULL for the column, so `.get()` is the whole
# point.
SCORES = [
    {"band": "CANDIDATE", "score": 61.0, "baseline_confidence": 70,          # one outcome
     "risk_reward": 3.7, "board_action": "HOLD", "board_confidence": 73,
     "cycle_id": "cyc-1", "ticker": "EXLS"},
    {"band": "AVOID", "score": 44.4, "baseline_confidence": 60,              # TWO outcomes
     "risk_reward": 1.45, "board_action": "BUY", "board_confidence": 70,
     "cycle_id": "cyc-2", "ticker": "DE"},
    {"band": "NEUTRAL", "score": 52.8, "baseline_confidence": 30,            # no outcome
     "risk_reward": None, "board_action": None, "board_confidence": None,
     "cycle_id": "cyc-3", "ticker": "AAPL"},
    {"band": "STRONG_CANDIDATE", "score": 71.5, "baseline_confidence": 72,   # board fields ABSENT
     "risk_reward": 3.82, "cycle_id": "cyc-4", "ticker": "TSM"},
    {"band": "NOT_SCOREABLE", "score": 12.0, "baseline_confidence": 20,      # NULL cycle_id
     "risk_reward": None, "cycle_id": None, "ticker": "AAPL"},
]
OUTCOMES = [
    {"cycle_id": "cyc-1", "ticker": "EXLS", "pnl_pct": 7.78},
    {"cycle_id": "cyc-2", "ticker": "DE", "pnl_pct": 8.02},
    {"cycle_id": "cyc-2", "ticker": "DE", "pnl_pct": -1.10},  # fan-out: 239 such groups live
    {"cycle_id": "cyc-9", "ticker": "EXLS", "pnl_pct": 99.9},  # right ticker, WRONG cycle
    {"cycle_id": "cyc-3", "ticker": "MSFT", "pnl_pct": 88.8},  # right cycle, WRONG ticker
    {"cycle_id": None, "ticker": "AAPL", "pnl_pct": 77.7},     # keyless: NULL = NULL is not true
]


@pytest.fixture
def stub_mongo(monkeypatch):
    """Answer `mongo_query.find_rows` from the documents above, projected in
    the caller's column order, and record what was asked for."""
    from app.db import mongo_query

    asked: list[tuple[str, dict, list]] = []
    docs = {"decision_scores": SCORES, "decision_outcomes": OUTCOMES}

    def fake_find_rows(collection, query, columns, sort=None, limit=0, session=None):
        asked.append((collection, query, list(columns)))
        if collection not in docs:
            raise AssertionError(f"unexpected collection {collection!r}")
        # The real contract: a tuple in `columns` order, None for a field the
        # document does not have.
        return [tuple(d.get(c) for c in columns) for d in docs[collection]]

    monkeypatch.setattr(mongo_query, "find_rows", fake_find_rows)
    return asked


def test_the_select_list_is_the_SQL_select_list_in_order():
    """`_SCORE_COLUMNS` IS the SQL's SELECT list, and order is meaning.

    Pinned literally rather than derived, because every other assertion here
    reads the same constant and would move with it — the shape that lets a
    swapped pairing stay green in a test that looks thorough."""
    assert dsr._SCORE_COLUMNS == ("band", "score", "baseline_confidence",
                                  "risk_reward", "board_action",
                                  "board_confidence")


# ── the join ────────────────────────────────────────────────────────────────

def test_unmatched_left_rows_survive_with_a_null_right_side(stub_mongo):
    """LEFT JOIN, not INNER.

    Breaks if the stitch emits nothing for a left row with no match — which is
    what `join_rows` (the inner one) does, and the substitution is a one-word
    edit. On the live data that mutation would silently drop 166 of 490 rows,
    every one of them a desk that never reached a verdict, and the surviving
    report still looks complete.
    """
    rows = dsr._shadow_rows()
    by_band = {r[0]: r for r in rows}
    assert by_band["NEUTRAL"][6] is None, "unmatched left row lost its NULL right side"
    assert by_band["STRONG_CANDIDATE"][6] is None
    assert len(rows) == 6, f"expected 5 left rows with one fan-out to 2, got {len(rows)}"


def test_the_join_needs_BOTH_key_columns(stub_mongo):
    """The key is (cycle_id, ticker), not either one alone.

    Breaks if the stitch keys on `ticker` only — `cyc-9/EXLS` then joins onto
    the `cyc-1/EXLS` score and the report gains a +99.9% P&L that belongs to a
    different cycle. Breaks the other way (cycle only) on `cyc-3/MSFT`.
    """
    rows = dsr._shadow_rows()
    pnl = {r[0]: r[6] for r in rows if r[0] != "AVOID"}
    assert len(pnl) == 4
    assert pnl["CANDIDATE"] == 7.78, "joined on ticker alone — picked up cyc-9"
    assert pnl["NEUTRAL"] is None, "joined on cycle alone — picked up MSFT"
    assert 99.9 not in [r[6] for r in rows]
    assert 88.8 not in [r[6] for r in rows]


def test_a_left_row_matching_two_right_rows_emits_two_rows(stub_mongo):
    """SQL fan-out. `decision_outcomes` has 239 duplicate (cycle_id, ticker)
    groups today, so this is not hypothetical.

    Breaks if the stitch takes only the first match (`index[k][0]`), which is
    the natural way to write it and quietly changes a row count.
    """
    rows = dsr._shadow_rows()
    avoid = sorted(r[6] for r in rows if r[0] == "AVOID")
    assert avoid == [-1.10, 8.02], f"fan-out collapsed: {avoid}"


def test_two_rows_with_the_same_incomplete_key_do_not_join(stub_mongo):
    """`NULL = NULL` is not true — and this is the case that needs BOTH sides.

    The fixtures hold a `decision_scores` row with a NULL `cycle_id` and a
    `decision_outcomes` row with a NULL `cycle_id`, both on AAPL. As tuples the
    two keys are `(None, "AAPL")` on each side and compare EQUAL in Python, so
    a stitch that indexes them joins them — a cross product of exactly the rows
    SQL excludes, and one no row count would look wrong for. Postgres returns
    the score row unmatched, so pnl_pct must stay None.

    This is `mongo_query._index_by`'s documented trap, reproduced here because
    the composite key cannot reuse that helper. It is the one guard in
    `_shadow_rows`, deliberately: written on both sides instead, each copy is
    redundant and can be deleted with every test still green — verified, that
    mutation was the only one of ten this file did not kill.
    """
    rows = dsr._shadow_rows()
    assert 77.7 not in [r[6] for r in rows]
    orphan = next(r for r in rows if r[0] == "NOT_SCOREABLE")
    assert orphan[6] is None, "a NULL cycle_id joined a NULL cycle_id"


def test_rows_are_tuples_in_the_SQL_select_order(stub_mongo):
    """The reporter reads r[4], r[5], r[6] positionally.

    Breaks if `risk_reward` — selected by the SQL and never read — is dropped
    as dead weight: every later index shifts by one, `undecided` starts
    counting board_confidence and the band/action table reads a number as an
    action. Nothing raises.
    """
    rows = dsr._shadow_rows()
    row = next(r for r in rows if r[0] == "CANDIDATE")
    assert isinstance(row, tuple) and len(row) == 7
    assert row == ("CANDIDATE", 61.0, 70, 3.7, "HOLD", 73, 7.78)


def test_missing_board_fields_read_as_None_not_KeyError(stub_mongo):
    """`attach_board_decision` `$set`s board_action/board_confidence onto the
    row afterwards, so on a desk that never reached a verdict the fields are
    ABSENT — 129 of 508 rows today, against 44 explicit nulls inherited from
    the archive. Postgres returned NULL for both; so must this."""
    rows = dsr._shadow_rows()
    tsm = next(r for r in rows if r[0] == "STRONG_CANDIDATE")
    assert tsm[4] is None and tsm[5] is None


def test_the_score_filter_is_the_faithful_IS_NOT_NULL(stub_mongo):
    """`WHERE ds.score IS NOT NULL`. In Mongo a query for `null` also matches a
    document that LACKS the field, so `$ne: None` excludes both — which is what
    a NULL column was. `{"score": {"$exists": True}}` would let the 18 explicit
    nulls through and `score` would arrive as None at `float()`."""
    dsr._shadow_rows()
    query = next(q for c, q, _ in stub_mongo if c == "decision_scores")
    assert query == {"score": {"$ne": None}}


# ── the universe half ───────────────────────────────────────────────────────

def test_universe_tickers_is_distinct_and_ordered(monkeypatch):
    """`SELECT DISTINCT ticker FROM fundamentals ORDER BY ticker`.

    `sql_to_mongo` refuses SELECT DISTINCT, so this is hand-written and both
    halves of it can be dropped. Unsorted output would reorder every band the
    report prints; a duplicate would score the same ticker twice and shift
    every percentage. The blank is dropped rather than sorted against strings
    (`sorted` would raise TypeError on a None).
    """
    from app.db import mongo_store

    monkeypatch.setattr(mongo_store, "distinct_values",
                        lambda coll, field, query=None: (
                            ["MSFT", "AAPL", "MSFT", "", None, "TSM"]
                            if (coll, field) == ("fundamentals", "ticker")
                            else pytest.fail(f"read {coll}.{field}")))
    assert dsr._universe_tickers() == ["AAPL", "MSFT", "TSM"]


# ── the couplings that must be gone ─────────────────────────────────────────

def test_no_postgres_coupling_is_left(): 
    """The check the port is judged on. A hit here means the file can still
    reach a store frozen on 2026-08-19 and report July as current."""
    src = SCRIPT.read_text(encoding="utf-8")
    hits = [f"{i}: {line.strip()}"
            for i, line in enumerate(src.splitlines(), 1)
            if re.search(r"psycopg|DATABASE_URL|pg_connection|dbname=|postgres", line)]
    assert hits == [], "Postgres coupling still present:\n  " + "\n  ".join(hits)


def test_every_read_names_a_postgres_table_not_a_resolved_collection():
    """`collection_for()` is called once, inside `mongo_store`. A second call at
    the call site is a no-op only while renames are off; the day they are
    switched on the read misses and the write creates an invisible second
    collection. `tests/unit/test_no_double_collection_resolution.py` is the
    repo-wide form; this is the local one, so a regression here names this
    file."""
    tree = ast.parse(SCRIPT.read_text(encoding="utf-8"))
    tables = set()
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id in ("mongo_store", "mongo_query")
                and node.args):
            first = node.args[0]
            assert isinstance(first, ast.Constant) and isinstance(first.value, str), (
                f"line {node.lineno}: the collection argument is computed, not a "
                f"literal table name: {ast.unparse(first)}")
            tables.add(first.value)
    assert tables == {"fundamentals", "decision_scores", "decision_outcomes"}, tables


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
