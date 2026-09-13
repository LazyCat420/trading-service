"""One sector vocabulary, expressed as the PERMITTED set.

`ticker_metadata.sector` carried 19 distinct values on 2026-09-12 because two
vocabularies wrote to one field — GICS off the static S&P list, Yahoo off
yfinance enrichment, last writer wins. So `Healthcare` and `Health Care` were
two sectors, as were `Financials`/`Financial Services`,
`Technology`/`Information Technology`, `Consumer Cyclical`/`Consumer Discretionary`,
`Consumer Defensive`/`Consumer Staples` and `Materials`/`Basic Materials`.

Every group-by-sector split one sector across two labels and reported two
half-size sectors. Sector exposure caps and breadth read that.

THE GUARD IS AN ALLOWLIST, NOT A DENYLIST. Listing the six known-bad spellings
would pass the day a vendor invents a seventh. The canonical set is small,
closed and stated; anything outside it fails.
"""

from __future__ import annotations

import pytest

from app.data.sector_taxonomy import CANONICAL_SECTORS, normalise_sector


@pytest.mark.parametrize("gics,yahoo", [
    ("Health Care", "Healthcare"),
    ("Financials", "Financial Services"),
    ("Information Technology", "Technology"),
    ("Consumer Discretionary", "Consumer Cyclical"),
    ("Consumer Staples", "Consumer Defensive"),
    ("Materials", "Basic Materials"),
])
def test_the_two_vocabularies_collapse_to_one(gics, yahoo):
    """The pairs that were two sectors are now one."""
    assert normalise_sector(gics) == normalise_sector(yahoo) == yahoo


def test_every_canonical_name_is_a_fixed_point():
    """Normalising an already-canonical value must not change it."""
    for name in CANONICAL_SECTORS:
        assert normalise_sector(name) == name


def test_absence_has_exactly_one_spelling():
    """`None`, `''` and `'Unknown'` all meant 'no sector' and only one survives.

    This is why `{"sector": {"$ne": None}}` used to match rows with no sector.
    """
    for absent in (None, "", "   ", "Unknown", "unknown", "None", "n/a", "-"):
        assert normalise_sector(absent) is None, f"{absent!r} must normalise to None"


def test_etf_is_an_asset_class_not_a_sector():
    """`ETF` was one of the 19. A fund's sector is unknown, not 'ETF'."""
    assert normalise_sector("ETF") is None
    assert normalise_sector("etf") is None


def test_an_unknown_spelling_is_dropped_not_invented():
    """A sector we cannot map becomes None — never a guess at the nearest name.

    Dropping is recoverable (the row simply has no sector and the backfill can
    revisit it). Guessing writes a wrong label that reads as authoritative.
    """
    assert normalise_sector("Frobnicators") is None
    assert normalise_sector("Tech Sector Q3") is None


def test_the_fixture_can_tell_a_mapped_value_from_a_dropped_one():
    """Non-vacuity control: if everything returned None these tests prove nothing."""
    assert normalise_sector("Health Care") == "Healthcare"
    assert normalise_sector("Frobnicators") is None


@pytest.mark.real_mongo
def test_the_live_store_holds_only_canonical_sectors(real_mongo):
    """The stored vocabulary must be a SUBSET of the permitted set.

    Marked `real_mongo` so it runs deliberately against an isolated database
    rather than reaching production in the default suite.
    """
    from app.db import mongo_store

    stored = {s for s in mongo_store.distinct_values("ticker_metadata", "sector")
              if s not in (None, "")}
    extra = stored - set(CANONICAL_SECTORS)
    assert not extra, (
        f"ticker_metadata.sector holds {len(extra)} non-canonical value(s): "
        f"{sorted(extra)}. Run scripts/backfill_sector_taxonomy.py --apply, or "
        f"add the spelling to sector_taxonomy._ALIASES if it is a real synonym."
    )
