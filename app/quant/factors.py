"""
Price-derived cross-sectional factors.

## Why only four factors

The plan this came from specified seven Fama-French-style factors: market
beta, size, value, momentum, profitability, investment, low-vol. Four of
those need a **point-in-time fundamentals panel** — book equity, gross
profit, asset growth and market cap *as they were known on each historical
date*. Measured 2026-07-25, `fundamentals` holds:

    4,782 rows | 737 tickers | 76 distinct snapshot_date | earliest 2026-05-06

That is a current-snapshot table being appended to daily, not a panel. Ranking
2019 stocks on book-to-market values first recorded in 2026 is textbook
look-ahead bias — it manufactures a beautiful backtest that cannot be traded.
So value / profitability / investment / size are **deliberately absent** until
the snapshot history is deep enough (earliest honest start ~2028; the schema's
`PRIMARY KEY (ticker, snapshot_date)` means history accrues for free from here).

`price_history` by contrast holds 15.1M rows across 2,744 tickers back to 1962,
with 2,072 tickers carrying >=10y. Everything below is computed from that alone:

  momentum   12-1  — 12-month return skipping the most recent month
  low_vol          — trailing realized volatility (sign-flipped: low vol = high score)
  beta             — OLS slope of excess returns vs the market proxy
  reversal         — short-horizon mean reversion (sign-flipped 1-month return)

Each factor returns a *cross-sectional z-score*, so values are comparable
across factors and directly rankable. A factor for a single ticker in
isolation is meaningless — the cross-section IS the signal.

## The skip-month is load-bearing

Momentum is 12-1, never 12-0. The most recent month carries short-term
reversal, which is the *opposite* sign to momentum; including it reliably
destroys the effect. `MOMENTUM_SKIP_SESSIONS` is the gap.
"""

from __future__ import annotations

import logging
from datetime import date, timedelta

import numpy as np
import pandas as pd

from app.db.connection import get_db

logger = logging.getLogger(__name__)

TRADING_DAYS_YEAR = 252
TRADING_DAYS_MONTH = 21

# Momentum skips the most recent month (short-term reversal has the opposite
# sign to momentum and cancels it out when included).
MOMENTUM_SKIP_SESSIONS = TRADING_DAYS_MONTH
MOMENTUM_LOOKBACK_SESSIONS = TRADING_DAYS_YEAR

LOW_VOL_SESSIONS = 60
BETA_SESSIONS = TRADING_DAYS_YEAR
REVERSAL_SESSIONS = TRADING_DAYS_MONTH

# Market proxy for beta. SPY has the deepest, cleanest history in our table.
MARKET_PROXY = "SPY"

# Below this many usable observations a per-ticker factor is noise, not a value.
MIN_OBSERVATIONS = 40

# Reversal is the one factor whose own window (21 sessions) is shorter than
# MIN_OBSERVATIONS, so it gets its own floor: ~80% of its window. Applying
# MIN_OBSERVATIONS would reject every valid observation, but the old
# `min(REVERSAL_SESSIONS, MIN_OBSERVATIONS) // 2` evaluated to 10 — half a
# window, ranked against names carrying a full year.
REVERSAL_MIN_SESSIONS = int(REVERSAL_SESSIONS * 0.8)

# A cross-section thinner than this cannot produce a meaningful z-score.
MIN_CROSS_SECTION = 5

# Winsorize z-scores to keep a single broken price series from dominating.
Z_CLIP = 3.0

FACTOR_NAMES = ("momentum", "low_vol", "beta", "reversal")


def load_price_panel(
    tickers: list[str],
    lookback_sessions: int = TRADING_DAYS_YEAR + TRADING_DAYS_MONTH + 10,
    as_of: date | None = None,
) -> pd.DataFrame:
    """Wide close-price panel [date x ticker] from price_history.

    `as_of` caps the window at a historical date so backtests cannot see the
    future; it defaults to today. The calendar pad (1.6x) converts the
    requested *trading* sessions into calendar days so weekends and holidays
    don't silently truncate the window.
    """
    tickers = sorted({t.strip().upper() for t in tickers if t and t.strip()})
    if not tickers:
        return pd.DataFrame()

    end = as_of or date.today()
    start = end - timedelta(days=int(lookback_sessions * 1.6))
    placeholders = ",".join(["%s"] * len(tickers))

    with get_db() as db:
        rows = db.execute(
            f"""
            SELECT ticker, date, close FROM price_history
            WHERE ticker IN ({placeholders})
              AND date >= %s AND date <= %s
              AND close IS NOT NULL AND close > 0
            ORDER BY date ASC
            """,
            [*tickers, start, end],
        ).fetchall()

    if not rows:
        return pd.DataFrame()

    df = pd.DataFrame(rows, columns=["ticker", "date", "close"])
    df["close"] = pd.to_numeric(df["close"], errors="coerce")
    panel = df.pivot_table(index="date", columns="ticker", values="close", aggfunc="last")
    panel = panel.sort_index()
    # A NaN mid-series is a missing print, not a halt — forward-fill a short
    # gap. A long gap stays NaN so the coverage check can drop the column.
    return panel.ffill(limit=5)


def _z_score(raw: dict[str, float]) -> dict[str, float]:
    """Cross-sectional z-score, winsorized to +/-Z_CLIP.

    Uses a population std (ddof=0): this is the full cross-section we care
    about, not a sample from a larger one. A degenerate cross-section (every
    value identical) yields NO factor — see below.
    """
    clean = {k: float(v) for k, v in raw.items()
             if v is not None and np.isfinite(v)}
    if len(clean) < MIN_CROSS_SECTION:
        return {}
    vals = np.array(list(clean.values()), dtype=float)
    mu = float(vals.mean())
    sd = float(vals.std(ddof=0))
    if sd <= 0:
        # No dispersion means there is no ranking to express, so there is no
        # factor. This used to return all-zeros, which is the ONE thing this
        # module's docstring forbids: a zero z-score reads as "perfectly
        # average" — a fabricated measurement rather than an absent one, and
        # the context block renders it to the board as real (2026-07-25 audit).
        logger.debug(
            "[Factors] degenerate cross-section (n=%d, sd=0) — emitting no "
            "factor rather than a zero-filled one", len(clean),
        )
        return {}
    return {
        k: float(np.clip((v - mu) / sd, -Z_CLIP, Z_CLIP))
        for k, v in clean.items()
    }


def momentum_12_1(panel: pd.DataFrame) -> dict[str, float]:
    """12-month return skipping the most recent month, per ticker (raw)."""
    out: dict[str, float] = {}
    need = MOMENTUM_LOOKBACK_SESSIONS + MOMENTUM_SKIP_SESSIONS
    for ticker in panel.columns:
        series = panel[ticker].dropna()
        if len(series) < min(need, MIN_OBSERVATIONS + MOMENTUM_SKIP_SESSIONS):
            continue
        # Skip the most recent month, then look back 12 months from there.
        end_idx = len(series) - MOMENTUM_SKIP_SESSIONS
        start_idx = max(0, end_idx - MOMENTUM_LOOKBACK_SESSIONS)
        if end_idx - start_idx < MIN_OBSERVATIONS:
            continue
        start_px = float(series.iloc[start_idx])
        end_px = float(series.iloc[end_idx - 1])
        if start_px <= 0:
            continue
        out[ticker] = (end_px - start_px) / start_px * 100.0
    return out


def low_volatility(panel: pd.DataFrame) -> dict[str, float]:
    """Trailing realized vol, SIGN-FLIPPED so high score = low vol.

    The low-vol anomaly says low-volatility names outperform on a
    risk-adjusted basis, so the *factor* must point at low vol. Returning
    raw vol here would invert every downstream ranking.
    """
    out: dict[str, float] = {}
    for ticker in panel.columns:
        series = panel[ticker].dropna().tail(LOW_VOL_SESSIONS + 1)
        if len(series) < MIN_OBSERVATIONS:
            continue
        rets = np.diff(np.log(series.values.astype(float)))
        if rets.size < MIN_OBSERVATIONS - 1:
            continue
        vol = float(np.std(rets, ddof=1)) * np.sqrt(TRADING_DAYS_YEAR) * 100.0
        if not np.isfinite(vol):
            continue
        out[ticker] = -vol
    return out


def market_beta(panel: pd.DataFrame, market: pd.Series | None = None) -> dict[str, float]:
    """OLS slope of each ticker's daily returns against the market proxy.

    Returned raw (not sign-flipped): downstream code decides whether high
    beta is desirable. Requires the market series to overlap the ticker.
    """
    if market is None or market.empty:
        return {}
    mkt = market.dropna()
    mkt_rets = pd.Series(
        np.diff(np.log(mkt.values.astype(float))), index=mkt.index[1:]
    )

    out: dict[str, float] = {}
    for ticker in panel.columns:
        series = panel[ticker].dropna().tail(BETA_SESSIONS + 1)
        if len(series) < MIN_OBSERVATIONS:
            continue
        rets = pd.Series(
            np.diff(np.log(series.values.astype(float))), index=series.index[1:]
        )
        joined = pd.concat([rets, mkt_rets], axis=1, join="inner").dropna()
        if len(joined) < MIN_OBSERVATIONS:
            continue
        y = joined.iloc[:, 0].values
        x = joined.iloc[:, 1].values
        var_x = float(np.var(x, ddof=1))
        if var_x <= 0:
            continue
        beta = float(np.cov(y, x, ddof=1)[0, 1] / var_x)
        if np.isfinite(beta):
            out[ticker] = beta
    return out


def short_term_reversal(panel: pd.DataFrame) -> dict[str, float]:
    """1-month return, SIGN-FLIPPED — recent losers are expected to bounce."""
    out: dict[str, float] = {}
    for ticker in panel.columns:
        series = panel[ticker].dropna().tail(REVERSAL_SESSIONS + 1)
        # Most of its OWN window, not MIN_OBSERVATIONS. Reversal is a 1-month
        # measure, so the 40-session floor the other factors use would reject
        # every valid observation; but the previous floor evaluated to 10
        # (`min(21, 40) // 2`), which let an 11-row ticker be z-scored against
        # names with 250 rows in the same cross-section (2026-07-25 audit).
        if len(series) < REVERSAL_MIN_SESSIONS:
            continue
        start_px = float(series.iloc[0])
        end_px = float(series.iloc[-1])
        if start_px <= 0:
            continue
        out[ticker] = -((end_px - start_px) / start_px * 100.0)
    return out


def compute_factors(
    tickers: list[str],
    as_of: date | None = None,
    include_market: bool = True,
) -> dict[str, dict[str, float]]:
    """Cross-sectional z-scores for every factor.

    Returns {factor_name: {ticker: z_score}}. A factor whose cross-section is
    too thin is omitted entirely rather than returned with fabricated values —
    a missing factor is honest, a zero-filled one is not.
    """
    universe = sorted({t.strip().upper() for t in tickers if t and t.strip()})
    if not universe:
        return {}

    load_list = universe + ([MARKET_PROXY] if include_market else [])
    panel = load_price_panel(load_list, as_of=as_of)
    if panel.empty:
        logger.warning("[Factors] empty price panel for %d tickers", len(universe))
        return {}

    market = panel[MARKET_PROXY] if MARKET_PROXY in panel.columns else None
    # The proxy is an input to beta, not a member of the cross-section —
    # leaving it in would let SPY be ranked against the stocks it explains.
    ticker_panel = panel[[c for c in panel.columns
                          if c in universe and c != MARKET_PROXY]]
    if ticker_panel.empty:
        return {}

    raw = {
        "momentum": momentum_12_1(ticker_panel),
        "low_vol": low_volatility(ticker_panel),
        "beta": market_beta(ticker_panel, market),
        "reversal": short_term_reversal(ticker_panel),
    }

    out: dict[str, dict[str, float]] = {}
    for name, values in raw.items():
        z = _z_score(values)
        if z:
            out[name] = z
        else:
            logger.debug("[Factors] %s: cross-section too thin (n=%d)", name, len(values))
    return out


def factor_exposures_for(
    ticker: str,
    universe: list[str],
    as_of: date | None = None,
) -> dict[str, float]:
    """This ticker's z-score on each factor, within `universe`.

    Convenience wrapper for the context block. Empty dict when the ticker
    fell out of every cross-section.
    """
    ticker = ticker.strip().upper()
    factors = compute_factors(universe, as_of=as_of)
    return {
        name: scores[ticker]
        for name, scores in factors.items()
        if ticker in scores
    }
