"""Replay real price_history against candidate freshness thresholds.

The question Direction 4 needs answered is NOT "how old is the data today"
(one Sunday sample tells us nothing) but "for a given as-of date, how many
tickers would each threshold gate, and is that number stable across weekdays".

Method: for each simulated "today" (a real trading day in the DB), compute the
age of each ticker's newest bar AS OF THAT DAY -- i.e. only using rows with
date <= that day, exactly what the pipeline would have seen. Then apply each
candidate rule and count how many tickers it would block.

Crucially this measures CALENDAR age and TRADING-DAY age separately, because
the whole "2 days old on a Sunday is fine, 2 days old on a Wednesday is not"
distinction lives in that gap.

Run it anywhere the app's own MongoDB is reachable -- from the repo, or inside
the container. It needs no DSN of its own:

    python scripts/simulate_freshness_thresholds.py active

WHY THAT INVOCATION CHANGED (2026-08-30)
----------------------------------------
It used to be run with a hand-built SQL connection string, exported as SIM_DSN
and read from `app.config.settings`. Both halves of that line are now dead:
the settings attribute it interpolated no longer exists (`AttributeError` on
today's Settings), and the SQL archive stopped taking writes at the 2026-08-19
cutover -- it still ANSWERS, which is the dangerous part. Run against the
archive on 2026-08-30 this script printed a clean "76 sessions 2026-05-01 ..
2026-08-19, 0 tickers blocked" for the active watchlist: a healthy-looking
table, eleven days out of date, describing a store the pipeline no longer
reads. It now reads MongoDB, through the app's own connection.

Populations: `active` (default, what a trade gate would act on), `watchlist`
(active+paused), `sp500`, or `all`. They answer different questions -- see the
2026-07-26 finding: the active watchlist is ~96% fresh while the non-S&P-500
backfill population is ~99% stalled, so a threshold measured over `all` would
describe a table nobody trades from.

AND `all` IS WHERE THE TWO STORES VISIBLY DISAGREE
--------------------------------------------------
Measured 2026-08-30 over the FROZEN half (2026-05-01 .. 2026-08-19, the last
day the archive holds), with the archive's own 2886-ticker universe forced on
both sides: `active`, `watchlist` and `sp500` reproduce the archive's table
row for row, and so do the 76 trading sessions. `all` does not -- all ten rows
move, by 20-75 tickers per rule (as of 2026-08-19: >1trd 2198 -> 2123,
>2trd 2174 -> 2121, >5cal 2151 -> 2120, >10trd 2003 -> 1981).

That gap is DATA, not translation. 114 of the 2886 tickers hold a different
set of bar dates, and 113 of them GAINED days in Mongo -- a backfill that ran
after the cutover filled in coverage the archive never received (AAP: 53 days
in the window in the archive, 76 in Mongo, the new days carry both vendors).
Those tickers are genuinely fresher than the archive believed, so fewer of
them are blocked. The only ticker where the archive holds days Mongo's reads
do not is NVDA, and those 9 days are exactly the `world_simulator` rows
excluded below: drop that one filter and Mongo is a strict superset of the
archive for every ticker, 0 days missing. That is the check to re-run if these
numbers ever have to be defended again.
"""

from __future__ import annotations

import os
import sys
from collections import defaultdict
from datetime import date, datetime

# Run-from-anywhere bootstrap, and it is load-bearing for the invocation above.
# The SQL version imported nothing from `app`, so `python scripts/<this>.py`
# worked from any cwd; the port's `app.db` dependency broke that with a bare
# `ModuleNotFoundError: No module named 'app'` at import time unless the caller
# happened to have PYTHONPATH set (the container does, via the Dockerfile's
# `ENV PYTHONPATH=/app` -- which is exactly why the breakage was invisible
# there). 34 other scripts in this directory carry this same line.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.db import mongo_store  # noqa: E402

# The same floor the SQL passed as the '2026-05-01' parameter. `date_fields`
# turns it into the naive-midnight datetime the collection stores, so the
# comparison is against a BSON Date and not a string (a string bound would
# match nothing here, because Date outranks String in BSON type order).
_WINDOW_START = date(2026, 5, 1)

# `world_simulator` is a SYNTHETIC vendor -- 58 rows, all NVDA, newest
# 2026-05-25 -- and a synthetic bar must not make a ticker look fresh. The
# live consumer of this measurement excludes it by name
# (`cycle_scheduler._run_watchlist_price_refresh`), so the simulation does too
# or it would be scoring a gate against a population the gate does not see.
#
# The two REAL vendors are deliberately NOT collapsed to one. price_history's
# key is (ticker, date, source) and the vendors disagree on prices, so any read
# of a PRICE has to pin one. This script reads no prices: it asks "what is the
# newest bar we hold for this ticker", which is the quantity the refresh
# scheduler acts on (`_stalest_first(universe, "price_history", "date")`,
# unpinned by design). Pinning a vendor here would report ONE vendor's
# coverage while claiming to report the store's -- measured 2026-08-30, 33 of
# the 63 active tickers carry both vendors with DIFFERENT newest dates, so the
# choice is not cosmetic.
_REAL_VENDOR = {"$ne": "world_simulator"}

# A real session has most of the daily-updating universe present. Sessions come
# from the data itself -- never a hardcoded calendar. A day the market was shut
# simply has no rows, so it can't become a simulated "today".
_SESSION_MIN_TICKERS = 100

_POPULATIONS = {
    "active": lambda: mongo_store.distinct_values(
        "watchlist", "ticker", {"status": "active"}),
    "watchlist": lambda: mongo_store.distinct_values(
        "watchlist", "ticker", {"status": {"$in": ["active", "paused"]}}),
    "sp500": lambda: mongo_store.distinct_values(
        "ticker_metadata", "ticker", {"sp500": True}),
    # `all` is the one population whose filter the SQL did NOT have: the
    # original was a bare `SELECT DISTINCT ticker FROM price_history`. Adding
    # the vendor exclusion keeps the universe consistent with the coverage
    # measured below (a ticker that exists only because a synthetic bar was
    # written for it is not a ticker the pipeline has data for). Measured
    # 2026-08-30 it drops NOTHING -- 2895 distinct tickers filtered and
    # unfiltered alike, because world_simulator's 58 rows are all NVDA, which
    # has real bars too. It is a guard against a future store, not a change to
    # today's number.
    "all": lambda: mongo_store.distinct_values(
        "price_history", "ticker", {"source": _REAL_VENDOR}),
}


def _as_date(value) -> date:
    """A stored bar date back to the `datetime.date` the PG column handed over.

    BSON has no date type, so the migration stored each `date` as naive
    midnight UTC. Everything below subtracts these, compares them and prints
    them, and a `datetime` prints as "2026-08-19 00:00:00" where the column
    printed "2026-08-19" -- so the conversion happens once, here at the read
    seam, rather than at four format strings that would drift apart.
    """
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10])


def load_universe(population: str) -> list[str]:
    """The tickers the chosen population contains, deduplicated and sorted."""
    tickers = _POPULATIONS[population]()
    return sorted({t for t in tickers if t})


def load_sessions(floor: date) -> list[date]:
    """Real trading days on/after `floor`, oldest first."""
    rows = mongo_store.aggregate("price_history", [
        {"$match": {"date": {"$gte": floor}, "source": _REAL_VENDOR}},
        # DISTINCT (date, ticker) BEFORE counting. The key is
        # (ticker, date, source), so a second vendor's print of the same
        # ticker-day is a duplicate ROW, not another ticker -- counting rows
        # would let a dual-source day clear the 100-ticker bar on 50 tickers.
        # This is the `COUNT(DISTINCT ticker)` the SQL spelled out.
        {"$group": {"_id": {"date": "$date", "ticker": "$ticker"}}},
        {"$group": {"_id": "$_id.date", "n": {"$sum": 1}}},
        # the HAVING
        {"$match": {"n": {"$gte": _SESSION_MIN_TICKERS}}},
        {"$sort": {"_id": 1}},
    ])
    return [_as_date(r["_id"]) for r in rows]


def load_bar_dates(universe: list[str], floor: date) -> dict[str, list[date]]:
    """Every date each ticker has a bar for, on/after `floor`, sorted.

    Distinct (ticker, date) pairs rather than rows: two vendor prints of one
    ticker-day are one day of coverage, and the ages computed below only ever
    ask which DAYS exist.
    """
    groups = mongo_store.aggregate("price_history", [
        {"$match": {"ticker": {"$in": list(universe)},
                    "date": {"$gte": floor},
                    "source": _REAL_VENDOR}},
        {"$group": {"_id": {"ticker": "$ticker", "date": "$date"}}},
    ])

    by_ticker: dict[str, list[date]] = defaultdict(list)
    for g in groups:
        key = g["_id"]
        by_ticker[key["ticker"]].append(_as_date(key["date"]))
    for dates in by_ticker.values():
        dates.sort()
    return by_ticker


def trading_day_age(sessions: list, latest, asof) -> int:
    """Sessions strictly after `latest`, up to and including `asof`.

    This is the quantity a horizon should be expressed in. Calendar age counts
    weekends and holidays the market was closed for, which is why a Monday
    reading of "3 days old" and a Wednesday reading of "3 days old" describe
    completely different situations.
    """
    return sum(1 for s in sessions if latest < s <= asof)


def main(argv: list[str] | None = None) -> None:
    # `argv` is the argument list WITHOUT the program name, defaulting to the
    # real one. Passing it explicitly is what lets a test drive the report;
    # reading sys.argv directly made the population depend on pytest's own
    # command line.
    args = sys.argv[1:] if argv is None else list(argv)
    population = args[0] if args else "active"
    if population not in _POPULATIONS:
        raise SystemExit(f"population must be one of {sorted(_POPULATIONS)}")

    universe = load_universe(population)
    sessions = load_sessions(_WINDOW_START)
    by_ticker = load_bar_dates(universe, _WINDOW_START)

    # An empty read is the failure this measurement exists to catch, so say so
    # instead of printing a well-formatted table of nothing.
    if not sessions:
        raise SystemExit(
            f"no trading sessions on/after {_WINDOW_START} carry "
            f"{_SESSION_MIN_TICKERS}+ tickers -- price_history is empty, "
            "unreachable, or its `date` field is not a BSON date")
    if not universe:
        raise SystemExit(f"population={population} is empty -- nothing to simulate")

    print(f"population={population} universe={len(universe)} tickers, "
          f"{len(sessions)} sessions {sessions[0]} .. {sessions[-1]}\n")

    # Simulate the last 10 real sessions as "today".
    print(f"{'as-of':<12} {'dow':<4} {'n':>4} "
          f"{'cal_age p50/p90':>16} {'trd_age p50/p90':>16} "
          f"{'>1trd':>6} {'>2trd':>6} {'>5cal':>6} {'>10trd':>7} {'nodata':>7}")
    print("-" * 96)

    for asof in sessions[-10:]:
        cal_ages, trd_ages = [], []
        no_data = 0
        for ticker in universe:
            seen = [d for d in by_ticker.get(ticker, []) if d <= asof]
            if not seen:
                no_data += 1
                continue
            latest = seen[-1]
            cal_ages.append((asof - latest).days)
            trd_ages.append(trading_day_age(sessions, latest, asof))

        if not cal_ages:
            continue

        def pct(values: list, p: float) -> int:
            s = sorted(values)
            return s[min(int(len(s) * p), len(s) - 1)]

        # Candidate rules, counted as "how many tickers would this block".
        gt1_trd = sum(1 for a in trd_ages if a > 1)
        gt2_trd = sum(1 for a in trd_ages if a > 2)
        gt5_cal = sum(1 for a in cal_ages if a > 5)     # today's _STALE_AFTER_DAYS
        gt10_trd = sum(1 for a in trd_ages if a > 10)

        n = len(cal_ages)
        print(f"{str(asof):<12} {asof.strftime('%a'):<4} {n:>4} "
              f"{pct(cal_ages,0.5):>7}/{pct(cal_ages,0.9):<8} "
              f"{pct(trd_ages,0.5):>7}/{pct(trd_ages,0.9):<8} "
              f"{gt1_trd:>6} {gt2_trd:>6} {gt5_cal:>6} {gt10_trd:>7} {no_data:>7}")

    print("\n(counts are TICKERS BLOCKED by each candidate rule, out of n)")


if __name__ == "__main__":
    main()
