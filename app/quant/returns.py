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
            SELECT ticker, date, close FROM price_history
            WHERE ticker IN ({placeholders}) AND date >= %s
            ORDER BY date ASC
            """,
            [*tickers, cutoff],
        ).fetchall()

    if not rows:
        return pd.DataFrame(), list(tickers)

    df = pd.DataFrame(rows, columns=["ticker", "date", "close"])
    df["close"] = df["close"].astype(float)
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
    ticker = ticker.strip().upper()
    with get_db() as db:
        rows = db.execute(
            """
            SELECT close FROM (
                SELECT date, close FROM price_history
                WHERE ticker = %s ORDER BY date DESC LIMIT %s
            ) recent ORDER BY date ASC
            """,
            [ticker, int(lookback_days) + 1],
        ).fetchall()
    closes = np.array([float(r[0]) for r in rows if r[0] is not None], dtype=float)
    closes = closes[closes > 0]
    if closes.size < 2:
        return np.array([])
    return np.diff(np.log(closes))
