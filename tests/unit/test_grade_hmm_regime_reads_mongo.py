"""`scripts/grade_hmm_regime.py` reads MongoDB, and reads it in SESSIONS.

The three Postgres SELECTs in this script (asset_prices, regime_hmm_posteriors,
shared_desk) were ported on 2026-08-30. A purely mechanical port — swap
`get_db().execute(sql)` for the `mongo_query` call the translator emits and
change nothing else — compiles, and then fails in three separate ways that the
oracle's row-set comparison cannot see, because all three are about the SHAPE
of what comes back rather than which rows come back:

  1. `regime_hmm_posteriors.as_of` is 255 BSON dates and 4 STRINGS
     (`persist_posterior` stores `str(dates[-1])`, which is now
     "2026-08-19 00:00:00" and which `date_fields.as_date` does not recognise).
     `datetime > str` raises, so the grade ABORTS rather than grading.
  2. `asset_prices` lost `PRIMARY KEY (symbol, asset_class, date)` — the Mongo
     `natural_key` index is not unique — so GSPC answers with 4,203 rows for
     203 sessions. `_move_after(spx, D, 5)` walks five ROWS, and five rows
     inside a 33-deep pile is the same afternoon.
  3. the head-to-head keys posteriors and desks on `str(...)` of two different
     types, so "2026-08-17 00:00:00" never meets "2026-08-17" and the compare
     reports that the LLM made no scoreable call — a finding this script
     genuinely reports, arrived at by a type mismatch.

Every assertion below was RED before the port: 1, 3 and 6 because the functions
they call still opened a Postgres cursor, and 2, 4 and 5 because the behaviour
they pin did not exist. Confirmed by re-running this file against a copy of the
script with each fix reverted — see the docstring on each test for the failure
that copy produced.

No live store: `mongo_query.find_rows` is patched, per the `block_production_mongo`
contract in tests/conftest.py.
"""

from __future__ import annotations

import re
from datetime import date, datetime
from pathlib import Path

import pytest

from scripts import grade_hmm_regime as g

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts" / "grade_hmm_regime.py"


# ── 1. the coupling is gone ──────────────────────────────────────────

def test_no_postgres_coupling_remains():
    """The grep from the porting brief, as an assertion.

    Before the port this matched three `from scripts.migration.pg_connection
    import get_db` lines (in `_asset_closes`, `backfill` and `_grade_llm`) plus
    their three `db.execute(...)` bodies.

    Matched against CODE only — the docstrings deliberately say "Postgres"
    when explaining what the archive guaranteed, and a text grep that condemned
    that would push the explanation out of the file.
    """
    code = []
    for line in SCRIPT.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        code.append(line)
    joined = "\n".join(code)
    # Strip docstrings/comments crudely but conservatively: any line that is
    # prose inside a triple-quoted block still gets scanned, so a real
    # `import psycopg` cannot hide in one.
    for pattern in ("psycopg", "DATABASE_URL", "pg_connection", "dbname=", "get_db"):
        assert pattern not in joined, f"{pattern!r} still present in {SCRIPT.name}"


def test_reads_pass_the_postgres_table_name_not_a_resolved_collection():
    """`collection_for()` is called exactly once, inside the helper.

    `mongo_query.find_rows("asset_prices", ...)` resolves the name once;
    `find_rows(collection_for("asset_prices"), ...)` resolves it twice, which
    is harmless only while renames are off and is a silent miss the day they
    are switched on.
    """
    src = SCRIPT.read_text(encoding="utf-8")
    assert "collection_for" not in src
    for table in ("asset_prices", "regime_hmm_posteriors", "shared_desk"):
        assert re.search(rf'find_rows\(\s*\n?\s*"{table}"', src), (
            f"no find_rows on the plain table name {table!r}")


# ── 2. asset_prices: the primary key Mongo lost ──────────────────────

def _pile(monkeypatch, rows):
    """Point `_asset_closes` at a fabricated asset_prices answer."""
    seen: dict = {}

    def _find_rows(collection, query, columns, sort=None, limit=0, **kw):
        seen["collection"] = collection
        seen["query"] = query
        seen["columns"] = list(columns)
        seen["sort"] = sort
        return rows

    monkeypatch.setattr(g.mongo_query, "find_rows", _find_rows)
    return seen


def test_asset_closes_restores_the_primary_key_and_keeps_the_first_write(monkeypatch):
    """One close per (symbol, asset_class, date), and it is the OLDEST write.

    First-write-wins is not a preference: `market_regime_collector` documents
    its write as `ON CONFLICT (symbol, asset_class, date) DO NOTHING`, and on
    the live store the min-`_id` row reproduces the frozen Postgres archive on
    196 of 196 GSPC sessions where the max-`_id` row matches on only 164.

    Against the pre-port copy this test errors instead of failing: the function
    it calls asks `scripts.migration.pg_connection` for a cursor.
    """
    rows = [
        (datetime(2026, 3, 2), 6881.62, "index", 1),   # the archive's value
        (datetime(2026, 3, 2), 6900.00, "index", 2),   # a later re-fetch
        (datetime(2026, 3, 2), 6905.00, "index", 3),
        (datetime(2026, 3, 3), 6890.00, "index", 4),
    ]
    stats: dict = {}
    seen = _pile(monkeypatch, rows)

    out = g._asset_closes("GSPC", stats=stats)

    assert out == [(date(2026, 3, 2), 6881.62), (date(2026, 3, 3), 6890.00)]
    assert stats == {"rows_read": 4, "sessions": 2}
    assert seen["collection"] == "asset_prices"
    # `close IS NOT NULL`: `{"$ne": None}` excludes a null AND a missing field,
    # which is what the SQL predicate did. Dropping it would let a post-cutover
    # document with no `close` through, and `float(None)` is a TypeError.
    assert seen["query"] == {"symbol": "GSPC", "close": {"$ne": None}}
    # The sort has to carry `_id`, or "first write" is whatever order the
    # storage engine felt like emitting.
    assert seen["sort"] == [("date", 1), ("_id", 1)]


def test_nan_is_not_a_price(monkeypatch):
    """NaN survives a NOT NULL check. asset_prices carries it (app/utils/numeric.py)."""
    _pile(monkeypatch, [
        (datetime(2026, 3, 2), float("nan"), "index", 1),
        (datetime(2026, 3, 2), 6881.62, "index", 2),
        (datetime(2026, 3, 3), 6890.00, "index", 3),
    ])
    out = g._asset_closes("GSPC")
    # The NaN row is dropped and does NOT consume the session's one slot.
    assert out == [(date(2026, 3, 2), 6881.62), (date(2026, 3, 3), 6890.00)]


def test_move_after_counts_sessions_not_rows(monkeypatch):
    """THE LOAD-BEARING ASSERTION: the horizon must be five SESSIONS.

    This is the defect that makes the un-deduped port dangerous rather than
    merely noisy. Twelve rows here describe six sessions; the honest 5-session
    move from 2026-03-02 is 100 -> 105, i.e. +5%. Read as five ROWS it stops on
    the second copy of 2026-03-04 — two and a half sessions in — and reports
    +2.5%, half the move that happened.

    On the live store the difference is not cosmetic: the un-deduped GSPC
    series puts 223 of 255 realized 5-day moves inside the +/-1% deadband
    (mean |move| 0.40%) and reports an 84% hit rate against a 58%
    always-FLAT benchmark. Deduped it is 148 of 256 FLAT, mean |move| 1.16%,
    and the hit rate is 57% — the HMM LOSES to the free benchmark by a point
    instead of beating it by 26.
    """
    piled = []
    for i, (d, c) in enumerate([
        (datetime(2026, 3, 2), 100.0), (datetime(2026, 3, 3), 101.0),
        (datetime(2026, 3, 4), 102.0), (datetime(2026, 3, 5), 103.0),
        (datetime(2026, 3, 6), 104.0), (datetime(2026, 3, 9), 105.0),
    ]):
        piled.append((d, c, "index", 2 * i))
        piled.append((d, c + 0.5, "index", 2 * i + 1))   # the duplicate copy

    raw = [(d.date(), c) for d, c, _cls, _oid in piled]
    _pile(monkeypatch, piled)
    deduped = g._asset_closes("GSPC")

    assert len(raw) == 12 and len(deduped) == 6

    honest = g._move_after(deduped, date(2026, 3, 1), 5)
    naive = g._move_after(raw, date(2026, 3, 1), 5)

    assert honest == pytest.approx((105.0 - 100.0) / 100.0 * 100.0)   # +5.00%
    assert naive == pytest.approx((102.5 - 100.0) / 100.0 * 100.0)    # +2.50%
    assert abs(naive) < abs(honest), (
        "duplicate bars shorten the horizon, which pushes every realized move "
        "toward the deadband and manufactures FLAT")


# ── 3. the date shape ────────────────────────────────────────────────

def test_a_string_as_of_does_not_abort_the_grade():
    """4 of the 259 stored posteriors carry `as_of` as TEXT.

    The negative control is the first assertion: comparing the stored value to
    a bar date is exactly what `_next_return_pct` does on its first iteration,
    and on the pre-port shape it raises rather than skipping.
    """
    stored = "2026-08-19 00:00:00"          # what persist_posterior writes now
    with pytest.raises(TypeError):
        _ = datetime(2026, 8, 20) > stored   # noqa: B015 - that IS the check

    assert g._as_date(stored) == date(2026, 8, 19)
    assert g._as_date("2026-08-19") == date(2026, 8, 19)
    assert g._as_date(datetime(2026, 8, 19, 14, 30)) == date(2026, 8, 19)
    assert g._as_date(date(2026, 8, 19)) == date(2026, 8, 19)
    assert g._as_date(None) is None


def test_the_two_collections_key_the_same_day_the_same_way():
    """The head-to-head joins `str(as_of)` to `str(created_at.date())`.

    `regime_hmm_posteriors.as_of` is a midnight datetime and
    `shared_desk.created_at` is a real timestamp, so before normalisation the
    two sides of that join print "2026-08-17 00:00:00" and "2026-08-17" and
    match on nothing — which is why the pre-port compare printed "the LLM
    produced no scoreable forward_call on these days" for all 81 of them.
    """
    as_of = datetime(2026, 8, 17)                     # posterior, midnight
    created = datetime(2026, 8, 17, 5, 59, 26, 902000)  # desk, a real time

    assert str(as_of) != str(created.date())          # the mismatch, pinned
    assert str(g._as_date(as_of)) == str(g._as_date(created)) == "2026-08-17"


def test_dated_normalises_and_orders_a_close_series():
    out = g._dated([(datetime(2026, 3, 4), 3), ("2026-03-02", 1), (date(2026, 3, 3), 2)])
    assert out == [(date(2026, 3, 2), 1.0), (date(2026, 3, 3), 2.0), (date(2026, 3, 4), 3.0)]
    assert g._sessions([(date(2026, 3, 2), 1.0), (date(2026, 3, 2), 1.5)]) == 1


# ── 4. shared_desk ───────────────────────────────────────────────────

def test_grade_llm_reads_mongo_and_accepts_both_desk_data_shapes(monkeypatch):
    """`desk_data` is a subdocument for the 1,762 migrated desks and JSON TEXT
    for the 274 written since the cutover. Both must score.

    A Mongo-side filter on `desk_data.regime_classification` would have matched
    only the archive half — 0 of the post-cutover desks — so the whole document
    is fetched and unwrapped in Python, exactly as the SQL version did.
    """
    call = {"forward_call": {"spx_direction": "UP"}}
    seen: dict = {}

    def _find_rows(collection, query, columns, sort=None, limit=0, **kw):
        seen.update(collection=collection, query=query,
                    columns=list(columns), sort=sort)
        import json
        return [
            # migrated: a real subdocument
            ("cycle-a", datetime(2026, 3, 3, 9, 30), {"regime_classification": call}),
            ("cycle-a", datetime(2026, 3, 3, 9, 40), {"regime_classification": call}),  # dupe
            # post-cutover: JSON text
            ("cycle-b", datetime(2026, 3, 4, 9, 30),
             json.dumps({"regime_classification": call})),
            # post-cutover: created_at default lost, 76 documents like this
            ("cycle-c", None, {"regime_classification": call}),
            # a desk with no forward_call at all
            ("cycle-d", datetime(2026, 3, 5, 9, 30), {"regime_classification": {}}),
        ]

    monkeypatch.setattr(g.mongo_query, "find_rows", _find_rows)

    spx = [(date(2026, 3, d), 100.0 + d) for d in (3, 4, 5, 6, 9, 10, 11, 12)]
    out = g._grade_llm({"2026-03-03", "2026-03-04"}, spx)

    assert seen["collection"] == "shared_desk"
    assert seen["query"] == {}                       # the SQL had no WHERE
    assert seen["columns"] == ["cycle_id", "created_at", "desk_data"]
    assert seen["sort"] == [("created_at", 1)]
    # cycle-a once (deduped), cycle-b once; cycle-c has no date, cycle-d no call.
    assert out == {"hits": 2, "n": 2}


def test_a_dateless_desk_cannot_claim_its_cycle(monkeypatch):
    """`created_at` lost its `now()` default: 76 of 2,036 documents have none.

    The dateless copy must not enter `seen`, or it would consume the cycle id
    and hide the dated copy that carries the same call.
    """
    call = {"forward_call": {"spx_direction": "UP"}}

    def _find_rows(collection, query, columns, sort=None, limit=0, **kw):
        return [
            ("cycle-a", None, {"regime_classification": call}),           # sorts first
            ("cycle-a", datetime(2026, 3, 3, 9, 30), {"regime_classification": call}),
        ]

    monkeypatch.setattr(g.mongo_query, "find_rows", _find_rows)
    spx = [(date(2026, 3, d), 100.0 + d) for d in (3, 4, 5, 6, 9, 10, 11)]
    assert g._grade_llm({"2026-03-03"}, spx) == {"hits": 1, "n": 1}
