"""`scripts/execution_quality.py` must read Mongo, and read it correctly.

WHY THIS FILE EXISTS
--------------------
This script is the ledger-side check on `app/quant/execution_costs.py`: that
module MODELS trading costs, this one reports what was actually paid, and the
whole point is that a modeled number never gets presented as a measured one. It
read PostgreSQL through `scripts.migration.pg_connection`, and PostgreSQL was
retired at the 2026-08-19 cutover. It did not go quiet — it went loud:

    AttributeError: 'Settings' object has no attribute 'DATABASE_URL'   EXIT=1

zero output, on every invocation. Loud and dead is still dead.

EVERY ASSERTION HERE WAS RED BEFORE THE PORT
--------------------------------------------
  * `test_no_postgres_coupling` — `gate_zero_pg.scan` counted 3 couplings on
    this file (connection_import line 36, get_db_call line 45, execute_call
    line 46) and now counts 0.
  * every other test — the script was one `main()` around a cursor, so
    `parse_since`, `load_fills`, `shortfall_bps`, `as_day` and
    `unreadable_fills` did not exist to call.

The behavioural tests pin the five things a mechanical port of THIS script gets
wrong. Each was verified against the live stores on 2026-08-30, and each was
re-checked against a deliberately broken copy of the ported code — a test that
stays green when the code is wrong is not a test.
"""
from __future__ import annotations

import subprocess
import sys
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from scripts import execution_quality as eq  # noqa: E402
from scripts.gate_zero_pg import scan  # noqa: E402

SCRIPT = "scripts/execution_quality.py"


# ── the store, as it really reads back ──────────────────────────────────────
# NOT a convenience shape. `app/db/money_policy.py` lists `trade_fills`
# fill_price / fill_value / fees as money and does NOT list decision_price, so
# `mongo_query.find_rows` returns Decimal, float, Decimal, Decimal, float in
# exactly this pattern. Verified live on 2026-08-30 against the COF fill of
# 2026-07-26:
#   ['Decimal', 'float', 'Decimal', 'Decimal', 'float']
# Fixtures that used plain floats here would make the money tests vacuous.
def _fill(ticker, side, qty, fill_price, decision_price, value, fees, when):
    return (ticker, side, float(qty), Decimal(str(fill_price)),
            None if decision_price is None else float(decision_price),
            Decimal(str(value)), Decimal(str(fees)), when)


def _store(monkeypatch, docs, *, total=None, dated=None):
    """Point the script's two reads at fixtures — never at production Mongo."""
    seen = []

    def find_rows(collection, query, columns, sort=None, limit=0):
        seen.append((collection, query, tuple(columns), sort))
        lo = query["filled_at"]["$gte"]
        rows = [d for d in docs if d[7] >= lo]
        # Honour the sort spec, the way Mongo would, INSTEAD of sorting
        # unconditionally. The first version of this fixture always sorted by
        # timestamp descending, which meant it was doing the job the code under
        # test is responsible for asking for: a mutant that re-sorted the rows
        # by the truncated `::date` stayed green, because the fake had already
        # established the order and Python's sort is stable. A fixture that
        # satisfies the assertion on its own tests nothing.
        for field, direction in reversed(list(sort or [])):
            idx = list(columns).index(field)
            rows.sort(key=lambda r: r[idx], reverse=direction < 0)
        return rows

    def count(collection, query=None):
        seen.append((collection, query, None, None))
        return (len(docs) if total is None else total) if not query else (
            len(docs) if dated is None else dated)

    monkeypatch.setattr(eq.mongo_query, "find_rows", find_rows)
    monkeypatch.setattr(eq.mongo_query, "count", count)
    return seen


def _run(monkeypatch, capsys, *argv):
    monkeypatch.setattr(sys, "argv", ["execution_quality.py", *argv])
    code = eq.main()
    return code, capsys.readouterr().out


# ── 1. the store it talks to ────────────────────────────────────────────────
def test_no_postgres_coupling():
    """The instrument reads the store the system actually writes.

    RED BEFORE: `scan` reported total=3 for this file — connection_import
    (`from scripts.migration.pg_connection import get_db`), get_db_call and
    execute_call.
    """
    result = scan(REPO, (SCRIPT,))
    assert result["total"] == 0, result["findings"]

    source = (REPO / SCRIPT).read_text(encoding="utf-8")
    code = "\n".join(l for l in source.splitlines() if not l.lstrip().startswith("#"))
    body = code.split('"""', 2)[2] if code.count('"""') >= 2 else code
    for token in ("psycopg", "DATABASE_URL", "pg_connection", "dbname="):
        assert token not in body, f"{token!r} still in {SCRIPT} outside the docstring"


def test_it_names_the_postgres_table_not_a_resolved_collection(monkeypatch):
    """`collection_for()` is called by the helper, exactly once.

    Passing an already-resolved collection name resolves it TWICE, which is
    harmless only while renames are off — the day they are switched on, the
    read misses and the write creates an invisible second collection.
    `mongo_store._coll`'s own docstring says so.
    """
    assert eq.FILLS == "trade_fills"
    seen = _store(monkeypatch, [])
    eq.load_fills(datetime(2026, 1, 1))
    eq.unreadable_fills()
    assert {c for c, *_ in seen} == {"trade_fills"}


# ── 2. money meets float — the crash a naive port ships ─────────────────────
def test_the_money_column_meets_the_float_column_and_the_float_is_promoted():
    """`fill_price` is Decimal, `decision_price` is float. Subtracting raises.

    RED BEFORE, and red against the obvious port: `(fill_price -
    decision_price) / decision_price` on values straight out of
    `mongo_query.find_rows` raises

        TypeError: unsupported operand type(s) for -: 'decimal.Decimal' and 'float'

    on the FIRST priced fill — reproduced live on 2026-08-30 against the COF
    fill of 2026-07-26 (Decimal('202.89356552584533') - 202.83999633789062).
    The asymmetry is real and permanent: `paper_trader` writes decision_price
    through `mongo_store.dec128()`, but `money_policy._MONEY_COLUMNS` does not
    list it, so it reads back float beside fill_price's Decimal.
    """
    bps = eq.shortfall_bps("BUY", Decimal("202.89356552584533"), 202.83999633789062)
    assert f"{bps:.2f}" == "2.64"           # the live COF row, as printed

    # Promoted, not demoted, so the result is still money-compatible: `main()`
    # does `total_shortfall_bps += bps * as_money(fill_value)`, and a float
    # returned from here raises the SAME TypeError one line further on.
    assert isinstance(bps, Decimal)
    assert isinstance(bps * Decimal("2020.47"), Decimal)

    exact = eq.shortfall_bps("BUY", Decimal("101"), 100.0)
    assert exact == Decimal(100), exact      # 1% is exactly 100 bps


def test_a_decision_price_that_arrives_as_decimal_is_handled_too():
    """The next real fill stores it as Decimal128, the backfilled twelve as float.

    `insert_docs` applies no money coercion, so `dec128(reference_price)` lands
    as Decimal128 for anything the live book fills; the 12 priced documents in
    the store today came through the backfill as doubles. Both read back float
    through `find_rows` today, but the function must not care which it gets.
    """
    assert eq.shortfall_bps("BUY", Decimal("101"), Decimal("100")) == Decimal(100)
    assert eq.shortfall_bps("BUY", 101.0, 100.0) == Decimal(100)


# ── 3. the sign convention ──────────────────────────────────────────────────
def test_a_sell_that_fills_below_the_decision_price_is_a_POSITIVE_shortfall():
    """A buy filling high and a sell filling low are the same failure.

    Dropping the side negation is the single most damaging silent port bug
    here: the two sides then CANCEL in the value-weighted average, and a book
    paying real costs on both legs reports ~0 bps — "execution was free", which
    is exactly the laundering this script exists to detect. The live book is
    45 BUY / 11 SELL, so the cancellation would be partial and plausible.
    """
    assert eq.shortfall_bps("BUY", 101.0, 100.0) == Decimal(100)     # filled high: worse
    assert eq.shortfall_bps("BUY", 99.0, 100.0) == Decimal(-100)     # filled low: better
    assert eq.shortfall_bps("SELL", 99.0, 100.0) == Decimal(100)     # filled low: WORSE
    assert eq.shortfall_bps("SELL", 101.0, 100.0) == Decimal(-100)

    # `str(side).upper()` — the archive column is 'BUY'/'SELL', but the
    # comparison must not be the thing that decides the sign by accident.
    assert eq.shortfall_bps("sell", 99.0, 100.0) == Decimal(100)
    assert eq.shortfall_bps("Buy", 101.0, 100.0) == Decimal(100)


# ── 4. the headline number ──────────────────────────────────────────────────
def test_the_headline_is_value_weighted_not_a_mean_of_the_bps_column(monkeypatch, capsys):
    """`sum(bps * value) / sum(value)`, not `mean(bps)`.

    A 4-share fill and a 4,000-share fill cost the book very different amounts
    for the same bps, and the unweighted average of the two is not a cost. The
    live spread makes this bite: FCF paid 15.58 bps on $2,538 while GOOG paid
    0.56 bps on $2,249 — the mean of the twelve is 3.71 bps, the value-weighted
    number is 3.63.
    """
    docs = [
        _fill("BIG", "BUY", 1, 100.10, 100.0, 100_000, 1.0, datetime(2026, 8, 2)),
        _fill("TINY", "BUY", 1, 110.00, 100.0, 1, 0.0, datetime(2026, 8, 1)),
    ]
    _store(monkeypatch, docs)
    code, out = _run(monkeypatch, capsys, "--since", "2026-01-01")
    assert code == 0
    # weighted: (10 * 100000 + 1000 * 1) / 100001 == 10.0099...
    assert "Value-weighted implementation shortfall: +10.01 bps" in out
    # the unweighted mean of 10 and 1000 is 505.00 — the number NOT printed
    assert "+505.00" not in out
    assert "Total fees recorded: $1.00 on $100,001.00 traded" in out


def test_the_totals_survive_the_money_types(monkeypatch, capsys):
    """`$32,661.52` traded and `$11.86` of fees — the live figures, formatted.

    Decimal reaches `format()` here, and `f"{Decimal128('30.03'):.2f}"` raises
    TypeError while a demoted float silently loses the exactness the column was
    promoted for. Both failure modes land on this line.
    """
    docs = [_fill("X", "BUY", 1, 100.0, 100.0, "32661.522738", "11.862497",
                  datetime(2026, 8, 1))]
    _store(monkeypatch, docs)
    _, out = _run(monkeypatch, capsys, "--since", "2026-01-01")
    assert "Total fees recorded: $11.86 on $32,661.52 traded" in out


# ── 5. what gets excluded, and how loudly ───────────────────────────────────
def test_unpriced_fills_are_excluded_and_never_counted_as_zero_cost(monkeypatch, capsys):
    """44 of the 56 live fills predate the column. Counting them as 0 bps lies.

    Both spellings of absent must behave the same. Postgres column DEFAULTs are
    gone, so a field the archive always carried can simply be MISSING on a
    post-cutover document, and `find_rows` hands back None for it either way.
    """
    docs = [
        _fill("OLD", "BUY", 1, 100.0, None, 1000, 0.0, datetime(2026, 7, 1)),
        _fill("ZERO", "BUY", 1, 100.0, 0.0, 1000, 0.0, datetime(2026, 7, 2)),
        _fill("NEW", "BUY", 1, 101.0, 100.0, 1000, 1.0, datetime(2026, 8, 1)),
    ]
    _store(monkeypatch, docs)
    _, out = _run(monkeypatch, capsys, "--since", "2026-01-01")
    assert "EXECUTION QUALITY — 3 fills since 2026-01-01" in out
    assert "2 fill(s) carry no decision_price" in out
    assert "Excluded, not counted as zero-cost." in out
    # the headline is the ONE priced fill's own number, undiluted by the two
    assert "Value-weighted implementation shortfall: +100.00 bps" in out
    assert "OLD" not in out and "ZERO" not in out


def test_every_fill_unpriced_says_so_instead_of_printing_zero_bps(monkeypatch, capsys):
    """Trap 7: an empty answer has to show WHY it is empty, and exit 0."""
    docs = [_fill("OLD", "BUY", 1, 100.0, None, 1000, 0.0, datetime(2026, 7, 1))]
    _store(monkeypatch, docs)
    code, out = _run(monkeypatch, capsys, "--since", "2026-01-01")
    assert code == 0
    assert "No cost-bearing fills yet." in out
    assert "0.00 bps" not in out


def test_a_fill_with_no_usable_timestamp_is_reported_not_silently_dropped(
        monkeypatch, capsys):
    """Trap 3 and trap 5 at once, on the one field every window depends on.

    `$gte` matches neither a missing field nor a string, so a fill written
    without a real BSON date is invisible to EVERY window this script offers —
    silently, with no row count looking wrong. Postgres declared `filled_at`
    NOT NULL, so the archive cannot contain one; Mongo has no such constraint.
    Live count on 2026-08-30: 0 of 56, so this guards the next write.
    """
    docs = [_fill("A", "BUY", 1, 101.0, 100.0, 1000, 1.0, datetime(2026, 8, 1))]
    _store(monkeypatch, docs, total=3, dated=1)
    _, out = _run(monkeypatch, capsys, "--since", "2026-01-01")
    assert "2 fill(s) carry no usable `filled_at`" in out

    _store(monkeypatch, docs, total=1, dated=1)
    _, clean = _run(monkeypatch, capsys, "--since", "2026-01-01")
    assert "usable `filled_at`" not in clean, "the warning must stay silent at 0"


def test_an_empty_window_says_so_and_still_explains_invisible_fills(
        monkeypatch, capsys):
    """The post-cutover window IS empty: the book has taken no fill since
    2026-08-18 14:39 (`orders` is frozen at the same instant). "No fills" must
    be reachable and must not be confusable with a broken read."""
    docs = [_fill("A", "BUY", 1, 101.0, 100.0, 1000, 1.0, datetime(2026, 8, 1))]
    _store(monkeypatch, docs, total=2, dated=1)
    code, out = _run(monkeypatch, capsys, "--since", "2026-08-19")
    assert code == 0
    assert "No fills since 2026-08-19." in out
    assert "1 fill(s) carry no usable `filled_at`" in out


# ── 6. the two things the SQL text hid ──────────────────────────────────────
def test_since_reaches_mongo_as_a_datetime_and_the_heading_keeps_the_text(
        monkeypatch, capsys):
    """`{"$gte": "2026-01-01"}` on a BSON Date is a cross-type comparison.

    It matches nothing on its own; today it survives only because
    `mongo_store` routes every filter through `date_fields.coerce_filter`,
    which happens to know `trade_fills.filled_at` is a timestamp. That seam
    belongs to another module and covers only the columns in
    `schema_manifest.json`, so the parse happens in this script.

    The heading still prints the argument as TYPED — converting first would
    turn "since 2026-01-01" into "since 2026-01-01 00:00:00".
    """
    assert eq.parse_since("2026-07-01") == datetime(2026, 7, 1)
    assert eq.parse_since("2026-07-01T06:30:00") == datetime(2026, 7, 1, 6, 30)
    with pytest.raises(ValueError):
        eq.parse_since("nonsense")

    docs = [_fill("A", "BUY", 1, 101.0, 100.0, 1000, 1.0, datetime(2026, 8, 1))]
    seen = _store(monkeypatch, docs)
    _, out = _run(monkeypatch, capsys, "--since", "2026-01-01")
    sent = [q for c, q, cols, _ in seen if cols][0]
    assert isinstance(sent["filled_at"]["$gte"], datetime), sent
    assert list(sent) == ["filled_at"], (
        "decision_price is filtered in Python, as the SQL did — a `$ne: None` "
        "filter is a different question and hides the excluded count")
    assert "fills since 2026-01-01" in out and "00:00:00" not in out


def test_the_sort_is_the_timestamp_and_the_printed_column_is_the_date(
        monkeypatch, capsys):
    """`ORDER BY filled_at DESC` bound to `filled_at::date`, not to the column.

    The cast's output column is ALSO named `filled_at`, and SQL resolves an
    unqualified ORDER BY name against the SELECT LIST first — so the archive
    sorted by CALENDAR DAY and every fill sharing a day was an unbroken tie.
    Postgres says it out loud once both are visible: `ORDER BY "filled_at" is
    ambiguous`. Measured 2026-08-30 over the twelve priced fills:

        ORDER BY filled_at            -> GOOG AMZN  ET TRMB SE  FCF ...
        ORDER BY trade_fills.filled_at -> GOOG AMZN TRMB SE  ET  FCF ...

    (20:55, 07:45 and 05:24 on 2026-08-12). This sorts the timestamp, which is
    what "newest fill first" means. Truncating to a date before sorting — the
    port that copies `filled_at::date` into the projection AND the sort — puts
    those three back in arbitrary order.
    """
    # Deliberately NOT in the answer's order: the fixture returns them as
    # given unless the code asks Mongo for a sort, so dropping the sort,
    # inverting it, or naming a field that is not in the projection all show up
    # as a wrong ticker order rather than as an accident of the fake.
    same_day = [
        _fill("EARLY", "BUY", 1, 101.0, 100.0, 1000, 1.0, datetime(2026, 8, 12, 5, 24)),
        _fill("LATE", "BUY", 1, 101.0, 100.0, 1000, 1.0, datetime(2026, 8, 12, 20, 55)),
        _fill("MID", "BUY", 1, 101.0, 100.0, 1000, 1.0, datetime(2026, 8, 12, 7, 45)),
    ]
    seen = _store(monkeypatch, same_day)
    rows = eq.load_fills(datetime(2026, 1, 1))
    assert [r[0] for r in rows] == ["LATE", "MID", "EARLY"]

    sort = [s for c, q, cols, s in seen if cols][0]
    assert sort == [("filled_at", -1)], sort
    # The timestamp must survive into the projection: a port that asks Mongo
    # only for the day has nothing left to order the three by.
    assert "filled_at" in seen[0][2] and "filled_at::date" not in seen[0][2]

    # ...and the column that is PRINTED is the date, as `::date` produced.
    assert rows[0][7] == date(2026, 8, 12)
    assert not isinstance(rows[0][7], datetime)
    _, out = _run(monkeypatch, capsys, "--since", "2026-01-01")
    assert "  2026-08-12\n" in out and "2026-08-12 20:55" not in out


def test_as_day_matches_what_the_cast_returned():
    assert eq.as_day(datetime(2026, 8, 12, 20, 55, 41, 476000)) == date(2026, 8, 12)
    assert eq.as_day(date(2026, 8, 12)) == date(2026, 8, 12)
    assert eq.as_day(None) is None


# ── 7. it has to RUN, not just import under a helpful sys.path ──────────────
def test_runs_under_the_commands_its_own_docstring_documents():
    """`python scripts/execution_quality.py` — the Usage lines, verbatim.

    `sys.path[0]` is `scripts/` under that invocation, not the repo root, and
    this venv carries no path entry for the repo, so a module-scope `from
    app.db import mongo_query` without the bootstrap raises
    `ModuleNotFoundError: No module named 'app'` and exits 1 with zero output.
    The rest of this file cannot see that: it puts REPO on `sys.path` itself
    and imports `from scripts import execution_quality`, which is the one path
    that was never broken. Only a subprocess reproduces the real `sys.path[0]`.

    Deliberately DB-free: `--help` renders the docstring through argparse and
    `--since nonsense` fails the parse, both before any collection is touched,
    so this test asserts reachability without a live store.
    """
    env = {"PATH": "/usr/bin:/bin"}

    ok = subprocess.run([sys.executable, SCRIPT, "--help"],
                        cwd=str(REPO), env=env, capture_output=True, text=True,
                        timeout=120)
    assert "ModuleNotFoundError" not in ok.stderr, ok.stderr
    assert ok.returncode == 0, f"exit={ok.returncode}\n{ok.stderr}"
    assert "usage: execution_quality.py [-h] [--since SINCE]" in ok.stdout
    assert "Realized implementation shortfall" in ok.stdout

    bad = subprocess.run([sys.executable, SCRIPT, "--since", "nonsense"],
                         cwd=str(REPO), env=env, capture_output=True, text=True,
                         timeout=120)
    assert "ModuleNotFoundError" not in bad.stderr, bad.stderr
    # Postgres answered this with a psycopg traceback and exit 1. Same code.
    assert bad.returncode == 1, bad.stderr
    assert "is not a date or timestamp" in bad.stderr
    assert "Traceback" not in bad.stderr
