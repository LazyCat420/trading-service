"""What an EXPLICITLY NAMED ticker may be refused for.

TWO USES, ONE LIST, AND THEY NEEDED DIFFERENT LISTS.

`STATIC_FALSE_TICKERS` (324 entries, `app/processors/ticker_extractor.py`) is a
hand-curated slang/acronym list built for **text extraction** — it is what stops
"the app is down" becoming a fetch for AppLovin. That job is unchanged and this
module does not touch it.

It was also being consulted by `yfinance_collector._is_blocked_ticker`, which
guards the **explicit** path: a watchlist refresh, a precollect, a screener
result — a caller that named the symbol outright. An explicitly named ticker is
not being extracted from anything, so asking whether it *looks like a word* is
the wrong question. The list's own docstring already makes this argument for
the runtime-augmented `FALSE_TICKERS`; it just never carried it one step
further.

MEASURED 2026-08-08, against the NASDAQ Trader symbol directory
(`nasdaqlisted.txt` + `otherlisted.txt`, 13,113 currently listed US symbols):

    STATIC_FALSE_TICKERS          324
      currently listed             124   <- refused on the explicit path
      not listed anywhere          200

Open item 14 spotted **19** of those 124 by eye. By eye is not a measurement:
it finds the names a reader recognises — APP, NOW, GOLD — and misses AleAnna,
Pathward Financial and the other hundred. The correction is 6.5x.

WHAT THE DAMAGE LOOKS LIKE. Not staleness — depth. Other vendors backfill the
recent tip, so `last_price` reads today for every one of them and every
freshness check passes. What is missing is history. Median depth across all of
`price_history` is **4,818 bars**; all 124 are under a quarter of that, 98 have
no rows at all, and fifteen sit at exactly 48:

    ALL ARE COO DD DTE FAST HAS IT LOW ON PM PSA SO TECH WELL   48 bars each
    AI 65 · NOW 109 · BE/NYC/OPEN 251 · TV 264 · A 285 · USB 288 · HD 316
    PICK 514 · APP 550                                  (vs a 4,818 median)

A 200-day SMA, a 52-week range, an ATR over any useful window or a multi-year
margin trend cannot be computed from 48 rows. One of them, `APP`, has already
carried a full six-agent analysis into `decision_scores` on that basis, and its
verdict carries no degradation flag.

THE SPLIT. The explicit path refuses only symbols that are **not listed
anywhere** — the 200. Blocking those costs nothing real and preserves the
behaviour `app/v3/data_report.py` depends on: a refusal is a SKIP, not an
outage, and without it a dead symbol's empty result classifies as a collector
error and reaches the self-healing watchdog as a crash.

STALENESS, AND WHY IT IS BOUNDED. `LISTED_ON_THE_SLANG_LIST` is a snapshot, and
a snapshot has no invalidation of its own — a company listing tomorrow under a
symbol on the slang list would be refused again. Two things bound that:
`tests/unit/test_explicit_fetch_guard.py` re-derives this set from a listings
file whenever one is available, and the guard is subtractive, so the failure
mode of a stale snapshot is the old behaviour for one new symbol rather than a
new class of error.

Note the direction of the check. It is a *never-block* allowlist, not a
universe. Being listed does not make a symbol worth analysing — that is the
screener's decision, and this module has no opinion on it.
"""

from __future__ import annotations

#: Entries of `STATIC_FALSE_TICKERS` that are currently listed US instruments.
#: Measured 2026-08-08 against NASDAQ Trader (13,113 symbols). These are real
#: companies and ETFs; the explicit path must never refuse them.
#:
#: Re-derive with `scripts/audit_ticker_blocklist.py`, which fetches the same
#: directory and prints the difference against this set.
LISTED_ON_THE_SLANG_LIST: frozenset[str] = frozenset({
    'A', 'AD', 'AI', 'ALL', 'AM', 'AN', 'ANNA', 'API', 'APP', 'AR', 'ARE',
    'AS', 'BE', 'BOT', 'BULL', 'BY', 'CARE', 'CASH', 'CDC', 'CFO', 'CIA',
    'COO', 'CORP', 'CTO', 'DC', 'DD', 'DTE', 'EDIT', 'EOD', 'EPS', 'EU',
    'FAST', 'FOR', 'FUND', 'GAIN', 'GAP', 'GO', 'GOLD', 'GOOD', 'GROW',
    'HARD', 'HAS', 'HD', 'HE', 'HERE', 'HIGH', 'HOLD', 'HOPE', 'HR',
    'IMF', 'IMO', 'IPO', 'IRS', 'IT', 'ITM', 'JUST', 'KNOW', 'LAND',
    'LIFE', 'LINE', 'LION', 'LIVE', 'LOW', 'MADE', 'MAX', 'MD', 'MID',
    'MIN', 'MOVE', 'NATO', 'NEAR', 'NEXT', 'NOW', 'NYC', 'OI', 'ON',
    'OPEN', 'OR', 'OUT', 'PAYS', 'PC', 'PI', 'PICK', 'PLAY', 'PM', 'POST',
    'PR', 'PRE', 'PSA', 'PUMP', 'RAM', 'REAL', 'RISE', 'ROCK', 'ROE',
    'RUN', 'SAFE', 'SF', 'SO', 'SSD', 'SURE', 'TALK', 'TECH', 'TIL',
    'TLDR', 'TOP', 'TV', 'TWO', 'UAE', 'UK', 'UN', 'UP', 'USB', 'USD',
    'VOTE', 'VS', 'WANT', 'WAR', 'WEEK', 'WELL', 'WIP', 'YEAR', 'YOLO',
    'YOU',
})

#: The date `LISTED_ON_THE_SLANG_LIST` was measured, so a reader can tell how
#: old the snapshot is without reading git history.
LISTED_SNAPSHOT_DATE = "2026-08-08"


def explicit_fetch_blocklist() -> frozenset[str]:
    """Symbols an explicitly-named fetch may refuse: slang, minus everything listed.

    Computed rather than stored so it cannot drift from the slang list it is
    derived from. A second hand-maintained copy of a list is the defect class
    this codebase has recorded most often.
    """
    try:
        from app.processors.ticker_extractor import STATIC_FALSE_TICKERS
    except ImportError:
        # Fail OPEN. The guard's only job is to turn a known-dead symbol's
        # empty result into a skip; losing it costs a misclassified event,
        # while failing closed would refuse every fetch in the system.
        return frozenset()

    return frozenset(STATIC_FALSE_TICKERS) - LISTED_ON_THE_SLANG_LIST


def is_blocked_for_explicit_fetch(ticker: str) -> bool:
    """True if a named fetch for `ticker` should be refused rather than attempted."""
    return (ticker or "").upper().strip() in explicit_fetch_blocklist()
