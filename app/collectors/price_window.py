"""How much price history a collector actually needs to ask a vendor for.

Every `collect_price_history` used to request its FULL window on every call —
yfinance a year, Polygon 365 days, FMP 365 days — and then write each bar back
with `insert_only=True`. Measured 2026-09-12 against the live store: 252 bars
through the per-row upsert path cost **4.40 s per ticker per call**, and every
one of those writes was a no-op because the rows were already there. On the
75-ticker watchlist that is ~5.5 minutes of a cycle spent asking Mongo to
insert rows it already holds, plus a year of vendor payload per ticker.

WHAT THE FIRST VERSION OF THIS MODULE GOT WRONG (2026-09-12, same day)
---------------------------------------------------------------------
It narrowed every window to `max(date)` + a 5-day overlap, justified like
this: "re-fetching corrects nothing, because the collectors write insert_only
($setOnInsert), so an existing row is never modified."

That is true only of rows that EXIST. `$setOnInsert` over a wide window was an
undocumented, load-bearing **self-healing backfill**: every full fetch silently
refilled the bars that were MISSING inside the window. `max(date)` cannot see
an interior hole — a ticker that lost a week last May still has a current
newest bar, so a window measured from `max(date)` covers only recent days and
the hole becomes permanent.

Measured against the live store the day the narrowing shipped: of 715 yfinance
tickers carrying a current bar, **41 held 313 interior holes** — failed
collection weeks (2026-05-06..15, 2026-06-08..17, 2026-07-20/21), with SDA and
MNTS each missing 52 of 251 sessions. The vendor still serves those bars: a
live `yf.Ticker("SDA").history(period="6mo")` returns all eight bars of the May
hole. Only the request had stopped reaching back for them.

A hole is worse than staleness, because nothing reports it. Every freshness
gate keys on recency, never on depth or contiguity, so a holed series passes
`MIN_OBSERVATIONS` and reads as healthy — while `regime_hmm` takes
`np.diff(np.log(closes))` across the hole and manufactures one enormous
log-return, which is the observation that defines the STRESSED volatility
regime.

SO THE RULE IS
--------------
Ask for the gap plus a small overlap — but only while we can SEE that the
series behind that gap is whole. Two independent things widen the window back
to the caller's full request:

1. **A short count.** The probe that finds `max(date)` now returns
   (oldest, newest, sessions held) for the caller's whole window in the SAME
   aggregation, and a series holding materially fewer sessions than its own
   span implies is re-fetched in full. Measured cost: nil — the max-only probe
   already scanned every row of the (ticker, source) partition, so adding the
   other two accumulators and a `$gte` bound on the window made it the same
   speed or faster (AAPL: 34.6 ms max-only over all history -> 6.2 ms for all
   three over a year).
2. **A periodic full sweep.** Each (ticker, source) asks for its whole window
   one day in every `FULL_SWEEP_EVERY_N_DAYS`, chosen by a stable hash of the
   pair, so the fleet is swept evenly and no ticker can go more than a week
   without one wide fetch. This is the guarantee; (1) is only an optimisation
   on top of it, because a count cannot resolve a hole of one or two sessions
   against holiday noise (see `looks_holed`).

Fails OPEN — any probe error returns the caller's original window. Collecting
too much is slow; collecting too little is a hole in the series, and a hole is
what the agents read as a price.

NOT A BACKSTOP: `app/data/sp500_price_collector.py`
---------------------------------------------------
The narrowing commit left the batch collector unnarrowed as the assumed
backstop for everything this module might skip. It is not one: the daily
`_sp500_daily_refresh_loop` calls it with `period="5d"`, and the only 6mo call
site fires when `price_history` is EMPTY, i.e. once in the life of the store.
That is why the May holes survived four months of daily refreshes over the
very tickers that hold them. (The batch path CAN return those bars — measured
2026-09-12, a real 100-ticker `yf.download(..., period="6mo")` chunk returned
all eight May bars for SDA with non-NaN Close, 95 of 100 tickers complete and
the 5 failures genuinely delisted symbols. It was never asked for them.)
Nothing here depends on that collector healing anything; it now runs one wide
sweep a week of its own, on the same clock (see `is_full_sweep_day`).
"""

from __future__ import annotations

import datetime
import logging
import math
import zlib
from typing import NamedTuple, Optional

logger = logging.getLogger(__name__)

#: Re-request this many days before the newest bar we hold. Covers a last bar
#: that was stored while still in-progress, and a vendor that settles late.
DEFAULT_OVERLAP_DAYS = 5

#: Every (ticker, source) gets one full-window fetch in this many days, on a
#: day derived from a stable hash of the pair — so a hole no count check can
#: see still heals within a week, and ~1/N of the fleet pays for it each day.
FULL_SWEEP_EVERY_N_DAYS = 7

#: Sessions a span may be missing to market holidays before the series looks
#: holed rather than merely closed. NYSE closes ~10 full days a year; measured
#: across the 674 live yfinance tickers with no hole, the worst normalised to
#: 12.5/year and the median to exactly 10.0, while every ticker with a real
#: multi-week hole normalised to 19/year or worse. Anything from ~13 to ~19
#: separates the two sets identically, so this sits in the middle of a plateau
#: rather than on a cliff — and it is deliberately on the generous side,
#: because a false positive costs the full window on EVERY call (the
#: narrowing's whole purpose) while a false negative costs at most one sweep
#: interval.
HOLIDAY_SESSIONS_PER_YEAR = 12
HOLIDAY_SLACK_SESSIONS = 2


class StoredWindow(NamedTuple):
    """What we hold for one (ticker, source) inside one request window."""

    oldest: datetime.date
    newest: datetime.date
    sessions: int


def _as_date(value) -> Optional[datetime.date]:
    if isinstance(value, datetime.datetime):
        return value.date()
    if isinstance(value, datetime.date):
        return value
    return None


def stored_window(
    ticker: str,
    source: str,
    since: Optional[datetime.date] = None,
) -> Optional[StoredWindow]:
    """Oldest bar, newest bar and DISTINCT sessions held, or None.

    One aggregation, the same one that used to fetch `max(date)` alone. Scoped
    to ONE source on purpose: the collectors key their upserts on
    (ticker, date, source), so each vendor owns its own series, and asking
    "what is the newest bar from any source" would let a fresh yfinance row
    convince the Polygon fallback it was already current and skip the very
    session it was called to fetch.

    `since` bounds the probe to the window the caller is about to request —
    which is both what the hole check must measure and what makes the probe
    cheaper than the unbounded `max(date)` it replaces.

    Sessions are counted DISTINCT: a duplicate row would otherwise pad the
    count and hide the very hole it is there to find.

    Returns None for "cannot tell" — nothing stored, or any probe failure —
    and every caller reads that as "ask for everything".
    """
    window = {"date": {"$gte": since}} if since is not None else {}
    try:
        from app.db import mongo_query

        # The `source` pin is written into the literal rather than assembled in
        # a variable: `tests/unit/test_price_history_one_vendor_guard.py` reads
        # this call STATICALLY, and a filter it cannot see is a filter it must
        # assume is missing.
        row = mongo_query.agg_row(
            "price_history",
            {"ticker": ticker, "source": source, **window},
            [("min", "date"), ("max", "date"), ("count_distinct", "date")],
        )
        oldest, newest, sessions = _as_date(row[0]), _as_date(row[1]), int(row[2])
    except Exception as e:  # pragma: no cover - probe must never raise
        logger.debug("[price_window] %s/%s: window probe failed (%s)", ticker, source, e)
        return None

    if oldest is None or newest is None or sessions <= 0:
        return None
    return StoredWindow(oldest, newest, sessions)


def latest_stored_date(ticker: str, source: str) -> Optional[datetime.date]:
    """Newest `price_history` bar held for this (ticker, source), or None."""
    window = stored_window(ticker, source)
    return window.newest if window else None


def weekdays_between(first: datetime.date, last: datetime.date) -> int:
    """Mon-Fri days in [first, last], inclusive — the sessions a span COULD
    hold before holidays are taken out. Weekends are the only closure this can
    know without a market calendar; the rest is `HOLIDAY_SESSIONS_PER_YEAR`."""
    if last < first:
        return 0
    days = (last - first).days + 1
    full_weeks, spare = divmod(days, 7)
    count = full_weeks * 5
    weekday = first.weekday()
    for offset in range(spare):
        if (weekday + offset) % 7 < 5:
            count += 1
    return count


def looks_holed(window: StoredWindow) -> bool:
    """Does this series hold fewer sessions than its own span implies?

    Measured only between the OLDEST and NEWEST bars we hold, never from the
    edge of the request: a ticker that listed (or was first collected) partway
    through the window is short for a reason no re-fetch can fix, and must not
    be widened forever.

    A ticker that trades seven days a week (crypto, FX) holds MORE bars than
    there are weekdays, so it can never trip this — which is the safe
    direction.
    """
    span_days = (window.newest - window.oldest).days + 1
    if span_days <= 0:
        return False
    allowance = (
        math.ceil(span_days * HOLIDAY_SESSIONS_PER_YEAR / 365.0)
        + HOLIDAY_SLACK_SESSIONS
    )
    missing = weekdays_between(window.oldest, window.newest) - window.sessions
    return missing > allowance


def is_full_sweep_day(
    key: str,
    source: str,
    today: Optional[datetime.date] = None,
    every_n_days: int = FULL_SWEEP_EVERY_N_DAYS,
) -> bool:
    """Is today this (key, source)'s day to re-fetch its whole window?

    Deterministic and stateless: the slot is a stable checksum of the pair, so
    every key gets exactly one sweep day in every `every_n_days`, the fleet
    spreads evenly across them, and nothing has to be remembered between calls
    or between restarts. `hash()` is NOT usable here — it is salted per
    process, so the same ticker would sweep on a different day after every
    deploy, and a restart could skip a ticker's turn entirely.

    Stateless is also why there is no "already swept today" memo: a memo set
    before the fetch would suppress the retry when that fetch fails, which is
    precisely the case the sweep exists for.
    """
    if every_n_days <= 1:
        return True
    today = today or datetime.date.today()
    slot = zlib.crc32(f"{key}\x00{source}".encode()) % every_n_days
    return today.toordinal() % every_n_days == slot


def incremental_days_back(
    ticker: str,
    source: str,
    full_days_back: int,
    overlap_days: int = DEFAULT_OVERLAP_DAYS,
    today: Optional[datetime.date] = None,
) -> int:
    """Days of history to request, never more than `full_days_back`.

    Returns `full_days_back` unchanged on a cold start (nothing stored), on any
    probe failure, on this pair's periodic sweep day, and whenever the stored
    series is short of the sessions its own span implies — the four cases where
    a narrow window would either under-fill a new series or leave an existing
    hole permanent.
    """
    today = today or datetime.date.today()

    if is_full_sweep_day(ticker, source, today=today):
        return full_days_back

    window = stored_window(ticker, source, since=today - datetime.timedelta(days=full_days_back))
    if window is None:
        return full_days_back

    gap_days = (today - window.newest).days
    if gap_days < 0:
        # A bar dated in the future (bad vendor row, clock skew). Don't reason
        # from it — ask for the full window and let the writer sort it out.
        logger.debug(
            "[price_window] %s/%s: newest bar %s is ahead of today", ticker, source, window.newest
        )
        return full_days_back

    if looks_holed(window):
        logger.info(
            "[price_window] %s/%s: %d sessions held over %s..%s — re-fetching the "
            "full %d-day window to refill the gap",
            ticker, source, window.sessions, window.oldest, window.newest, full_days_back,
        )
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
    # Never widen. `incremental_days_back` already caps at `requested_days`, so
    # this is belt-and-braces — but it is the property callers depend on, and
    # it should hold even if that cap is ever relaxed, so it is enforced here
    # rather than assumed.
    needed = min(needed, requested_days)
    for period, days in _YF_PERIODS:
        if days >= needed:
            return period
    return requested_period
