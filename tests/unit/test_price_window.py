"""The incremental price window: ask the vendor for the gap, not the archive.

Every `collect_price_history` used to request its full window on every call and
write each bar back with `insert_only=True`. Measured 2026-09-12 against the
live store, 252 bars cost 4.40 s of per-row round-trips per ticker per call and
every write was a no-op. These tests pin the narrowing rules — above all the
ones that must NOT narrow, because collecting too little is a hole in the
series and a hole is what the agents read as a price.

`price_window` imports `mongo_query` INSIDE the function, so the patch target
is the source module (`app.db.mongo_query.agg_row`). Patching an attribute on
`price_window` would leave the real function in place and these tests would
quietly read production.
"""

import datetime
from unittest.mock import patch

import pytest

from app.collectors import price_window as pw


TODAY = datetime.date(2026, 9, 12)


def _stored(value):
    """Patch the max(date) probe to report `value` as the newest stored bar."""
    return patch("app.db.mongo_query.agg_row", return_value=(value,))


def _probe_raises():
    return patch("app.db.mongo_query.agg_row", side_effect=RuntimeError("mongo down"))


# --------------------------------------------------------------------------
# The window must stay WIDE in every case where we cannot prove it can shrink.
# --------------------------------------------------------------------------

def test_cold_start_asks_for_the_full_window():
    """A ticker with no stored bars still gets its whole backfill."""
    with _stored(None):
        assert pw.incremental_days_back("NEWCO", "yfinance", 365, today=TODAY) == 365


def test_probe_failure_fails_open_to_the_full_window():
    """An unreachable DB must not silently shrink every request to nothing."""
    with _probe_raises():
        assert pw.incremental_days_back("AAPL", "yfinance", 365, today=TODAY) == 365


def test_a_future_dated_bar_does_not_shrink_the_window():
    """Clock skew or a bad vendor row would otherwise yield a negative gap."""
    with _stored(TODAY + datetime.timedelta(days=3)):
        assert pw.incremental_days_back("AAPL", "yfinance", 365, today=TODAY) == 365


def test_a_gap_wider_than_the_full_window_is_capped():
    """Never ask for more than the caller wanted."""
    with _stored(datetime.date(2020, 1, 1)):
        assert pw.incremental_days_back("AAPL", "yfinance", 365, today=TODAY) == 365


# --------------------------------------------------------------------------
# ...and SHRINK when the series is current.
# --------------------------------------------------------------------------

def test_a_current_series_asks_only_for_the_overlap():
    """Nothing missing => just the overlap, so a partial last bar is retried."""
    with _stored(TODAY):
        got = pw.incremental_days_back("AAPL", "yfinance", 365, overlap_days=5, today=TODAY)
    assert got == 5


def test_a_one_day_gap_asks_for_the_gap_plus_the_overlap():
    with _stored(TODAY - datetime.timedelta(days=1)):
        got = pw.incremental_days_back("AAPL", "yfinance", 365, overlap_days=5, today=TODAY)
    assert got == 6


def test_a_month_long_gap_is_covered_whole():
    """A ticker that went stale gets every missing session, not just the tail."""
    with _stored(TODAY - datetime.timedelta(days=30)):
        got = pw.incremental_days_back("AAPL", "yfinance", 365, overlap_days=5, today=TODAY)
    assert got == 35


def test_the_window_is_never_zero():
    """A zero-day request would fetch nothing at all."""
    with _stored(TODAY):
        assert pw.incremental_days_back("AAPL", "yfinance", 365, overlap_days=0, today=TODAY) >= 1


# --------------------------------------------------------------------------
# Source scoping: each vendor owns its own series.
# --------------------------------------------------------------------------

def test_the_probe_is_scoped_to_one_source():
    """Polygon must not be told it is current because yfinance wrote a row.

    Polygon is the last fallback — it is called precisely because the earlier
    vendors left the newest session missing. A probe that took max(date) across
    all sources would see the fresh yfinance bar and skip the fetch.
    """
    with patch("app.db.mongo_query.agg_row", return_value=(TODAY,)) as agg:
        pw.latest_stored_date("AAPL", "polygon")
    _collection, query, _aggs = agg.call_args[0]
    assert query == {"ticker": "AAPL", "source": "polygon"}


def test_a_datetime_from_mongo_is_reduced_to_a_date():
    """Mongo stores these as datetimes; the arithmetic is in dates."""
    with _stored(datetime.datetime(2026, 9, 11, 13, 30)):
        assert pw.latest_stored_date("AAPL", "yfinance") == datetime.date(2026, 9, 11)


# --------------------------------------------------------------------------
# yfinance takes a coarse period string rather than a day count.
# --------------------------------------------------------------------------

@pytest.mark.parametrize("gap_days,expected", [
    (0, "5d"),      # current: 5 days of overlap fits the smallest period
    (1, "1mo"),     # 6 needed  -> 5d is too small, 1mo is the next up
    (25, "1mo"),    # 30 needed -> exactly fills 1mo
    (30, "3mo"),    # 35 needed -> spills past 1mo
    (100, "6mo"),   # 105 needed
    (200, "1y"),    # 205 needed
])
def test_narrow_period_picks_the_smallest_covering_period(gap_days, expected):
    """Expectations are written out, not recomputed from _YF_PERIODS: a test
    that re-derives the table from the code under test cannot fail."""
    with _stored(TODAY - datetime.timedelta(days=gap_days)):
        assert pw.narrow_yf_period("AAPL", "1y", today=TODAY) == expected


def test_narrow_period_never_widens_the_caller_request():
    """A caller asking for 1mo must never be handed 1y."""
    with _stored(datetime.date(2020, 1, 1)):  # huge gap
        assert pw.narrow_yf_period("AAPL", "1mo", today=TODAY) == "1mo"


def test_narrow_period_passes_unknown_periods_through():
    """`max`/`ytd` and any future period string are not ours to reinterpret."""
    with _stored(TODAY):
        assert pw.narrow_yf_period("AAPL", "max", today=TODAY) == "max"
        assert pw.narrow_yf_period("AAPL", "ytd", today=TODAY) == "ytd"


def test_narrow_period_falls_open_on_a_probe_failure():
    with _probe_raises():
        assert pw.narrow_yf_period("AAPL", "1y", today=TODAY) == "1y"


def test_cold_start_keeps_the_full_period():
    with _stored(None):
        assert pw.narrow_yf_period("NEWCO", "1y", today=TODAY) == "1y"


def test_narrow_period_clamps_even_if_the_day_count_exceeds_the_request():
    """The never-widen guard, exercised directly.

    `incremental_days_back` caps at the requested window, so in normal
    operation the clamp in `narrow_yf_period` is unreachable — which means a
    mutation removing it survives every other test here. Driving the day count
    past the request pins the property itself rather than today's arithmetic.
    """
    # 365 is deliberate: it is COVERED by a real period ("1y"), so without the
    # clamp the function returns "1y" for a caller that asked for "1mo". A
    # value past the end of the table (9999) falls through to the same answer
    # by accident and kills nothing.
    with patch.object(pw, "incremental_days_back", return_value=365):
        assert pw.narrow_yf_period("AAPL", "1mo", today=TODAY) == "1mo"
