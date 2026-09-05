"""
Returns-matrix loading for portfolio math.

Reads the Postgres price_history table directly (2,700+ tickers of daily
closes already in the DB) instead of fanning out per-ticker Polygon calls.
"""

from __future__ import annotations

import logging
from datetime import date, timedelta

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

# Calendar-day multiplier so `lookback_days` trading rows survive weekends,
# holidays, and ragged listings.
_CALENDAR_PAD = 1.6
MIN_COVERAGE = 0.6
MAX_FFILL_GAP = 5

# `source` is part of the price_history primary key, so one ticker-date can
# carry several vendor prints. Measured 2026-07-29: 9,225 dual-source
# ticker-dates across 38 tickers, and the vendors do NOT agree — mean absolute
# close difference 20.05%, with 2,959 of 9,225 pairs over 50bps. The spread is
# an adjustment-convention difference (yfinance returns dividend/split-adjusted
# closes, polygon raw), so it is systematic, not noise: DRIP 718%, AGNC 6.69%,
# CVX 1.71%, ALLY 1.11% mean absolute difference.
#
# That makes vendor mixing a correctness bug in two directions at once:
#   * pairing two prints of the SAME date injects a near-zero return and
#     dilutes variance — CRH 253-bar annualized vol read 25.18% vs 32.44%
#     (understated 23%), ALLY 23.92% vs 29.85%
#   * alternating between conventions across dates manufactures jumps — DRIP
#     read 2,660.95% annualized vol with 133 daily moves over 15%, against
#     232.39% and 1 jump once a single vendor is pinned
#
# So collapsing to one row per date is NOT sufficient; the series must come
# from ONE vendor for the whole window. Preference is FRESHNESS first, then row
# count: a vendor only competes if its newest bar is within
# _FRESHNESS_LAG_DAYS of the best vendor's newest bar, and among those the
# deepest history wins (yfinance holds 15.14M of 15.15M rows, so it wins
# whenever it is current), ties broken by source name so the choice is
# deterministic across processes. Single-source tickers are unaffected: AAPL
# reads 24.73% either way.
#
# Freshness outranks depth because depth alone picked a dead series in
# cycle-v3-1785504601: yfinance stopped writing RBLX/EC on 2026-07-17 (a bad
# vendor bar re-failed validation every day) while polygon carried bars through
# 07-30 — and the row-count rule kept choosing yfinance, so the desk analysed
# RBLX 24% off its real price with polygon's fresh series sitting in the same
# table. When both vendors are current the freshness test ties and the choice
# is identical to the old rule, so scoring paths only change behavior in
# exactly the failure mode.

# Calendar days a vendor's newest bar may lag the best vendor's newest bar and
# still be pinned. 2 covers overnight publishing skew between vendors (they
# lag EACH OTHER, not today, so weekends don't need padding) without letting a
# stale series through; the stale-price guardrail fires at 4+ trading days.
_FRESHNESS_LAG_DAYS = 2


def _dominant_source_sql(alias: str = "price_history") -> str:
    """SQL scalar subquery naming the freshest-then-deepest vendor for a ticker.

    Bound parameter is `%(ticker)s`, so callers must use named parameters.
    """
    return f"""
        SELECT source FROM {alias}
        WHERE ticker = %(ticker)s
        GROUP BY source
        ORDER BY
            (max(date) >= (
                SELECT max(date) - {_FRESHNESS_LAG_DAYS} FROM {alias}
                WHERE ticker = %(ticker)s
            )) DESC,
            count(*) DESC,
            source
        LIMIT 1
    """


def dominant_source_sql(alias: str = "price_history") -> str:
    """Public name for `_dominant_source_sql` — see that function.

    The one-vendor rule is not a property of the evaluation layer; it is a
    property of `price_history` itself, whose primary key is
    `(ticker, date, source)`. Any module reading that table needs this filter,
    so the helper is exported rather than reimplemented. Reimplementing it is
    how `outcome_tracker` and `challenger` ended up with the same bug twice.

    Callers must use NAMED parameters (`%(ticker)s`) and must place the filter
    INSIDE any subquery that carries a `LIMIT`, or the limit is applied before
    de-duplication and the window silently spans half as many dates.
    """
    return _dominant_source_sql(alias)


def _keep_dominant_source(df: pd.DataFrame) -> pd.DataFrame:
    """Drop every row whose vendor is not the ticker's dominant vendor.

    Per-ticker rather than global: two tickers may legitimately have different
    dominant vendors, and mixing conventions WITHIN a column is the bug.
    Mirrors `_dominant_source_sql`: freshness first (when the frame carries a
    `date` column), then row count, then source name.
    """
    if "source" not in df.columns or df["source"].nunique() <= 1:
        return df

    grouped = df.groupby(["ticker", "source"], sort=True)
    if "date" in df.columns:
        stats = grouped.agg(_n=("source", "size"), _mx=("date", "max")).reset_index()
        stats["_mx"] = pd.to_datetime(stats["_mx"])  # date objects → comparable
        best = stats.groupby("ticker")["_mx"].transform("max")
        stats["_fresh"] = stats["_mx"] >= best - pd.Timedelta(days=_FRESHNESS_LAG_DAYS)
    else:
        stats = grouped.size().reset_index(name="_n")
        stats["_fresh"] = True
    winner = (
        stats.sort_values(
            ["ticker", "_fresh", "_n", "source"],
            ascending=[True, False, False, True],
        )
        .groupby("ticker", sort=True)
        .head(1)
        .loc[:, ["ticker", "source"]]
        .rename(columns={"source": "_keep_source"})
    )
    merged = df.merge(winner, on="ticker", how="left")
    kept = merged[merged["source"] == merged["_keep_source"]]
    dropped = len(df) - len(kept)
    if dropped:
        logger.debug(
            "[returns] dropped %d off-vendor rows to keep one convention per ticker",
            dropped,
        )
    return kept.drop(columns=["_keep_source"])


def keep_dominant_source(df: pd.DataFrame) -> pd.DataFrame:
    """Public name for `_keep_dominant_source` — see that function.

    Use this for MULTI-ticker frames, where the single-ticker
    `dominant_source_sql()` filter does not apply. The frame must carry
    `ticker` and `source` columns; drop `source` after filtering.
    """
    return _keep_dominant_source(df)


def load_returns_matrix(
    tickers: list[str],
    lookback_days: int = 252,
) -> tuple[pd.DataFrame, list[str]]:
    """Aligned daily log-returns for `tickers` from price_history."""
    tickers = sorted({t.strip().upper() for t in tickers if t and t.strip()})
    if not tickers:
        return pd.DataFrame(), []

    from app.db import mongo_store

    cutoff = date.today() - timedelta(days=int(lookback_days * _CALENDAR_PAD))
    query = {"ticker": {"$in": tickers}, "date": {"$gte": cutoff}}
    docs = mongo_store.find_docs("price_history", query, sort=[("date", 1)])

    if not docs:
        return pd.DataFrame(), list(tickers)

    df = pd.DataFrame(docs)
    df["close"] = df["close"].astype(float)
    df = _keep_dominant_source(df)
    prices = (
        df.pivot_table(index="date", columns="ticker", values="close", aggfunc="last")
        .sort_index()
        .tail(lookback_days + 1)
    )

    coverage = prices.notna().mean()
    dropped = sorted(c for c in prices.columns if coverage[c] < MIN_COVERAGE)
    kept = [c for c in prices.columns if c not in dropped]
    dropped += sorted(set(tickers) - set(prices.columns))
    if not kept:
        return pd.DataFrame(), dropped

    prices = prices[kept].ffill(limit=MAX_FFILL_GAP)
    returns = np.log(prices).diff().dropna(how="all")
    return returns, dropped


def load_close_returns(ticker: str, lookback_days: int = 500) -> np.ndarray:
    """Daily log-return series for one ticker (for GARCH fitting)."""
    from app.db import mongo_store

    ticker = ticker.strip().upper()
    docs = mongo_store.find_docs(
        "price_history",
        {"ticker": ticker},
        sort=[("date", -1)],
        limit=int(lookback_days) + 1,
    )
    if not docs:
        return np.array([])
    docs = list(reversed(docs))
    closes = np.array([float(d.get("close")) for d in docs if d.get("close") is not None], dtype=float)
    closes = closes[closes > 0]
    if closes.size < 2:
        return np.array([])
    return np.diff(np.log(closes))


def dominant_source_for(ticker: str) -> str | None:
    """The freshest-then-deepest vendor for one ticker, from MongoDB.

    The Mongo counterpart of `_dominant_source_sql`, and the reason it exists:
    the SQL version was added on 2026-07-30 after measuring a mean 20.05%
    disagreement between vendors on dual-source tickers (ALLY 1.11%, CRH ~1%,
    DRIP 718%), on 19% of completed desks. `price_history`'s primary key is
    `(ticker, date, source)`, so BOTH vendors carry a row for the same date and
    an unfiltered `sort=[("date", -1)], limit=1` returns whichever the storage
    engine happens to emit first. Read an entry price one way and an exit price
    the other and a vendor spread becomes P&L.

    The Mongo port of `latest_close`/`forward_window` dropped that filter, so
    the 2026-07-30 fix was live in SQL and absent the moment those functions
    moved. This restores it with the identical rule: prefer a vendor whose most
    recent bar is within `_FRESHNESS_LAG_DAYS` of the ticker's newest bar, then
    the vendor with the most rows, then the name, so ties break the same way in
    both stores.

    Returns None when the ticker has no rows or only one vendor, in which case
    the caller needs no filter.
    """
    from app.db import mongo_store

    stats = mongo_store.aggregate("price_history", [
        {"$match": {"ticker": ticker}},
        {"$group": {"_id": "$source", "n": {"$sum": 1}, "mx": {"$max": "$date"}}},
    ])
    if len(stats) <= 1:
        return None

    newest = max(r["mx"] for r in stats if r.get("mx") is not None)
    cutoff = newest - timedelta(days=_FRESHNESS_LAG_DAYS)

    def _rank(r: dict):
        mx = r.get("mx")
        fresh = bool(mx is not None and mx >= cutoff)
        # freshness DESC, count DESC, source name ASC — same order as the SQL
        return (not fresh, -int(r.get("n") or 0), str(r["_id"] or ""))

    return sorted(stats, key=_rank)[0]["_id"]


def _one_vendor(ticker: str, query: dict) -> dict:
    """Add the dominant-source pin to a single-ticker price_history filter."""
    src = dominant_source_for(ticker)
    return {**query, "source": src} if src is not None else query


def latest_close(ticker: str) -> float | None:
    """Most recent close for `ticker`."""
    from app.db import mongo_store

    ticker = ticker.strip().upper()
    docs = mongo_store.find_docs(
        "price_history",
        _one_vendor(ticker, {"ticker": ticker, "close": {"$gt": 0}}),
        sort=[("date", -1)],
        limit=1,
    )
    if not docs or docs[0].get("close") is None:
        return None
    val = float(docs[0]["close"])
    return val if val == val and val > 0 else None


def forward_window(ticker: str, start, sessions: int) -> list[float] | None:
    """`sessions` consecutive closes from the first bar on/after `start`."""
    from app.db import mongo_store

    ticker = ticker.strip().upper()
    n = int(sessions)
    if n < 2:
        return None

    # Without the vendor pin, `limit=n` returns n ROWS spanning only ~n/2
    # DATES on a dual-source ticker, so the "window" silently covers half the
    # sessions it claims.
    docs = mongo_store.find_docs(
        "price_history",
        _one_vendor(ticker, {"ticker": ticker, "close": {"$gt": 0},
                             "date": {"$gte": start}}),
        sort=[("date", 1)],
        limit=n,
    )
    closes = [float(d.get("close")) for d in docs if d.get("close") is not None]
    closes = [c for c in closes if c == c and c > 0]
    if len(closes) < n:
        return None  # window has not closed yet
    return closes


#: How far past the horizon date a bar may sit and still be accepted as "the
#: close at the horizon". Covers a weekend plus a long holiday weekend; beyond
#: that the market data is missing rather than merely non-trading, and the
#: honest answer is to leave the row unresolved.
HORIZON_GRACE_DAYS = 5


def close_on_or_after(ticker: str, when, grace_days: int = HORIZON_GRACE_DAYS
                      ) -> tuple[float | None, "datetime | None"]:
    """The first close at or after `when`. Returns (close, its date).

    THE CONTRACT THIS EXISTS FOR. `decision_outcomes` rows are stamped with a
    7-day horizon and every card in the panel says "7-day". Resolution used
    `latest_close`, i.e. whatever the price happens to be on the day the
    resolver gets around to the row — so the horizon was "whenever the sweep
    ran", not "entry + 7 days".

    MEASURED 2026-09-05 over 2,694 resolved rows, `resolved_at - created_at`:

        >30 days      1,932   71.7%     <- the actual population
        7.0-7.9 days    699   25.9%     <- the stated contract
        <7 days          37    1.4%
        8-30 days        26    1.0%

        median 43.0 days against a stated 7.

    Three quarters of every "7-day outcome" was a six-week outcome wearing a
    one-week label, and every win rate and decision score built on that cohort
    inherited the mismatch.

    Weekends and holidays are why this is "on or after" rather than an exact
    date match: the horizon frequently lands on a day with no bar. `grace_days`
    bounds how far it will walk forward, so a genuinely missing stretch of
    market data returns (None, None) and the row stays unresolved instead of
    silently resolving against a price weeks later — which is the defect.

    Uses the same `_one_vendor` pin as `latest_close` and `forward_window`, so
    a dual-source ticker cannot resolve against one vendor here and another
    somewhere else.
    """
    from datetime import timedelta as _td

    from app.db import mongo_store

    ticker = ticker.strip().upper()
    docs = mongo_store.find_docs(
        "price_history",
        _one_vendor(ticker, {
            "ticker": ticker,
            "close": {"$gt": 0},
            "date": {"$gte": when, "$lte": when + _td(days=int(grace_days))},
        }),
        sort=[("date", 1)],
        limit=1,
    )
    if not docs:
        return None, None
    val = docs[0].get("close")
    if val is None:
        return None, None
    val = float(val)
    if not (val == val and val > 0):
        return None, None
    return val, docs[0].get("date")


def forward_move_pct(ticker: str, start, sessions: int) -> float | None:
    """Percent move over an exact `sessions`-bar forward window, or None."""
    w = forward_window(ticker, start, sessions)
    if not w or w[0] <= 0:
        return None
    return (w[-1] - w[0]) / w[0] * 100.0
