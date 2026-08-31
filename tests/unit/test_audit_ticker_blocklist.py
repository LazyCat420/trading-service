"""`scripts/audit_ticker_blocklist.py` re-derives the slang split, and reads MongoDB.

WHAT WAS ACTUALLY WRONG, AND WHY IT DID NOT LOOK WRONG
------------------------------------------------------
The depth half of this audit used to run `count(DISTINCT date) FROM
price_history` through `scripts.migration.pg_connection`. Postgres stopped
taking writes at the 2026-08-19 cutover, so on 2026-08-30 the script still
exited 0, still printed a full 134-line report, and still said

    DEPTH — median across all of price_history is 4,769 bars.

which is the ARCHIVE's median. The live store's is 4,743, over 2,895 tickers
rather than 2,886, and 22 of the 121 symbols the report lists had gained
sessions the archive cannot see (+155 in total, none lost). Nothing in the
output said which store answered. That is the failure mode this file exists to
prevent coming back: not a crash, a plausible report about a frozen collection.

The tests below would all have been RED against the pre-port file — the two
end-to-end ones because `main()` reached `pg_connection` and printed
"(depth check skipped — database unreachable: ...)" instead of a DEPTH block,
`test_no_postgres_coupling` because the import was still there, and the
`percentile_disc` ones because the function did not exist. Verified by running
this file against the previous revision, not assumed.
"""

from __future__ import annotations

import math
import statistics
from pathlib import Path

import pytest

from scripts import audit_ticker_blocklist as audit

SOURCE = Path(audit.__file__).read_text(encoding="utf-8")


# ── the store it reads ───────────────────────────────────────────────

def test_no_postgres_coupling():
    """The residual grep, as a test: nothing here may reach the frozen archive."""
    for needle in ("psycopg", "DATABASE_URL", "pg_connection", "dbname=",
                   "get_db("):
        assert needle not in SOURCE, (
            f"{needle!r} is still in audit_ticker_blocklist.py — the depth "
            "half is reading the archive again, which answers 2026-08-19 "
            "without saying so"
        )


def test_the_depth_read_names_the_postgres_table_not_a_collection():
    """`collection_for()` is called once, inside the helper. A resolved name
    passed in would resolve twice and, the day renames are switched on, read a
    collection nobody writes."""
    assert 'distinct_values("price_history"' in SOURCE


# ── percentile_disc: Postgres semantics, not statistics.median ───────

@pytest.mark.parametrize("values,expected", [
    ([1, 2, 3], 2),                 # odd: the middle
    ([1, 2, 3, 4], 2),              # even: the LOWER middle, a real member
    ([4743], 4743),                 # one ticker
    ([1, 1, 1, 9], 1),
])
def test_percentile_disc_is_discrete(values, expected):
    assert audit.percentile_disc(values) == expected


def test_percentile_disc_is_not_the_arithmetic_median():
    """The negative control, and it is not hypothetical.

    Against the archive's 2,886 per-ticker counts Postgres answered 4,769 and
    `statistics.median` answers 4,771.5 — a value no ticker has. The number is
    printed as a bar count and divided by four to threshold real bar counts, so
    an interpolated midpoint is the wrong object even when it is close.
    """
    values = [10, 20, 30, 40]
    assert audit.percentile_disc(values) == 20
    assert statistics.median(values) == 25.0
    assert audit.percentile_disc(values) != statistics.median(values)


def test_percentile_disc_of_nothing_is_none_not_zero():
    """Postgres returns NULL for an empty ordered set. Returning 0 would make
    every symbol pass the `n < median / 4` test and the report read clean."""
    assert audit.percentile_disc([]) is None


# ── the depth read: distinct dates, all vendors ──────────────────────

class _FakeStore:
    """Just enough of `mongo_store` for the two calls this script makes."""

    def __init__(self, docs: list[tuple[str, str, str]]):
        # (ticker, date, source)
        self.docs = docs
        self.queries: list[dict] = []

    def distinct_values(self, collection, field, query=None):
        assert collection == "price_history", collection
        self.queries.append({"field": field, "query": query})
        if field == "ticker":
            return sorted({t for t, _d, _s in self.docs})
        assert field == "date"
        t = (query or {}).get("ticker")
        return sorted({d for tk, d, _s in self.docs if tk == t})


@pytest.fixture
def fake_store(monkeypatch):
    def _install(docs):
        store = _FakeStore(docs)
        monkeypatch.setattr("app.db.mongo_store.distinct_values",
                            store.distinct_values)
        return store
    return _install


def test_depth_counts_distinct_dates_not_vendor_prints(fake_store):
    """TRAP 8, as a negative control.

    `price_history`'s natural key is (ticker, date, source) and two vendors
    disagree by 20% on average, so three documents can be two sessions. A
    `count_docs`-based implementation returns 3 here and inflates every depth
    number in the report — which is the flaw this script's first draft shipped.
    """
    fake_store([("APP", "2026-08-01", "yfinance"),
                ("APP", "2026-08-01", "polygon"),
                ("APP", "2026-08-02", "yfinance")])
    assert audit.distinct_dates("APP") == 2


def test_depth_does_not_pin_a_vendor(fake_store):
    """The other half of the same rule.

    A distinct set of DATES cannot be inflated by a duplicate print, so the
    read is vendor-immune by construction and must stay vendor-AGNOSTIC:
    pinning yfinance here would report one vendor's coverage under a heading
    that says `price_history`, and would drop the two sessions only polygon
    carries.
    """
    store = fake_store([("APP", "2026-08-01", "yfinance"),
                        ("APP", "2026-07-01", "polygon"),
                        ("APP", "2026-07-02", "polygon")])
    assert audit.distinct_dates("APP") == 3
    date_queries = [q["query"] for q in store.queries if q["field"] == "date"]
    assert date_queries == [{"ticker": "APP"}]
    for q in date_queries:
        assert "source" not in q, (
            "the depth read pinned a vendor; the median it is compared against "
            "counts every vendor's sessions, so the two sides would no longer "
            "be the same measurement"
        )


def test_depth_by_ticker_is_the_group_by(fake_store):
    fake_store([("A", "d1", "y"), ("A", "d2", "y"), ("A", "d2", "p"),
                ("B", "d1", "y")])
    assert audit.depth_by_ticker() == {"A": 2, "B": 1}


# ── end to end ───────────────────────────────────────────────────────

def _listings_dir(tmp_path: Path, symbols: list[str]) -> Path:
    d = tmp_path / "listings"
    d.mkdir()
    rows = "\n".join(f"{s}|{s} Test Corp - Common Stock|Q|N|N|100|N|N"
                     for s in symbols)
    # The trailer line is part of the real file and becomes a phantom listing
    # if it is not skipped, so it is included here deliberately.
    (d / "nasdaqlisted.txt").write_text(
        "Symbol|Security Name|Market Category|Test Issue|Financial Status|"
        "Round Lot Size|ETF|NextShares\n" + rows +
        "\nFile Creation Time: 0830202621:31|||||||\n")
    (d / "otherlisted.txt").write_text(
        "ACT Symbol|Security Name|Exchange|CQS Symbol|ETF|Round Lot Size|"
        "Test Issue|NASDAQ Symbol\n"
        "File Creation Time: 0830202621:31|||||||\n")
    return d


def test_the_trailer_line_is_not_a_listing(tmp_path):
    listings = audit.load_listings(_listings_dir(tmp_path, ["APP", "GOLD"]))
    assert set(listings) == {"APP", "GOLD"}


def test_main_prints_depth_from_mongo(tmp_path, monkeypatch, capsys, fake_store):
    """The whole report, against a store standing in for Mongo.

    RED before the port: `main()` went to `pg_connection`, whose pool raises
    under the test fixtures, and the run ended at
    "(depth check skipped — database unreachable: ...)" with no DEPTH block at
    all.
    """
    fake_store(
        [("APP", f"d{i}", "yfinance") for i in range(3)]
        + [("GOLD", f"d{i}", "yfinance") for i in range(40)]
        + [("GOLD", "d0", "polygon")]                       # same session twice
        + [("ZZZZ", f"d{i}", "yfinance") for i in range(400)]
    )
    monkeypatch.setattr(audit, "load_listings",
                        lambda offline: {"APP": "AppLovin", "GOLD": "Barrick",
                                         "UN": "Unilever"})
    monkeypatch.setattr("sys.argv", ["audit_ticker_blocklist.py"])

    assert audit.main() == 0
    out = capsys.readouterr().out

    # median over {APP:3, GOLD:40, ZZZZ:400} -> percentile_disc(0.5) = 40
    assert "DEPTH — median across all of price_history is 40 bars." in out
    assert "  GOLD       40 bars" in out         # 41 documents, 40 sessions
    assert "  APP         3 bars  ← under a quarter of median" in out
    assert "  1 of 3 have no rows at all" in out  # UN is listed, has no rows
    # ORDER BY 2 DESC survived the port
    assert out.index("GOLD") < out.index("APP")


def test_main_says_so_when_price_history_is_empty(tmp_path, monkeypatch,
                                                  capsys, fake_store):
    """`percentile_disc` of nothing is NULL, and the old code formatted NULL
    with `:,` — a TypeError where the honest answer is one line of English."""
    fake_store([])
    monkeypatch.setattr(audit, "load_listings", lambda offline: {"APP": "AppLovin"})
    monkeypatch.setattr("sys.argv", ["audit_ticker_blocklist.py"])

    assert audit.main() == 0
    out = capsys.readouterr().out
    assert "price_history holds no rows" in out
    assert "DEPTH" not in out


def test_a_dead_store_is_reported_not_raised(monkeypatch, capsys):
    """Exit code 0 and a printed reason, exactly as the Postgres version did —
    this audit's first half is useful with no database at all."""
    def _boom(*_a, **_k):
        raise RuntimeError("no route to host")

    monkeypatch.setattr("app.db.mongo_store.distinct_values", _boom)
    monkeypatch.setattr(audit, "load_listings", lambda offline: {"APP": "AppLovin"})
    monkeypatch.setattr("sys.argv", ["audit_ticker_blocklist.py"])

    assert audit.main() == 0
    out = capsys.readouterr().out
    assert "depth check skipped" in out and "no route to host" in out
    assert "explicit-fetch blocklist is" in out
