"""How much price history a collector actually needs to ask a vendor for.

Every `collect_price_history` used to request its FULL window on every call —
yfinance a year, Polygon 365 days, FMP 365 days — and then write each bar back
with `insert_only=True`. Measured 2026-09-12 against the live store: 252 bars
through the per-row upsert path cost **4.40 s per ticker per call**, and every
one of those writes was a no-op because the rows were already there. On the
75-ticker watchlist that is ~5.5 minutes of a cycle spent asking Mongo to
insert rows it already holds, plus a year of vendor payload per ticker.

**Re-fetching the old bars corrects nothing.** All three collectors write with
`insert_only=True` (`$setOnInsert`), so an existing row is never modified. A
split/dividend restatement of a bar we already stored is therefore invisible to
the full re-fetch as well — the wide window buys no accuracy, only time. The
one writer that DOES update is yfinance's quoted-date repair, and that targets
the newest bar, which is inside any window this module returns.

So the rule is: ask for the gap, plus a small overlap so a partial or
not-yet-settled last bar gets another chance, and fall back to the caller's
full window whenever we hold nothing (cold start) or cannot tell.

Fails OPEN — any probe error returns the caller's original window. Collecting
too much is slow; collecting too little is a hole in the series, and a hole is
what the agents read as a price.
"""

from __future__ import annotations

import datetime
import logging
from typing import Optional

logger = logging.getLogger(__name__)

#: Re-request this many days before the newest bar we hold. Covers a last bar
#: that was stored while still in-progress, and a vendor that settles late.
DEFAULT_OVERLAP_DAYS = 5


def latest_stored_date(ticker: str, source: str) -> Optional[datetime.date]:
    """Newest `price_history` bar held for this (ticker, source), or None.

    Scoped to ONE source on purpose. The collectors key their upserts on
    (ticker, date, source), so each vendor owns its own series; asking "what is
    the newest bar from any source" would let a fresh yfinance row convince the
    Polygon fallback it was already current and skip the very session it was
    called to fetch.
    """
    try:
        from app.db import mongo_query

        row = mongo_query.agg_row(
            "price_history", {"ticker": ticker, "source": source}, [("max", "date")]
        )
        latest = row[0] if row else None
    except Exception as e:  # pragma: no cover - probe must never raise
        logger.debug("[price_window] %s/%s: max(date) probe failed (%s)", ticker, source, e)
        return None

    if latest is None:
        return None
    if isinstance(latest, datetime.datetime):
        return latest.date()
    if isinstance(latest, datetime.date):
        return latest
    logger.debug("[price_window] %s/%s: unexpected max(date) type %r", ticker, source, type(latest))
    return None


def incremental_days_back(
    ticker: str,
    source: str,
    full_days_back: int,
    overlap_days: int = DEFAULT_OVERLAP_DAYS,
    today: Optional[datetime.date] = None,
) -> int:
    """Days of history to request, never more than `full_days_back`.

    Returns `full_days_back` unchanged on a cold start (nothing stored) or any
    probe failure, so a new ticker still gets its full backfill.
    """
    latest = latest_stored_date(ticker, source)
    if latest is None:
        return full_days_back

    today = today or datetime.date.today()
    gap_days = (today - latest).days
    if gap_days < 0:
        # A bar dated in the future (bad vendor row, clock skew). Don't reason
        # from it — ask for the full window and let the writer sort it out.
        logger.debug("[price_window] %s/%s: newest bar %s is ahead of today", ticker, source, latest)
        return full_days_back

    return max(1, min(full_days_back, gap_days + overlap_days))


#: yfinance takes a coarse period string, not a day count. Smallest first; each
#: entry is (period, days it covers).
_YF_PERIODS: tuple[tuple[str, int], ...] = (
    ("5d", 5),
    ("1mo", 30),
    ("3mo", 91),
    ("6mo", 182),
    ("1y", 365),
    ("2y", 730),
    ("5y", 1825),
    ("10y", 3650),
)


def narrow_yf_period(
    ticker: str,
    requested_period: str,
    source: str = "yfinance",
    overlap_days: int = DEFAULT_OVERLAP_DAYS,
    today: Optional[datetime.date] = None,
) -> str:
    """The smallest yfinance period that still covers the gap.

    Never widens: a caller asking for `6mo` can be narrowed to `1mo`, but a
    caller asking for `1mo` is never handed `6mo`. Anything unrecognised
    (`max`, `ytd`, a new period string) is passed through untouched.
    """
    requested_days = dict(_YF_PERIODS).get(requested_period)
    if requested_days is None:
        return requested_period

    needed = incremental_days_back(
        ticker, source, requested_days, overlap_days=overlap_days, today=today
    )
    for period, days in _YF_PERIODS:
        if days >= needed:
            return period if days <= requested_days else requested_period
    return requested_period
