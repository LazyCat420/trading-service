"""The Mongo read seam of `scripts/regime_overlay_backtest.py`.

`tests/unit/test_regime_overlay.py` owns the SIMULATION — exposure, costs,
drawdown, and the point-in-time rule. This file owns the LOADER, which is the
half the Postgres-to-Mongo port rewrote, and it pins the three things that
port can silently get wrong.

WHY EACH TEST HERE EXISTS (all three were live defects, not hypotheticals)
-------------------------------------------------------------------------

1. THE STRING `as_of`. Postgres declared `regime_hmm_posteriors.as_of` a
   `date`, so the join in `load_aligned_series` compared two values of one
   type. Mongo has no column types. Measured against the live store on
   2026-08-30: 255 of 259 SPY posteriors hold a BSON date, and the other 4 —
   every one written after the 2026-08-19 cutover — hold the STRING
   "2026-08-19 00:00:00", because `app/quant/regime_hmm.py:300` writes
   `str(dates[-1])` and `app/db/date_fields.as_date` only recognises a bare
   `YYYY-MM-DD`. A string key never equals a datetime key, so an unnormalised
   port scores 255 observations out of 259 and prints a plausible report.

2. THE ORDER. BSON sorts by TYPE before value and String ranks below Date, so
   `sort=[("as_of", 1)]` returns those 4 newest rows FIRST. Against the live
   store the report's window line read "2026-08-19 .. 2026-08-17" — a
   backwards window on a query that asked for ascending order.

3. THE VENDOR. `price_history`'s primary key is `(ticker, date, source)` and
   SPY carries 269 dual-vendor dates in Mongo. Unpinned, `closes[i + 1]` can
   be the OTHER vendor's print of the SAME session, which turns a daily return
   into a vendor spread: measured on the live store, the buy-and-hold mean
   went from +0.082%/day pinned to +0.405%/day unpinned — a 5x fabrication.

Every test runs offline: `mongo_query.find_rows` and the vendor resolver are
patched, so `block_production_mongo` is never tripped.
"""

from __future__ import annotations

import io
import re
import tokenize
from datetime import date, datetime
from pathlib import Path

import pytest

import scripts.regime_overlay_backtest as mod


REPO = Path(__file__).resolve().parents[2]

# The exact string shape the live writer produces — not an invented one.
# `str(datetime(2026, 8, 19))` == "2026-08-19 00:00:00".
LIVE_STRING_AS_OF = "2026-08-19 00:00:00"


class _Store:
    """Stands in for `mongo_query`, recording what the loader asked for."""

    def __init__(self, prices, posts):
        self._rows = {"price_history": prices, "regime_hmm_posteriors": posts}
        self.calls: list[tuple] = []

    def find_rows(self, collection, query, columns, sort=None, limit=0,
                  session=None):
        self.calls.append((collection, query, tuple(columns), sort))
        rows = self._rows[collection]
        if collection == "price_history" and "source" in query:
            rows = [r for r in rows if r[2] == query["source"]]
        return [tuple(r[: len(columns)]) for r in rows]


@pytest.fixture
def store(monkeypatch):
    """Install a `_Store` and a fixed vendor answer; hand back a builder."""
    from app.db import mongo_query
    import app.quant.returns as rets

    def _install(prices, posts, dominant="yfinance"):
        s = _Store(prices, posts)
        monkeypatch.setattr(mongo_query, "find_rows", s.find_rows)
        monkeypatch.setattr(rets, "dominant_source_for", lambda _t: dominant)
        return s

    return _install


def _price(d, close, source="yfinance"):
    return (d, close, source)


def _post(as_of, p_stressed):
    return (as_of, {"CALM": 1.0 - p_stressed, "STRESSED": p_stressed},
            "STRESSED" if p_stressed >= 0.5 else "CALM")


# ── 1. the store it reads ────────────────────────────────────────────

def test_the_loader_reads_mongo_and_nothing_else(store):
    """RED before the port: `load_aligned_series` opened
    `scripts.migration.pg_connection.get_db()` and never touched
    `mongo_query`, so `calls` came back empty (and the import raised first).
    """
    s = store(
        [_price(datetime(2026, 1, 5), 100.0), _price(datetime(2026, 1, 6), 110.0)],
        [_post(datetime(2026, 1, 5), 0.8)],
    )
    rows = mod.load_aligned_series("SPY")

    assert [c[0] for c in s.calls] == ["price_history", "regime_hmm_posteriors"]
    assert len(rows) == 1
    # 01-05's posterior trades 01-06's return: 100 -> 110 = +10%.
    assert rows[0]["next_return_pct"] == pytest.approx(10.0)


def _code_only(text: str) -> str:
    """`text` with every comment and string literal removed.

    A raw grep cannot make the distinction this test needs. The word
    "Postgres" BELONGS in this module's prose — the docstrings say which store
    the reads left, and why a backtest may not read a frozen archive. What may
    not survive is a Postgres COUPLING: an import, a DSN, a cursor call.
    Stripping comments and strings is exactly the line between the two, so the
    test is not satisfied by deleting the explanation.
    """
    kept = []
    for tok in tokenize.generate_tokens(io.StringIO(text).readline):
        name = tokenize.tok_name.get(tok.type, "")
        if tok.type == tokenize.COMMENT or name.startswith("FSTRING") \
                or tok.type == tokenize.STRING:
            continue
        kept.append(tok.string)
    return " ".join(kept)


def test_no_postgres_coupling_survives_in_the_module():
    """The port's own acceptance criterion, asserted rather than grepped by
    hand. RED before the port on both halves: `from scripts.migration.
    pg_connection import get_db`, `db.execute(...).fetchall()` and
    `dominant_source_sql()` were all live CODE."""
    text = (REPO / "scripts/regime_overlay_backtest.py").read_text(encoding="utf-8")

    # Never legitimate anywhere in the file, prose included.
    assert not re.findall(r"psycopg|DATABASE_URL|pg_connection|dbname=", text, re.I)

    # And no Postgres machinery in the executable half.
    assert not re.findall(
        r"postgres|\bget_db\b|\bfetchall\b|\bexecute\b|dominant_source_sql",
        _code_only(text), re.I,
    )

    # Non-vacuity: the same scan MUST condemn the shape this replaced, or it
    # is matching nothing and would pass on a file that never changed.
    #
    # This used to read `git show 77e6dc3:scripts/regime_overlay_backtest.py`,
    # which was the pre-port file right up until the port was COMMITTED — and
    # then HEAD became the ported version, the control found no `pg_connection`
    # in it, and the test failed for having succeeded. A negative control
    # pinned to a moving ref expires the moment the work lands. It is a
    # FIXTURE now: the shape itself, which cannot rot.
    before = (
        "from scripts.migration.pg_connection import get_db\n"
        "from app.quant.returns import dominant_source_sql\n"
        "def load_aligned_series(ticker):\n"
        "    with get_db() as db:\n"
        "        return db.execute(\n"
        "            'SELECT date, close FROM price_history WHERE ticker = %s '\n"
        "            'AND source = ' + dominant_source_sql(), [ticker]\n"
        "        ).fetchall()\n"
    )
    assert re.findall(r"pg_connection", before)
    assert re.findall(r"\bget_db\b|\bfetchall\b|dominant_source_sql",
                      _code_only(before), re.I)


# ── 2. the string as_of (trap: a string timestamp is not a date) ──────

def test_a_string_as_of_is_aligned_exactly_like_a_dated_one(store):
    """The post-cutover shape. RED without `_as_day`'s string branch: the
    string key misses `by_index` and this row vanishes with no error."""
    store(
        [_price(datetime(2026, 8, 18), 100.0),
         _price(datetime(2026, 8, 19), 100.0),
         _price(datetime(2026, 8, 20), 102.0)],
        [_post(datetime(2026, 8, 18), 0.10),
         _post(LIVE_STRING_AS_OF, 0.90)],
    )
    rows = mod.load_aligned_series("SPY")

    assert len(rows) == 2, "the string-dated posterior was silently dropped"
    string_row = [r for r in rows if r["as_of"] == date(2026, 8, 19)]
    assert len(string_row) == 1
    assert string_row[0]["p_stressed"] == pytest.approx(0.90)
    # 08-19 -> 08-20 is 100 -> 102.
    assert string_row[0]["next_return_pct"] == pytest.approx(2.0)


def test_posteriors_come_back_in_calendar_order_not_bson_type_order(store):
    """Mongo hands strings back BEFORE dates (String < Date in BSON type
    order), which is the order this feeds in. The loader must undo it, or the
    report prints a window that ends before it starts."""
    store(
        [_price(datetime(2026, 8, d), 100.0 + d) for d in (17, 18, 19, 20)],
        [_post("2026-08-19 00:00:00", 0.1),      # strings first, as Mongo sorts
         _post(datetime(2026, 8, 17), 0.1),
         _post(datetime(2026, 8, 18), 0.1)],
    )
    rows = mod.load_aligned_series("SPY")

    days = [r["as_of"] for r in rows]
    assert days == sorted(days), days
    assert days[0] < days[-1], "the report's window line would read backwards"


def test_as_of_is_a_calendar_day_not_a_datetime(store):
    """`as_of` is printed as the report's window. Postgres handed back a
    `date`; leaking Mongo's midnight datetime would append a ' 00:00:00' the
    column never had."""
    store([_price(datetime(2026, 1, 5), 100.0), _price(datetime(2026, 1, 6), 101.0)],
          [_post(datetime(2026, 1, 5), 0.1)])
    (row,) = mod.load_aligned_series("SPY")
    assert type(row["as_of"]) is date


@pytest.mark.parametrize("value", ["", "not-a-date", "2026-13-99", None, 42])
def test_an_unreadable_as_of_is_dropped_rather_than_guessed(value):
    """A row that cannot be placed on the calendar cannot be aligned to a
    session. Returning it under a guessed day would put a real return against
    the wrong posterior."""
    assert mod._as_day(value) is None


def test_as_day_agrees_across_every_shape_the_store_holds():
    """date, datetime and both string spellings must land on ONE key, or the
    join is type-dependent — which is the whole defect."""
    keys = {
        mod._as_day(date(2026, 8, 19)),
        mod._as_day(datetime(2026, 8, 19)),
        mod._as_day(datetime(2026, 8, 19, 13, 45, 7)),
        mod._as_day("2026-08-19"),
        mod._as_day(LIVE_STRING_AS_OF),
    }
    assert keys == {datetime(2026, 8, 19)}


# ── 3. the vendor pin ────────────────────────────────────────────────

def test_the_price_read_pins_one_vendor(store):
    """`price_history` is keyed `(ticker, date, source)`; an unpinned read is
    the defect `test_price_history_one_vendor_guard` exists for."""
    s = store([_price(datetime(2026, 1, 5), 100.0),
               _price(datetime(2026, 1, 6), 101.0)],
              [_post(datetime(2026, 1, 5), 0.1)])
    mod.load_aligned_series("SPY")

    price_query = next(q for coll, q, _, _ in s.calls if coll == "price_history")
    assert price_query.get("source") == "yfinance"


def test_the_next_session_is_the_next_DAY_not_the_other_vendors_print(store):
    """The concrete harm the pin prevents. Both vendors print 01-05 and 01-06
    and they disagree; unpinned, `closes[i + 1]` is 01-05's polygon print and
    the 'daily return' is a vendor spread. Pinned, it is the real +10%."""
    prices = [
        _price(datetime(2026, 1, 5), 100.0, "yfinance"),
        _price(datetime(2026, 1, 5), 80.0, "polygon"),
        _price(datetime(2026, 1, 6), 110.0, "yfinance"),
        _price(datetime(2026, 1, 6), 88.0, "polygon"),
    ]
    store(prices, [_post(datetime(2026, 1, 5), 0.9)])
    (row,) = mod.load_aligned_series("SPY")
    assert row["next_return_pct"] == pytest.approx(10.0)
    # -20% is what the unpinned pairing (100 -> 80, same session) would give.
    assert row["next_return_pct"] != pytest.approx(-20.0)


# ── the point-in-time rule still holds through the new seam ──────────

def test_the_newest_posterior_has_no_session_to_trade_and_is_dropped(store):
    """Unchanged contract, re-pinned at the Mongo seam: including it with a
    zero return would dilute every statistic in the report. This is why the
    live run reports 258 usable observations from 259 posteriors."""
    store([_price(datetime(2026, 1, 5), 100.0)],
          [_post(datetime(2026, 1, 5), 0.9)])
    assert mod.load_aligned_series("SPY") == []


def test_a_posterior_with_no_price_bar_at_all_is_dropped(store):
    """A posterior whose `as_of` is not a trading session in the pinned
    vendor's series has no return to pair with."""
    store([_price(datetime(2026, 1, 5), 100.0), _price(datetime(2026, 1, 6), 110.0)],
          [_post(datetime(2026, 1, 4), 0.9), _post(datetime(2026, 1, 5), 0.9)])
    rows = mod.load_aligned_series("SPY")
    assert [r["as_of"] for r in rows] == [date(2026, 1, 5)]


def test_an_empty_posterior_collection_yields_an_empty_series(store):
    """The one case where `[]` is the right answer, pinned so that a future
    read bug cannot hide behind it: no posteriors, nothing to align."""
    store([_price(datetime(2026, 1, 5), 100.0)], [])
    assert mod.load_aligned_series("SPY") == []
