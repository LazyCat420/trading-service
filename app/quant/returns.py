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

from app.db.connection import get_db

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
# from ONE vendor for the whole window. Preference is by row count in the
# window (yfinance holds 15.14M of 15.15M rows, so it wins in practice), ties
# broken by source name so the choice is deterministic across processes.
# Single-source tickers are unaffected: AAPL reads 24.73% either way.


def _dominant_source_sql(alias: str = "price_history") -> str:
    """SQL scalar subquery naming the vendor with the most rows for a ticker.

    Bound parameter is `%(ticker)s`, so callers must use named parameters.
    """
    return f"""
        SELECT source FROM {alias}
        WHERE ticker = %(ticker)s
        GROUP BY source
        ORDER BY count(*) DESC, source
        LIMIT 1
    """


def _keep_dominant_source(df: pd.DataFrame) -> pd.DataFrame:
    """Drop every row whose vendor is not the ticker's dominant vendor.

    Per-ticker rather than global: two tickers may legitimately have different
    dominant vendors, and mixing conventions WITHIN a column is the bug.
    """
    if "source" not in df.columns or df["source"].nunique() <= 1:
        return df

    winner = (
        df.groupby(["ticker", "source"], sort=True)
        .size()
        .reset_index(name="_n")
        .sort_values(["ticker", "_n", "source"], ascending=[True, False, True])
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


def load_returns_matrix(
    tickers: list[str],
    lookback_days: int = 252,
) -> tuple[pd.DataFrame, list[str]]:
    """Aligned daily log-returns for `tickers` from price_history.

    Returns (returns_df [date x ticker], dropped) where dropped lists tickers
    excluded for having under 60% coverage of the window — a thin column
    would poison every pairwise estimate in the covariance matrix.
    """
    tickers = sorted({t.strip().upper() for t in tickers if t and t.strip()})
    if not tickers:
        return pd.DataFrame(), []

    cutoff = date.today() - timedelta(days=int(lookback_days * _CALENDAR_PAD))
    placeholders = ",".join(["%s"] * len(tickers))
    with get_db() as db:
        rows = db.execute(
            f"""
            SELECT ticker, date, close, source FROM price_history
            WHERE ticker IN ({placeholders}) AND date >= %s
            ORDER BY date ASC
            """,
            [*tickers, cutoff],
        ).fetchall()

    if not rows:
        return pd.DataFrame(), list(tickers)

    df = pd.DataFrame(rows, columns=["ticker", "date", "close", "source"])
    df["close"] = df["close"].astype(float)
    # Before this, `aggfunc="last"` collapsed duplicate ticker-dates to one
    # value but picked the vendor by row order — undefined, since the query
    # only sorts by date. A column could therefore switch adjustment
    # convention between dates. Pin the vendor first; the pivot's collapse is
    # now a defensive no-op.
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
    """Daily log-return series for one ticker (for GARCH fitting).

    One vendor, one row per date. The vendor filter has to sit INSIDE the
    subquery: `LIMIT` is applied before any de-duplication, so on a
    dual-source ticker the old form returned `lookback_days` ROWS spanning only
    half as many dates (CRH: 253 rows over ~127 dates). See the module header
    for the measured effect on volatility.
    """
    ticker = ticker.strip().upper()
    with get_db() as db:
        rows = db.execute(
            f"""
            SELECT close FROM (
                SELECT date, close FROM price_history
                WHERE ticker = %(ticker)s
                  AND source = ({_dominant_source_sql()})
                ORDER BY date DESC LIMIT %(limit)s
            ) recent ORDER BY date ASC
            """,
            {"ticker": ticker, "limit": int(lookback_days) + 1},
        ).fetchall()
    closes = np.array([float(r[0]) for r in rows if r[0] is not None], dtype=float)
    closes = closes[closes > 0]
    if closes.size < 2:
        return np.array([])
    return np.diff(np.log(closes))


def latest_close(ticker: str) -> float | None:
    """Most recent close for `ticker`, from ONE vendor.

    `ORDER BY date DESC LIMIT 1` without a source filter is non-deterministic on
    a dual-source ticker: both vendors carry the same max date, and whichever
    row the planner emits first wins. The vendors disagree by a mean 20.05%
    (ALLY 1.11%, CRH ~1%, DRIP 718%) — see the module header — so an entry price
    read one way and an exit price read the other turns a vendor spread into
    P&L. 19% of completed desks sit on such a ticker.
    """
    ticker = ticker.strip().upper()
    with get_db() as db:
        row = db.execute(
            f"""
            SELECT close FROM price_history
            WHERE ticker = %(ticker)s AND close IS NOT NULL AND close > 0
              AND source = ({_dominant_source_sql()})
            ORDER BY date DESC LIMIT 1
            """,
            {"ticker": ticker},
        ).fetchone()
    if not row or row[0] is None:
        return None
    val = float(row[0])
    return val if val == val and val > 0 else None


def forward_window(ticker: str, start, sessions: int) -> list[float] | None:
    """`sessions` consecutive closes from the first bar on/after `start`.

    Returns None unless the FULL window exists — a short window silently scored
    as a full one is how a "+7 session" move becomes a 3-session move on a
    dual-source ticker, where `LIMIT 8` returns 8 ROWS spanning ~4 dates.
    Measured on CRH: the unfiltered read gives +0.970% where the truth is
    -2.358%, a sign flip, on 19% of scored desks.

    One bar per date, one vendor for the whole window — mixing conventions
    mid-window manufactures a jump (DRIP: 133 daily moves over 15%).
    """
    ticker = ticker.strip().upper()
    n = int(sessions)
    if n < 2:
        return None
    with get_db() as db:
        rows = db.execute(
            f"""
            SELECT close FROM price_history
            WHERE ticker = %(ticker)s AND close IS NOT NULL AND close > 0
              AND date >= %(start)s
              AND source = ({_dominant_source_sql()})
            ORDER BY date ASC LIMIT %(n)s
            """,
            {"ticker": ticker, "start": start, "n": n},
        ).fetchall()
    closes = [float(r[0]) for r in rows if r[0] is not None]
    closes = [c for c in closes if c == c and c > 0]
    if len(closes) < n:
        return None  # window has not closed yet
    return closes


def forward_move_pct(ticker: str, start, sessions: int) -> float | None:
    """Percent move over an exact `sessions`-bar forward window, or None."""
    w = forward_window(ticker, start, sessions)
    if not w or w[0] <= 0:
        return None
    return (w[-1] - w[0]) / w[0] * 100.0
