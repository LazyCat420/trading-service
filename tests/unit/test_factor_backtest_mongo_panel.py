"""`scripts/factor_backtest.py` builds its price panel from MongoDB, one vendor
per ticker, with the vendor chosen over the WHOLE window.

WHAT WOULD HAVE BEEN RED BEFORE
-------------------------------
Two independent things, and the second is the one worth a test file.

1. The panel came from `scripts.migration.pg_connection.get_db()`, which since
   2026-08-28 raises `AttributeError: 'Settings' object has no attribute
   'DATABASE_URL'` before a single row is read. Every assertion below that
   calls `load_panel` failed at the first line of the function.

2. The SQL selected `ticker, date, close` and no `source`, and let
   `pivot_table(aggfunc="last")` decide which vendor's print won each cell.
   `price_history` is keyed `(ticker, date, source)`; the vendors disagree by
   20.05% on average (yfinance adjusts for dividends and splits, polygon does
   not), so "whichever row landed last in the frame" is a coin flip per cell.
   `tests/unit/test_price_history_one_vendor_guard.py` carried this file at a
   budget of exactly 1 unpinned read for that reason.

THE FAILURE MODE THIS FILE IS REALLY AIMED AT
---------------------------------------------
`load_panel` fetches a YEAR AT A TIME. The obvious port pins the vendor inside
the loop, where the frame is already in hand — and that is wrong in a way no
row count notices: the dominant vendor is resolved against one year of history
at a time, so a ticker can be served polygon's raw closes for 2024 and
yfinance's adjusted closes for 2025. Alternating conventions across dates is
the more damaging half of the vendor bug (DRIP: 133 daily moves over 15% mixed,
1 pinned), and the panel that comes out has the right shape, the right dates
and the right ticker count.

So the fixture below is built so that the two placements DISAGREE: polygon wins
2024 on its own, yfinance wins the window. A per-chunk pin returns polygon's
2024 closes and keeps polygon's four extra December dates; the whole-window pin
returns yfinance's and drops them.

`check_one_vendor_panel` takes `load_panel` as an argument rather than closing
over the module so the same assertions can be run against a deliberately broken
copy of the script — which is how "this test would have failed" was checked
rather than asserted.
"""
from __future__ import annotations

import ast
import datetime as dt
import pathlib
import re
from unittest.mock import patch

import pandas as pd
import pytest

from app.db import mongo_store
import scripts.factor_backtest as fb

REPO = pathlib.Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts" / "factor_backtest.py"

# ── the fixture ──────────────────────────────────────────────────────
#
# ZZZ is carried by two vendors that disagree by ~89 on every shared date.
#
#   2024  yfinance 12-27, 12-30, 12-31            (3 rows, max 2024-12-31)
#         polygon  12-20, 12-23, 12-24, 12-26,
#                  12-27, 12-30, 12-31            (7 rows, max 2024-12-31)
#   2025  yfinance 01-02 .. 01-31 (22 sessions)   (max 2025-01-31)
#         polygon  none
#
# Within the 2024 chunk the two vendors tie on freshness and polygon wins on
# depth. Across the window polygon's newest bar is 31 days behind yfinance's,
# past _FRESHNESS_LAG_DAYS, so yfinance wins outright.
#
# AAA is single-vendor and covers every date, so the panel's index is the full
# union either way and "which dates survived" is a property of ZZZ's column
# rather than of the index.

_YF_2024 = [dt.date(2024, 12, 27), dt.date(2024, 12, 30), dt.date(2024, 12, 31)]
_PG_ONLY_2024 = [dt.date(2024, 12, 20), dt.date(2024, 12, 23),
                 dt.date(2024, 12, 24), dt.date(2024, 12, 26)]
_POLY_2024 = _PG_ONLY_2024 + _YF_2024
_YF_2025 = [dt.date(2025, 1, d) for d in
            (2, 3, 6, 7, 8, 9, 10, 13, 14, 15, 16, 17, 21, 22, 23, 24, 27, 28,
             29, 30, 31)]

YF_CLOSE = 10.0      # adjusted convention
POLY_CLOSE = 99.0    # raw convention — 89.0 away, unmissable in a panel cell


def _docs() -> list[dict]:
    rows: list[dict] = []
    for d in _YF_2024 + _YF_2025:
        rows.append({"ticker": "ZZZ", "date": dt.datetime(d.year, d.month, d.day),
                     "close": YF_CLOSE, "source": "yfinance"})
    for d in _POLY_2024:
        rows.append({"ticker": "ZZZ", "date": dt.datetime(d.year, d.month, d.day),
                     "close": POLY_CLOSE, "source": "polygon"})
    for d in _POLY_2024 + _YF_2024 + _YF_2025:
        rows.append({"ticker": "AAA", "date": dt.datetime(d.year, d.month, d.day),
                     "close": 5.0, "source": "yfinance"})
    return rows


class _FakeStore:
    """`mongo_store.find_docs` over the fixture, honouring the real contract.

    The date range and the projection are both applied, because both are part
    of what is being tested: a port that forgets `source` in the projection
    cannot pin anything, and a port that pins per chunk only shows its hand
    when the chunks are actually served separately.
    """

    def __init__(self, docs: list[dict]):
        self.docs = docs
        self.calls: list[tuple] = []

    def __call__(self, collection, query, sort=None, projection=None,
                 limit=0, session=None):
        self.calls.append((collection, query, projection))
        assert collection == "price_history", (
            f"read {collection!r}; the helpers resolve the Postgres table name "
            "themselves, so the call site must pass the TABLE name")
        rng = query.get("date", {})
        lo, hi = rng.get("$gte"), rng.get("$lte")
        out = []
        for d in self.docs:
            day = d["date"].date().isoformat()
            if lo is not None and day < lo:
                continue
            if hi is not None and day > hi:
                continue
            if projection:
                keep = {k for k, v in projection.items() if v and k != "_id"}
                out.append({k: v for k, v in d.items() if k in keep})
            else:
                out.append(dict(d))
        return out


def check_one_vendor_panel(load_panel, docs=None) -> pd.DataFrame:
    """Assertions on any `load_panel(start, end)`. Returns the panel."""
    fake = _FakeStore(docs if docs is not None else _docs())
    with patch.object(mongo_store, "find_docs", fake):
        panel = load_panel("2024-12-01", "2025-01-31")

    assert not panel.empty, (
        "empty panel from a seeded fixture — the read matched nothing")

    # the read asked for `source`, or nothing downstream could pin a vendor
    for collection, query, projection in fake.calls:
        assert projection and projection.get("source"), (
            f"projection {projection!r} omits `source`: a read that never "
            "fetches the vendor cannot choose one")
        assert query.get("close") == {"$ne": None, "$gt": 0}, (
            f"close filter is {query.get('close')!r}, not the translation of "
            "`close IS NOT NULL AND close > 0`")

    # the panel index is calendar dates, the shape psycopg handed back
    assert all(isinstance(d, dt.date) and not isinstance(d, dt.datetime)
               for d in panel.index), (
        f"panel index holds {type(panel.index[0])!r}; the SQL version returned "
        "datetime.date and the printed date range depends on it")

    zzz = panel["ZZZ"].dropna()

    # 1. every surviving ZZZ close is yfinance's, none is polygon's
    assert set(zzz.unique()) == {YF_CLOSE}, (
        f"ZZZ carries {sorted(set(zzz.unique()))}; {POLY_CLOSE} is polygon's "
        "raw-convention print and must not appear once yfinance is the "
        "dominant vendor for the window")

    # 2. the four polygon-only December dates are dropped from ZZZ, and are
    #    still in the index because AAA covers them — so this is a statement
    #    about the vendor pin, not about the calendar
    for d in _PG_ONLY_2024:
        assert d in panel.index, "fixture broken: AAA should hold the index open"
        assert pd.isna(panel.at[d, "ZZZ"]), (
            f"{d} survives in ZZZ, but only polygon printed it — a per-CHUNK "
            "pin keeps these, a whole-window pin drops them")

    # 3. and the yfinance dates are all there, 2024 half included: the pin
    #    dropped off-vendor rows, not history
    assert set(zzz.index) == set(_YF_2024 + _YF_2025), (
        f"ZZZ covers {len(zzz)} sessions, expected "
        f"{len(_YF_2024) + len(_YF_2025)}")

    # 4. the single-vendor ticker is untouched
    assert set(panel["AAA"].dropna().unique()) == {5.0}
    return panel


def test_panel_pins_one_vendor_over_the_whole_window():
    check_one_vendor_panel(fb.load_panel)


def test_panel_is_chunked_by_year_and_still_pins_across_the_chunks():
    """The chunking is load-bearing for the trap above, so pin that it happens.

    If a later edit drops the year loop the whole-window/per-chunk distinction
    stops being testable, and `check_one_vendor_panel` would keep passing while
    guarding nothing.
    """
    fake = _FakeStore(_docs())
    with patch.object(mongo_store, "find_docs", fake):
        fb.load_panel("2024-12-01", "2025-01-31")
    years = {q["date"]["$gte"][:4] for _c, q, _p in fake.calls}
    assert years == {"2024", "2025"}, (
        f"expected one read per calendar year, got ranges for {sorted(years)}")


def test_empty_read_returns_an_empty_frame_not_a_crash():
    fake = _FakeStore([])
    with patch.object(mongo_store, "find_docs", fake):
        panel = fb.load_panel("2024-12-01", "2025-01-31")
    assert isinstance(panel, pd.DataFrame) and panel.empty
    assert fake.calls, "no read was issued at all"


def test_the_fixture_can_tell_the_two_pin_placements_apart():
    """NEGATIVE CONTROL for the fixture itself.

    `check_one_vendor_panel` is only worth running if the data it feeds can
    distinguish a whole-window pin from a per-chunk one. Resolve the vendor
    against 2024 alone and polygon must win; resolve it against the window and
    yfinance must win. If those two ever agree, the assertions above are
    tautological and this file is decoration.
    """
    from app.quant.returns import keep_dominant_source

    df = pd.DataFrame(_docs())
    df["date"] = pd.to_datetime(df["date"]).dt.date

    window = keep_dominant_source(df)
    assert set(window[window.ticker == "ZZZ"].source.unique()) == {"yfinance"}

    chunk = df[[d.year == 2024 for d in df.date]]
    chunk_pin = keep_dominant_source(chunk)
    assert set(chunk_pin[chunk_pin.ticker == "ZZZ"].source.unique()) == {"polygon"}


def test_the_script_has_no_postgres_surface_left():
    """The port's other half: the module must not be able to reach the archive.

    Mirrors what `scripts/pg_script_inventory.py::classify` looks for, so a
    reintroduced coupling shows up here rather than only in a JSON refresh.
    """
    src = SCRIPT.read_text()
    tree = ast.parse(src)
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {a.name for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    assert not [m for m in imported if "pg_connection" in m or "psycopg" in m], (
        f"still imports a Postgres driver or pool: {sorted(imported)}")

    # comments and docstrings may DISCUSS the migration; code may not do it
    code = "\n".join(line.split("#", 1)[0] for line in src.splitlines())
    for doc in {ast.get_docstring(n, clean=False)
                for n in ast.walk(tree)
                if isinstance(n, (ast.Module, ast.ClassDef, ast.FunctionDef))}:
        if doc:
            code = code.replace(doc, "")
    assert "DATABASE_URL" not in code
    assert not re.search(r"postgres(?:ql)?://", code)
    assert not re.search(r"\bFROM\s+price_history\b", code, re.I), (
        "a SQL literal against the frozen archive is back in the code")
