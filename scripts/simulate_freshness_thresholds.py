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

Run inside the trading-service container so it reads the app's own DSN:

    sudo docker exec trading-service sh -c \
      'SIM_DSN=$(python -c "from app.config import settings; print(settings.DATABASE_URL)") \
       python /tmp/simulate_freshness_thresholds.py active'

Populations: `active` (default, what a trade gate would act on), `watchlist`
(active+paused), `sp500`, or `all`. They answer different questions -- see the
2026-07-26 finding: the active watchlist is ~96% fresh while the non-S&P-500
backfill population is ~99% stalled, so a threshold measured over `all` would
describe a table nobody trades from.
"""

import os
import sys
from collections import defaultdict

import psycopg

DSN = os.environ["SIM_DSN"]

_POPULATIONS = {
    "active": "SELECT ticker FROM watchlist WHERE status = 'active'",
    "watchlist": "SELECT ticker FROM watchlist WHERE status IN ('active','paused')",
    "sp500": "SELECT ticker FROM ticker_metadata WHERE sp500 = TRUE",
    "all": "SELECT DISTINCT ticker FROM price_history",
}

_POPULATION = sys.argv[1] if len(sys.argv) > 1 else "active"
if _POPULATION not in _POPULATIONS:
    raise SystemExit(f"population must be one of {sorted(_POPULATIONS)}")
UNIVERSE_SQL = _POPULATIONS[_POPULATION]

# Real sessions, from the data itself -- never a hardcoded calendar. A day the
# market was shut simply has no rows, so it can't become a simulated "today".
# Threshold: a real session has most of the daily-updating universe present.
SESSIONS_SQL = """
    SELECT date, COUNT(DISTINCT ticker) n
    FROM price_history
    WHERE date >= %s
    GROUP BY date
    HAVING COUNT(DISTINCT ticker) >= 100
    ORDER BY date
"""

BARS_SQL = """
    SELECT ticker, date
    FROM price_history
    WHERE ticker = ANY(%s) AND date >= %s
"""


def trading_day_age(sessions: list, latest, asof) -> int:
    """Sessions strictly after `latest`, up to and including `asof`.

    This is the quantity a horizon should be expressed in. Calendar age counts
    weekends and holidays the market was closed for, which is why a Monday
    reading of "3 days old" and a Wednesday reading of "3 days old" describe
    completely different situations.
    """
    return sum(1 for s in sessions if latest < s <= asof)


def main() -> None:
    with psycopg.connect(DSN) as conn:
        universe = [r[0] for r in conn.execute(UNIVERSE_SQL).fetchall()]
        sessions = [r[0] for r in conn.execute(SESSIONS_SQL, ["2026-05-01"]).fetchall()]
        bars = conn.execute(BARS_SQL, [universe, "2026-05-01"]).fetchall()

    by_ticker = defaultdict(list)
    for ticker, date in bars:
        by_ticker[ticker].append(date)
    for dates in by_ticker.values():
        dates.sort()

    print(f"population={_POPULATION} universe={len(universe)} tickers, "
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
