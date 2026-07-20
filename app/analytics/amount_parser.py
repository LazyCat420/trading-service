"""
Parse congressional disclosure amount ranges into usable dollar estimates.

Congress discloses trade size as a bracket, never an exact figure, and the feed
carries at least three formats depending on when the row was scraped:

    "$1,001 - $15,000"      most common
    "1K-15K"                older scrape format
    "$15,001"               open-ended / lower-bound only
    ""                      missing

Everything downstream must treat the output as an ESTIMATE. We return the
bracket midpoint plus an explicit confidence so charts and agent tools can
distinguish "we know this was a $1k-$15k trade" from "we are guessing".
"""

import re

# Confidence levels, in descending order of how much you should trust the number.
CONFIDENCE_RANGE = "range"      # both bounds known — midpoint is a fair estimate
CONFIDENCE_BOUND = "bound"      # only a lower bound — true size may be far higher
CONFIDENCE_NONE = "none"        # nothing parseable

_SUFFIX_MULTIPLIERS = {"K": 1_000, "M": 1_000_000, "B": 1_000_000_000}

# Matches "$1,001", "1K", "15M", "1,000,001" — with or without $ and separators.
_NUMBER_RE = re.compile(r"\$?\s*([\d,]+(?:\.\d+)?)\s*([KMB])?", re.IGNORECASE)


def _to_float(raw: str, suffix: str | None) -> float | None:
    try:
        value = float(raw.replace(",", ""))
    except ValueError:
        return None
    if suffix:
        value *= _SUFFIX_MULTIPLIERS.get(suffix.upper(), 1)
    return value


def parse_amount_range(amount_range: str | None) -> tuple[float | None, str]:
    """Return (estimated_usd, confidence).

    The estimate is the bracket midpoint when both bounds are present. For an
    open-ended lower bound we return the bound itself rather than inventing a
    ceiling — understating a whale trade is safer than fabricating its size.
    """
    if not amount_range or not amount_range.strip():
        return None, CONFIDENCE_NONE

    matches = _NUMBER_RE.findall(amount_range)
    values = [v for v in (_to_float(raw, suf) for raw, suf in matches) if v is not None]

    if not values:
        return None, CONFIDENCE_NONE

    if len(values) == 1:
        return values[0], CONFIDENCE_BOUND

    low, high = min(values), max(values)
    return (low + high) / 2.0, CONFIDENCE_RANGE
