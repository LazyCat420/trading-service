"""One sector vocabulary, and the map from every spelling that reached the store.

`ticker_metadata.sector` held **19** distinct values (measured 2026-09-12) because
two vocabularies were writing to one field. `app/data/sp500_universe.py` reads the
GICS name off the static `SP500_TICKERS` list (line 63) and then, when `enrich` is
on, overwrites it with whatever yfinance returns (line 80) — which is Yahoo's.
Whichever source answered last is what got stored, per ticker, per run.

The result is that `Healthcare` and `Health Care` are the same sector under two
labels, as are `Financials`/`Financial Services`,
`Technology`/`Information Technology`, `Consumer Cyclical`/`Consumer Discretionary`,
`Consumer Defensive`/`Consumer Staples` and `Materials`/`Basic Materials`. Every
group-by-sector — exposure caps, sector breadth, correlation buckets — silently
splits one sector in two and reports two half-size sectors where there is one.
That is a risk-control input, not a cosmetic problem.

WHY YAHOO IS THE TARGET. `company_registry` already holds exactly Yahoo's 11, and
the live enrichment path is yfinance, so normalising toward Yahoo renames the
minority of rows and leaves the enrichment path writing its native value.

`ETF` IS NOT A SECTOR. It was one of the 19. An ETF's sector is unknown, not
"ETF" — the fund type belongs in `asset_class`, which `etf_collector` already
sets. `normalise_sector` maps it to None so the two facts stop sharing a field.

ABSENCE HAS ONE SPELLING: None. The store also held `""` and `"Unknown"`, so a
`{"sector": {"$ne": None}}` filter matched rows with no sector. All three now
normalise to None.
"""

from __future__ import annotations

#: The permitted set. A guard asserts the stored vocabulary is a SUBSET of this,
#: expressed as what is ALLOWED rather than what is banned — a denylist cannot
#: see the next vendor's spelling, and this field has already taken two.
CANONICAL_SECTORS: frozenset[str] = frozenset({
    "Basic Materials",
    "Communication Services",
    "Consumer Cyclical",
    "Consumer Defensive",
    "Energy",
    "Financial Services",
    "Healthcare",
    "Industrials",
    "Real Estate",
    "Technology",
    "Utilities",
})

#: Every non-canonical spelling observed in the live store on 2026-09-12, mapped
#: to its canonical name. Keys are compared case-insensitively and whitespace
#: stripped, so "health care" and "Health  Care" both resolve.
_ALIASES: dict[str, str] = {
    # GICS -> Yahoo
    "health care": "Healthcare",
    "financials": "Financial Services",
    "information technology": "Technology",
    "consumer discretionary": "Consumer Cyclical",
    "consumer staples": "Consumer Defensive",
    "materials": "Basic Materials",
    "telecommunication services": "Communication Services",
    "telecom services": "Communication Services",
    "information tech": "Technology",
    # spacing / punctuation variants
    "real-estate": "Real Estate",
    "consumer-cyclical": "Consumer Cyclical",
}

#: Values that mean "we do not know", in any casing.
_ABSENT = frozenset({"", "unknown", "none", "null", "n/a", "na", "-", "etf"})


def normalise_sector(value: object) -> str | None:
    """Canonical sector name, or None when the value means 'unknown'.

    Returns None rather than raising on an unrecognised string: a collector
    meeting a genuinely new sector must not be able to stop a cycle. The guard
    test is what makes a new spelling loud, at build time, where it is cheap.
    """
    if value is None:
        return None
    text = str(value).strip()
    if not text or text.lower() in _ABSENT:
        return None
    if text in CANONICAL_SECTORS:
        return text
    alias = _ALIASES.get(text.lower())
    if alias:
        return alias
    # Case-only difference from a canonical name ("healthcare" -> "Healthcare").
    for canon in CANONICAL_SECTORS:
        if canon.lower() == text.lower():
            return canon
    return None
