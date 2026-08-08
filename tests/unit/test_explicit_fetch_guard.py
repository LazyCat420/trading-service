"""A slang list must not refuse a listed company (open item 14, 2nd).

`yfinance_collector._is_blocked_ticker` guards the EXPLICIT path — a caller
that named the symbol outright. It was consulting `STATIC_FALSE_TICKERS`, a
324-entry hand-curated slang list built to stop the *text extractor* pulling
"APP" or "NOW" out of prose. 124 of those entries are currently listed US
instruments, so every explicit yfinance fetch for AppLovin, ServiceNow,
Allstate, Gartner, ON Semiconductor and 119 others was refused.

WHY NOTHING CAUGHT IT. Other vendors backfill the recent tip, so `last_price`
reads today for all of them and every freshness check passes. The damage is
DEPTH: 48 bars against a 4,818-bar median. A check that passes for both states
is not a check, and freshness was the only thing being measured.
"""

import pytest

from app.collectors.explicit_fetch_guard import (
    LISTED_ON_THE_SLANG_LIST,
    explicit_fetch_blocklist,
    is_blocked_for_explicit_fetch,
)

#: Named individually because these are the ones a reader recognises, and a
#: regression on any of them is a regression on a real position candidate.
REAL_COMPANIES = [
    ("APP", "AppLovin"), ("NOW", "ServiceNow"), ("ALL", "Allstate"),
    ("IT", "Gartner"), ("ON", "ON Semiconductor"), ("WELL", "Welltower"),
    ("FAST", "Fastenal"), ("OPEN", "Opendoor"), ("RUN", "Sunrun"),
    ("HD", "Home Depot"), ("LOW", "Lowe's"), ("PM", "Philip Morris"),
    ("USB", "U.S. Bancorp"), ("A", "Agilent"), ("SO", "Southern Company"),
]


@pytest.mark.parametrize("ticker,company", REAL_COMPANIES)
def test_a_listed_company_is_never_refused_on_the_explicit_path(ticker, company):
    assert not is_blocked_for_explicit_fetch(ticker), (
        f"{ticker} ({company}) is a listed company — refusing an explicit fetch "
        f"for it caps its history at ~48 bars while every freshness check passes"
    )


def test_the_guard_is_called_through_the_collector_not_reimplemented():
    """`_is_blocked_ticker` is what `data_report` and `collect_price_history`
    actually call. A test that only exercised the new module would pass while
    the collector kept its own copy of the old check — the shape that let a
    blocked trade read as a kept one for weeks (open item 7)."""
    from app.collectors.yfinance_collector import _is_blocked_ticker

    for ticker, company in REAL_COMPANIES:
        assert not _is_blocked_ticker(ticker), f"{ticker} ({company}) still blocked"


def test_slang_with_no_listing_is_still_refused():
    """The 200 that are not listed anywhere keep being refused, which is what
    makes `data_report` classify an empty result as a SKIP rather than an
    outage — without it, a dead symbol reaches the self-healing watchdog as a
    crash."""
    blocked = explicit_fetch_blocklist()
    assert blocked, "the guard refuses nothing at all — data_report loses its skip path"

    still_slang = [t for t in ("THE", "AND", "CEO", "ETF", "USA") if t in blocked]
    assert still_slang, (
        "none of the obvious non-symbols are refused — the split removed the "
        "guard instead of narrowing it"
    )


def test_the_extractor_keeps_the_whole_slang_list():
    """Text extraction is the job the list was built for and must be untouched.
    Narrowing it there would let "the app is down" become a fetch for AppLovin,
    which is the defect the list prevents."""
    from app.processors.ticker_extractor import STATIC_FALSE_TICKERS

    for ticker, _company in REAL_COMPANIES:
        assert ticker in STATIC_FALSE_TICKERS, (
            f"{ticker} was removed from the slang list — the extraction guard "
            "was narrowed, which is not what this fix does"
        )


def test_the_blocklist_is_derived_and_not_a_second_copy():
    """A hand-maintained second copy of a list is the defect class this
    codebase records most often. The blocklist must be computed FROM the slang
    list, so an entry added there is covered without touching this module."""
    from app.processors.ticker_extractor import STATIC_FALSE_TICKERS

    blocked = explicit_fetch_blocklist()
    assert blocked == frozenset(STATIC_FALSE_TICKERS) - LISTED_ON_THE_SLANG_LIST
    assert blocked <= frozenset(STATIC_FALSE_TICKERS), \
        "the guard refuses something the slang list never named"


def test_the_allowlist_only_names_entries_that_are_on_the_slang_list():
    """A stale allowlist entry is dead weight that reads as coverage."""
    from app.processors.ticker_extractor import STATIC_FALSE_TICKERS

    stray = sorted(LISTED_ON_THE_SLANG_LIST - frozenset(STATIC_FALSE_TICKERS))
    assert not stray, (
        f"these are allowlisted but no longer on the slang list: {stray} — "
        "the exception outlived the thing it excepted"
    )


def test_the_snapshot_still_matches_a_listings_file_when_one_is_available():
    """The snapshot's invalidation, run in CI when the fixture is present.

    Skipped rather than networked: a unit test must not depend on
    nasdaqtrader.com being up. `scripts/audit_ticker_blocklist.py` is the
    online version and prints the same diff.
    """
    from pathlib import Path

    fixture = Path(__file__).resolve().parents[1] / "fixtures" / "nasdaq_listings"
    if not (fixture / "nasdaqlisted.txt").exists():
        pytest.skip(
            "no listings fixture — run scripts/audit_ticker_blocklist.py to "
            "check the snapshot against the live directory"
        )

    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
    try:
        from audit_ticker_blocklist import load_listings
    finally:
        sys.path.pop(0)

    from app.processors.ticker_extractor import STATIC_FALSE_TICKERS

    listings = load_listings(fixture)
    fresh = {t for t in STATIC_FALSE_TICKERS if t in listings}
    missing = sorted(fresh - LISTED_ON_THE_SLANG_LIST)
    assert not missing, (
        f"these slang entries are listed instruments and are being refused: "
        f"{missing} — add them to LISTED_ON_THE_SLANG_LIST"
    )
