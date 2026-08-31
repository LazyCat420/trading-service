"""`refresh_technicals.py` must ask the LIVE store which tickers are stale.

It repairs `technicals`, the table every desk's RSI/ATR is read from, so the
list it computes decides what gets recomputed. Until this port it asked the
Postgres archive — frozen at the 2026-08-19 cutover — and since the archive DSN
field was deleted on 08-28 it did not even do that: every run died with
`AttributeError` inside the migration package's connector before its first
query. A repair tool that cannot name what is broken repairs nothing.

The three things the port could have got wrong quietly:

1. WHERE IT READS. Source-level, because there is no run-time symptom to catch:
   a Postgres-backed run and a Mongo-backed run print the same two counters.

2. WHICH BAR "current" MEANS. `price_history` is keyed (ticker, date, source)
   and the vendors disagree, so `MAX(date)` across vendors is not the newest
   bar the repair can use — `compute_technicals` pins the ticker's dominant
   vendor and can only write technicals up to THAT vendor's newest bar.
   Measured on the live collection 2026-08-30: 180 of 2,895 tickers carry two
   vendors and 30 have a dominant vendor behind the all-vendor max. EAT is the
   worst, and its numbers are the fixture below — yfinance to 2026-08-14 over
   10,735 rows, polygon to 2026-08-17 over 251, technicals at 2026-08-14, i.e.
   exactly current. Against the all-vendor maximum EAT scores 3 days stale on
   every run, forever; at `--stale-days 2` every repair recomputes it and
   nothing changes.

3. WHETHER `--dry-run` IS ONE. `compute_technicals` WRITES. The single-ticker
   branch called it before the flag was ever read, so `--ticker X --dry-run`
   wrote 500 indicator rows and printed a count.

RED BEFORE THE PORT, and red for each way the port could have gone wrong.
Verified 2026-08-30 by loading mutated copies of the module in place of the
shipped one and running these same assertions against them, not by assertion:

    control (as shipped)                13 pass /  0 fail
    the pre-port Postgres file           1 pass / 12 fail
    benchmark = all-vendor MAX(date)    10 pass /  3 fail
    vendor pin = depth only             10 pass /  3 fail
    --dry-run ignored for --ticker      11 pass /  2 fail
    dates printed with str()            12 pass /  1 fail
"""
from __future__ import annotations

import io
import re
import sys
import tokenize
from datetime import datetime
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from scripts import refresh_technicals as rt  # noqa: E402

SOURCE = (REPO / "scripts" / "refresh_technicals.py").read_text(encoding="utf-8")

#: The seams that mean "this file talks to the frozen archive".
PG_SEAM = re.compile(r"psycopg|DATABASE_URL|pg_connection|dbname=|postgres", re.I)


def code_only(source: str) -> list[tuple[int, str]]:
    """`source` with its comments and string literals removed.

    A docstring that SAYS "this used to read Postgres" is not a coupling, and
    a check that cannot tell the difference forces the port to delete the one
    paragraph explaining itself — `test_prose_about_postgres_is_not_a_coupling`
    in the inventory suite makes the same distinction. What matters is whether
    an import or a call still names the archive.
    """
    out: list[tuple[int, str]] = []
    for tok in tokenize.generate_tokens(io.StringIO(source).readline):
        if tok.type in (tokenize.COMMENT, tokenize.STRING):
            continue
        if tok.type == tokenize.NAME or tok.type == tokenize.OP:
            out.append((tok.start[0], tok.line.rstrip()))
    return out


def D(y: int, m: int, d: int) -> datetime:
    """A BSON date as Mongo hands it back: a datetime at midnight, not a date."""
    return datetime(y, m, d)


# `SELECT ticker, source, MAX(date), count(*) ... GROUP BY ticker, source`, in
# the tuple order the call site asks for. EAT and MSFT are the live shapes
# measured 2026-08-30; DEAD is the RBLX/EC failure mode returns.py records — a
# deep vendor that stopped publishing beside a shallow one that did not.
PRICE_ROWS = [
    ("EAT", "yfinance", D(2026, 8, 14), 10735),
    ("EAT", "polygon", D(2026, 8, 17), 251),
    ("MSFT", "yfinance", D(2026, 8, 28), 10194),
    ("DEAD", "yfinance", D(2026, 8, 1), 10000),
    ("DEAD", "polygon", D(2026, 8, 17), 251),
    ("NEW", "yfinance", D(2026, 8, 28), 40),
]

TECH_ROWS = [
    ("EAT", D(2026, 8, 14)),     # exactly current against its dominant vendor
    ("MSFT", D(2026, 8, 20)),    # genuinely 8 days behind
    ("DEAD", D(2026, 7, 1)),
    # NEW has prices and no technicals at all.
]


@pytest.fixture
def store(monkeypatch):
    """The two grouped scans and the one scalar, served from the rows above."""
    from app.db import mongo_query

    seen: dict[str, list] = {"collections": [], "compute": []}

    def group_rows(collection, query, keys, aggs, select, sort=None, limit=0):
        seen["collections"].append(collection)
        if collection == "price_history":
            tkr = query.get("ticker")
            return [r for r in PRICE_ROWS if not tkr or r[0] == tkr]
        if collection == "technicals":
            return list(TECH_ROWS)
        raise AssertionError(f"unexpected collection {collection!r}")

    def scalar(collection, query, column, sort=None, session=None):
        seen["collections"].append(collection)
        assert collection == "technicals" and column == "date"
        hits = [d for t, d in TECH_ROWS if t == query.get("ticker")]
        return max(hits) if hits else None

    def compute_technicals(ticker, period=500):
        seen["compute"].append(ticker)
        return 287

    monkeypatch.setattr(mongo_query, "group_rows", group_rows)
    monkeypatch.setattr(mongo_query, "scalar", scalar)
    monkeypatch.setattr("app.processors.technical_processor.compute_technicals",
                        compute_technicals)
    return seen


def run(monkeypatch, capsys, *argv) -> tuple[int, str]:
    monkeypatch.setattr(sys, "argv", ["refresh_technicals.py", *argv])
    code = rt.main()
    return code, capsys.readouterr().out


# ── 1. where it reads ────────────────────────────────────────────────
def test_the_module_names_no_postgres_seam():
    hits = sorted({(n, line) for n, line in code_only(SOURCE) if PG_SEAM.search(line)})
    assert not hits, ("refresh_technicals.py still names a Postgres seam:\n  "
                      + "\n  ".join(f"line {n}: {line}" for n, line in hits))


def test_that_check_would_have_failed_the_file_it_replaced():
    """Negative control, both ways.

    Without the first half the assertion above passes on any file that merely
    says nothing — including one that says nothing because the tokenizer broke.
    Without the second it passes only on a port that also deleted the paragraph
    explaining what it ported off, which is how the reasons get lost."""
    before = "from scripts.migration.pg_connection import get_db\nx = 1\n"
    assert [l for _, l in code_only(before) if PG_SEAM.search(l)]

    prose = '''"""This used to read the Postgres archive via psycopg."""\nx = 1\n'''
    assert not [l for _, l in code_only(prose) if PG_SEAM.search(l)]


def test_the_scans_name_postgres_TABLES_not_resolved_collections(store, monkeypatch, capsys):
    """`collection_for()` is called once, inside the helper. A call site that
    resolves the name itself resolves it twice, and the day renames are turned
    on the read misses and the write creates a second, invisible collection."""
    run(monkeypatch, capsys, "--dry-run")
    assert set(store["collections"]) == {"price_history", "technicals"}


# ── 2. which bar "current" means ─────────────────────────────────────
def test_the_benchmark_is_the_dominant_vendors_newest_bar(store):
    newest, vendor, any_vendor = rt.newest_price_by_ticker()["EAT"]
    assert (newest, vendor) == (D(2026, 8, 14), "yfinance")
    # The all-vendor answer is kept, and it is NOT the benchmark: if these two
    # were equal the pin would be decorative and this file could not tell.
    assert any_vendor == D(2026, 8, 17)
    assert newest != any_vendor


def test_freshness_outranks_depth(store):
    """A vendor that stopped publishing loses to a shallower current one.

    Depth alone picked a dead series in cycle-v3-1785504601 (RBLX/EC, yfinance
    frozen while polygon carried on), which is why `_one_vendor` orders on
    freshness first. DEAD is 40x deeper on yfinance and must still lose."""
    newest, vendor, any_vendor = rt.newest_price_by_ticker()["DEAD"]
    assert (newest, vendor) == (D(2026, 8, 17), "polygon")
    assert any_vendor == D(2026, 8, 17)


def test_the_pin_decides_the_stale_verdict(store, monkeypatch, capsys):
    """EAT is current against the vendor the recompute reads, and 3 days behind
    the all-vendor maximum. At --stale-days 2 the two answers disagree, and
    only the pinned one is repairable."""
    code, out = run(monkeypatch, capsys, "--dry-run", "--stale-days", "2")
    assert code == 0
    listed = [line for line in out.splitlines() if line.strip().startswith("EAT ")]
    assert not listed, f"EAT was flagged stale against a bar it cannot reach:\n{out}"
    assert "MSFT" in out and "DEAD" in out
    assert "1 ticker(s) have a fresher print from another vendor" in out


def test_the_lag_is_measured_against_the_pinned_bar(store, monkeypatch, capsys):
    """DEAD's lag is 2026-08-17 minus 2026-07-01 = 47 days, not the 31 days a
    depth-only pin (yfinance, 2026-08-01) would report."""
    code, out = run(monkeypatch, capsys, "--dry-run")
    row = [l for l in out.splitlines() if l.strip().startswith("DEAD ")]
    assert row, out
    assert "47d" in row[0] and "polygon" in row[0]


def test_a_ticker_with_no_technicals_at_all_is_never_not_zero(store, monkeypatch, capsys):
    """`lag is None` is the sentinel for "never computed" — a day count would
    collide with the genuine 20,000-day lags the ASC-limit bug produced."""
    code, out = run(monkeypatch, capsys, "--dry-run")
    assert "never computed: 1" in out
    assert "stale by >3d           : 3" in out


# ── 3. whether --dry-run is one ──────────────────────────────────────
def test_a_single_ticker_dry_run_writes_nothing(store, monkeypatch, capsys):
    code, out = run(monkeypatch, capsys, "--ticker", "EAT", "--dry-run")
    assert code == 0
    assert store["compute"] == [], "a dry run called compute_technicals, which WRITES"
    assert "EAT: last_price 2026-08-14 (yfinance) | last_tech 2026-08-14 | lag 0d" in out
    assert "DRY RUN" in out


def test_a_bulk_dry_run_writes_nothing(store, monkeypatch, capsys):
    code, out = run(monkeypatch, capsys, "--dry-run")
    assert code == 0 and store["compute"] == []


def test_a_single_ticker_without_dry_run_still_recomputes(store, monkeypatch, capsys):
    code, out = run(monkeypatch, capsys, "--ticker", "msft")
    assert code == 0
    assert store["compute"] == ["MSFT"]
    assert "MSFT: 287 rows written" in out


def test_an_unknown_ticker_says_so_instead_of_computing(store, monkeypatch, capsys):
    code, out = run(monkeypatch, capsys, "--ticker", "ZZZZ", "--dry-run")
    assert code == 0 and store["compute"] == []
    assert "no price history" in out


# ── the report is still a report ─────────────────────────────────────
def test_dates_print_as_calendar_days(store, monkeypatch, capsys):
    """Postgres returned `date`; Mongo returns `datetime`. `str()` on the
    latter appends ' 00:00:00' and pushes every later column nine characters
    right, which is a silently unreadable table rather than an error."""
    code, out = run(monkeypatch, capsys, "--dry-run")
    assert "00:00:00" not in out
    assert "2026-08-28" in out and "2026-08-20" in out
    assert rt._day(D(2026, 8, 14)) == "2026-08-14"
    assert rt._day(None) == "never"
