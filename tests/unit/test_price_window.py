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

import collections
import datetime
import os
import pathlib
import subprocess
import sys
from unittest.mock import patch

import pytest

from app.collectors import price_window as pw


TODAY = datetime.date(2026, 9, 12)


#: The clustered collection failure the live store was carrying on 2026-09-12:
#: SDA and MNTS each lost these eight sessions, kept a CURRENT newest bar, and
#: so looked healthy to every recency gate in the codebase.
HOLE_START = datetime.date(2026, 5, 6)
HOLE_END = datetime.date(2026, 5, 15)

#: Real NYSE closures per year. Deliberately not the module's allowance: a
#: complete series really is short by about this much, and it must NOT widen.
REAL_HOLIDAYS_PER_YEAR = 10

#: yfinance's period strings, spelled out here rather than imported from the
#: code under test — a test that re-derives the table it is checking cannot
#: fail. (Same reason the parametrised expectations below are written out.)
PERIOD_DAYS = {"5d": 5, "1mo": 30, "3mo": 91, "6mo": 182, "1y": 365,
               "2y": 730, "5y": 1825, "10y": 3650}

#: Captured BEFORE the autouse fixture below patches it, so the sweep section
#: can exercise the real thing.
REAL_IS_FULL_SWEEP_DAY = pw.is_full_sweep_day


@pytest.fixture(autouse=True)
def _not_a_sweep_day():
    """Hold the periodic full sweep off for every test that is not about it.

    `incremental_days_back` widens to the full window on each (ticker, source)
    pair's own sweep day. That is a property of the CALENDAR, so leaving it
    live would widen an arbitrary subset of the narrowing tests below
    depending on which date TODAY is set to — a test that passes because of
    today's date is a test that fails on some other day. The sweep has its own
    section, which calls `REAL_IS_FULL_SWEEP_DAY` directly.
    """
    with patch.object(pw, "is_full_sweep_day", return_value=False):
        yield


def _weekdays(first, last):
    """Mon-Fri days in [first, last], counted the slow obvious way.

    An INDEPENDENT oracle: `pw.weekdays_between` does the same sum in closed
    form, and this loop exists so a bug in that arithmetic cannot hide behind
    expectations computed with it.
    """
    if last < first:
        return 0
    return sum(
        1
        for i in range((last - first).days + 1)
        if (first + datetime.timedelta(days=i)).weekday() < 5
    )


def _complete_sessions(oldest, newest):
    """How many bars a WHOLE series spanning these dates holds — every weekday
    less the market holidays that really fall in it."""
    span = (newest - oldest).days + 1
    return _weekdays(oldest, newest) - round(span * REAL_HOLIDAYS_PER_YEAR / 365.0)


def _stored(value, oldest=None, sessions=None, span_days=365):
    """Patch the window probe to report a series whose newest bar is `value`.

    The probe answers (min(date), max(date), distinct sessions) in one
    aggregation. By default the series described is COMPLETE — it holds every
    session its own span implies, less a realistic holiday count — so a test
    that says nothing about holes gets a series with none.

    Pass `sessions` to describe a holed series, or `oldest` to move the start
    (a ticker first collected partway through the window).
    """
    if value is None:
        return patch("app.db.mongo_query.agg_row", return_value=(None, None, 0))
    newest_date = value.date() if isinstance(value, datetime.datetime) else value
    if oldest is None:
        oldest = newest_date - datetime.timedelta(days=span_days)
    if sessions is None:
        sessions = _complete_sessions(oldest, newest_date)
    return patch("app.db.mongo_query.agg_row", return_value=(oldest, value, sessions))


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


# --------------------------------------------------------------------------
# THE HOLE. A window measured from max(date) cannot see one — and the wide
# window it replaced was, undocumented, the only thing that refilled them.
#
# Measured against the live store on 2026-09-12, the day the narrowing
# shipped: 41 of 715 yfinance tickers with a CURRENT newest bar held 313
# interior holes between them. SDA and MNTS each held 199 of 251 sessions,
# missing 2026-05-06..15 and 2026-06-08..17 — and the vendor still serves
# every one of those bars.
# --------------------------------------------------------------------------

SDA_OLDEST = datetime.date(2025, 9, 12)
SDA_NEWEST = datetime.date(2026, 9, 11)   # current: no gap at the tail at all
SDA_SESSIONS = 199                        # of 251; the rest is the May/June hole


def test_an_interior_hole_widens_the_request_far_enough_to_reach_it():
    """The regression this module was rewritten for.

    Asserted as a PROPERTY — the first day we ask the vendor for is at or
    before the first missing session — not as a day count, so it survives any
    retuning of the overlap, the sweep interval or the tolerance. Before the
    fix this returned 6 days (gap 1 + overlap 5), reaching back only to
    2026-09-06 and leaving a 129-day-old hole permanent.
    """
    with _stored(SDA_NEWEST, oldest=SDA_OLDEST, sessions=SDA_SESSIONS):
        days = pw.incremental_days_back("SDA", "yfinance", 365, today=TODAY)
    first_day_requested = TODAY - datetime.timedelta(days=days)
    assert first_day_requested <= HOLE_START, (
        f"asked the vendor for {days} days, back to {first_day_requested}; "
        f"the missing sessions start {HOLE_START} and would stay missing"
    )


@pytest.mark.parametrize("requested", ["6mo", "1y"])
def test_an_interior_hole_widens_the_yfinance_period_far_enough_to_reach_it(requested):
    """The same property through the period string yfinance actually takes.

    `6mo` is what `collect_price_history` passes by default, so this is the
    live path: narrowed to `1mo` it could not reach 54 days back, let alone
    129.
    """
    with _stored(SDA_NEWEST, oldest=SDA_OLDEST, sessions=SDA_SESSIONS):
        period = pw.narrow_yf_period("SDA", requested, today=TODAY)
    reaches_back_to = TODAY - datetime.timedelta(days=PERIOD_DAYS[period])
    assert reaches_back_to <= HOLE_START, (
        f"{requested} was narrowed to {period}, which only reaches "
        f"{reaches_back_to}; the hole starts {HOLE_START}"
    )


def test_the_detector_catches_a_hole_of_a_fortnight_at_most():
    """How big must a hole be before the count check sees it?

    The answer is a tolerance, not a constant: a count cannot tell a missing
    session from a market holiday, so the module carries an allowance and this
    test measures what it comes to rather than pinning it. The property is the
    SENSITIVITY — a fortnight of missing sessions in a year must be caught,
    because that is the size of the failures the live store was carrying
    (SDA/MNTS 52 sessions, CHMP 31, NGTF 30, SKYI 30). Anything smaller is
    left to the periodic sweep, which is tested below.
    """
    complete = _complete_sessions(SDA_OLDEST, SDA_NEWEST)

    def widens(hole):
        with _stored(SDA_NEWEST, oldest=SDA_OLDEST, sessions=complete - hole):
            return pw.incremental_days_back("SDA", "yfinance", 365, today=TODAY) == 365

    smallest_caught = next((h for h in range(1, 61) if widens(h)), None)
    assert smallest_caught is not None, "no hole size at all triggers a re-fetch"
    assert smallest_caught <= 10, (
        f"the count check only notices a hole of {smallest_caught} sessions; "
        "two trading weeks must not pass as holidays"
    )


@pytest.mark.parametrize("span_days", [30, 91, 182, 365, 730])
def test_a_series_short_only_by_real_holidays_is_never_re_fetched(span_days):
    """The false-positive guard, and the whole perf case.

    A complete series IS short of its weekday count — by the ~10 sessions a
    year the exchange is closed. If that read as a hole, every ticker would
    take the full window on every call and the narrowing would be undone. This
    is measured live: across the 674 yfinance tickers with no hole, the worst
    normalised to 12.5 missing weekdays per year.
    """
    newest = TODAY - datetime.timedelta(days=1)
    with _stored(newest, oldest=newest - datetime.timedelta(days=span_days)):
        days = pw.incremental_days_back("AAPL", "yfinance", 365, today=TODAY)
    assert days < 365, "a whole series was re-fetched in full"


def test_a_series_that_trades_every_day_never_looks_holed():
    """Crypto and FX hold MORE bars than there are weekdays. The comparison
    has to fail safe in that direction or those tickers widen forever."""
    oldest, newest = SDA_OLDEST, SDA_NEWEST
    every_day = (newest - oldest).days + 1
    with _stored(newest, oldest=oldest, sessions=every_day):
        assert pw.incremental_days_back("BTC-USD", "yfinance", 365, today=TODAY) < 365


def test_a_ticker_first_collected_partway_through_the_window_is_not_widened():
    """A series that starts inside the window is short for a reason no
    re-fetch can mend (it listed, or we started collecting, in March). The
    shortfall is measured between the bars we HOLD, not from the edge of the
    request, so this stays narrow instead of asking for a year every call."""
    newest = TODAY - datetime.timedelta(days=1)
    with _stored(newest, oldest=datetime.date(2026, 3, 2)):
        assert pw.incremental_days_back("NEWLY", "yfinance", 365, today=TODAY) < 365


# --------------------------------------------------------------------------
# The probe itself: one aggregation, bounded to the window, scoped to the
# source, and fatal to nothing.
# --------------------------------------------------------------------------

def test_the_window_probe_is_bounded_and_source_scoped():
    """One `$match`, carrying all three of the facts the decision needs.

    The bound is what keeps the upgraded probe free: measured 2026-09-12
    against the live store, AAPL's unbounded max(date) took 34.6 ms and this
    bounded three-accumulator version 6.2 ms.
    """
    with patch("app.db.mongo_query.agg_row",
               return_value=(SDA_OLDEST, SDA_NEWEST, 251)) as agg:
        pw.incremental_days_back("AAPL", "polygon", 365, today=TODAY)
    collection, query, aggs = agg.call_args[0]
    assert collection == "price_history"
    assert query["ticker"] == "AAPL"
    assert query["source"] == "polygon", "a fresh yfinance row must not shorten polygon"
    assert query["date"]["$gte"] == TODAY - datetime.timedelta(days=365)
    # DISTINCT sessions, not a row count: a duplicate row would otherwise pad
    # the total and hide the very hole this exists to find.
    assert ("count_distinct", "date") in aggs


def test_a_probe_row_of_the_wrong_shape_fails_open():
    """Unpacking happens inside the guard, so a store that answers something
    unexpected costs a wide fetch, never an exception in a collector."""
    with patch("app.db.mongo_query.agg_row", return_value=(TODAY,)):
        assert pw.incremental_days_back("AAPL", "yfinance", 365, today=TODAY) == 365


def test_weekdays_between_matches_a_day_by_day_count():
    """`weekdays_between` sums in closed form; `_weekdays` counts. They must
    agree for every start weekday and every span length, or the hole check is
    measuring a span nobody has."""
    first = datetime.date(2026, 1, 1)
    for start in range(9):           # every weekday, plus a wrap
        for length in range(0, 40):
            a = first + datetime.timedelta(days=start)
            b = a + datetime.timedelta(days=length)
            assert pw.weekdays_between(a, b) == _weekdays(a, b), (a, b)
    assert pw.weekdays_between(datetime.date(2026, 5, 5), datetime.date(2026, 5, 4)) == 0


# --------------------------------------------------------------------------
# The periodic full sweep — the guarantee the count check sits on top of.
# A hole of one or two sessions is indistinguishable from a holiday, so the
# only thing that can promise those heal is asking for everything sometimes.
# --------------------------------------------------------------------------

def _sweep_day_for(ticker, source="yfinance", start=None):
    start = start or TODAY
    for i in range(pw.FULL_SWEEP_EVERY_N_DAYS):
        day = start + datetime.timedelta(days=i)
        if REAL_IS_FULL_SWEEP_DAY(ticker, source, today=day):
            return day
    raise AssertionError(f"{ticker} has no sweep day in an interval")


@pytest.mark.parametrize("ticker", ["AAPL", "SDA", "MNTS", "NEWCO", "BRK-B", "000660.KS"])
def test_every_pair_sweeps_exactly_once_per_interval(ticker):
    """Not "roughly sometimes": exactly one day in every N consecutive days,
    for every ticker, wherever you start counting. A sweep that can be missed
    is not a guarantee, and one that can repeat is a slow fetch for nothing."""
    for offset in range(pw.FULL_SWEEP_EVERY_N_DAYS):
        start = TODAY + datetime.timedelta(days=offset)
        hits = sum(
            1
            for i in range(pw.FULL_SWEEP_EVERY_N_DAYS)
            if REAL_IS_FULL_SWEEP_DAY(ticker, "yfinance",
                                      today=start + datetime.timedelta(days=i))
        )
        assert hits == 1, f"{ticker} swept {hits} times from {start}"


def test_the_sweep_is_scoped_per_source_like_everything_else():
    """yfinance and polygon own separate series, so they sweep on separate
    days — one vendor's wide fetch says nothing about the other's holes."""
    days = {
        source: _sweep_day_for("SDA", source)
        for source in ("yfinance", "polygon", "fmp")
    }
    assert len(set(days.values())) > 1, f"every source sweeps together: {days}"


def test_the_sweep_day_does_not_move_between_processes():
    """`hash()` is salted per process: using it would give every ticker a new
    sweep day after every deploy, and a restart could skip a ticker's turn
    outright. Two interpreters with different hash seeds must agree."""
    module = pathlib.Path(pw.__file__).resolve()
    snippet = (
        "import datetime, importlib.util;"
        f"spec=importlib.util.spec_from_file_location('pw', r'{module}');"
        "m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m);"
        "print([i for i in range(14) if m.is_full_sweep_day('SDA','yfinance',"
        "today=datetime.date(2026,9,12)+datetime.timedelta(days=i))])"
    )
    seen = set()
    for seed in ("0", "1", "12345"):
        env = {**os.environ, "PYTHONHASHSEED": seed}
        out = subprocess.run([sys.executable, "-c", snippet], capture_output=True,
                             text=True, env=env, timeout=60)
        assert out.returncode == 0, out.stderr
        seen.add(out.stdout.strip())
    assert len(seen) == 1, f"the sweep day moved with the hash seed: {seen}"


def test_the_sweep_spreads_the_fleet_across_the_interval():
    """One day in N per ticker is only affordable if the days are ~1/N of the
    fleet each. A slot function that clumped would put the whole watchlist on
    one wide day."""
    fleet = [f"TK{i}" for i in range(700)]
    per_day = collections.Counter(
        next(i for i in range(pw.FULL_SWEEP_EVERY_N_DAYS)
             if REAL_IS_FULL_SWEEP_DAY(t, "yfinance", today=TODAY + datetime.timedelta(days=i)))
        for t in fleet
    )
    assert len(per_day) == pw.FULL_SWEEP_EVERY_N_DAYS
    mean = len(fleet) / pw.FULL_SWEEP_EVERY_N_DAYS
    assert max(per_day.values()) < 2 * mean, per_day


def test_the_sweep_widens_a_series_that_looks_perfect():
    """On its own day, a current and complete series is re-fetched whole
    anyway — that is what heals the holes too small to count."""
    ticker = "AAPL"
    sweep_day = _sweep_day_for(ticker)
    with patch.object(pw, "is_full_sweep_day", REAL_IS_FULL_SWEEP_DAY):
        with _stored(sweep_day - datetime.timedelta(days=1)):
            assert pw.incremental_days_back(ticker, "yfinance", 365, today=sweep_day) == 365


def test_the_day_after_its_sweep_a_ticker_is_narrow_again():
    """The sweep is periodic, not sticky: if it did not narrow back the next
    day, the whole fleet would drift to the full window within a week."""
    ticker = "AAPL"
    next_day = _sweep_day_for(ticker) + datetime.timedelta(days=1)
    with patch.object(pw, "is_full_sweep_day", REAL_IS_FULL_SWEEP_DAY):
        with _stored(next_day - datetime.timedelta(days=1)):
            assert pw.incremental_days_back(ticker, "yfinance", 365, today=next_day) < 365


def test_a_hole_too_small_to_count_still_heals_on_the_sweep_day():
    """The layering, stated as a test: HIMS was missing exactly 2 sessions of
    251 — below anything a count can separate from a holiday — so the count
    check leaves it narrow, and the sweep is what covers it, within a week."""
    ticker = "HIMS"
    complete = _complete_sessions(SDA_OLDEST, SDA_NEWEST)
    quiet_day = _sweep_day_for(ticker) + datetime.timedelta(days=1)
    with patch.object(pw, "is_full_sweep_day", REAL_IS_FULL_SWEEP_DAY):
        with _stored(SDA_NEWEST, oldest=SDA_OLDEST, sessions=complete - 2):
            narrow = pw.incremental_days_back(ticker, "yfinance", 365, today=quiet_day)
        with _stored(SDA_NEWEST, oldest=SDA_OLDEST, sessions=complete - 2):
            swept = pw.incremental_days_back(
                ticker, "yfinance", 365, today=_sweep_day_for(ticker))
    assert narrow < 365, "a 2-session shortfall is holiday noise, not a hole"
    assert swept == 365, "and the sweep is what makes it heal anyway"


def test_an_interval_of_one_sweeps_every_day():
    """The escape hatch: set the interval to 1 and every call is a full fetch,
    i.e. the pre-narrowing behaviour, with no other change."""
    assert REAL_IS_FULL_SWEEP_DAY("ANY", "yfinance", today=TODAY, every_n_days=1)
    assert REAL_IS_FULL_SWEEP_DAY("ANY", "yfinance", today=TODAY + datetime.timedelta(days=1),
                                  every_n_days=1)


# --------------------------------------------------------------------------
# The batch refresh rides the same clock.
#
# `BootService._sp500_daily_refresh_loop` is the only writer that covers the
# ~650 tickers the per-cycle collectors never touch (the cycle analyses ~6 a
# day). It was named as the backstop for the narrowing and asked for `5d`, so
# it healed nothing; it now widens on one day in seven. These live here, next
# to the clock they share, because the two must not drift apart.
# --------------------------------------------------------------------------

def test_the_batch_refresh_widens_on_its_sweep_day_and_not_otherwise():
    from app.services.boot_service import BootService

    with patch.object(pw, "is_full_sweep_day", return_value=True):
        assert BootService._refresh_period() == "6mo"
    with patch.object(pw, "is_full_sweep_day", return_value=False):
        assert BootService._refresh_period() == "5d"


def test_the_batch_refresh_has_no_hardcoded_window_left():
    """A guard on the SHAPE of the call, not a list of banned literals: every
    `_sp500_full_refresh` in the daily loop must take its period from a call,
    which is the only form that can ever widen. `period="5d"` written back in
    by a later edit fails here rather than four months later in the store."""
    import ast

    from app.services import boot_service

    tree = ast.parse(pathlib.Path(boot_service.__file__).read_text())
    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "_sp500_full_refresh"
    ]
    assert calls, "no refresh call found — did the loop move?"
    for call in calls:
        periods = [kw.value for kw in call.keywords if kw.arg == "period"]
        assert periods, "the refresh must be told which window to ask for"
        assert all(isinstance(v, ast.Call) for v in periods), (
            "a literal period cannot widen: the sp500 refresh is the only "
            "writer covering the tickers the cycle never analyses"
        )
