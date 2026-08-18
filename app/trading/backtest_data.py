"""
Backtest Data Provider — prepares fixed OOS backtest windows for the evolution loop.

Pulls OHLCV data from the MongoDB price_history collection and exports
it as a Parquet file for the sandbox executor.
"""

import logging
import os
import tempfile

import pandas as pd

from app.db import mongo_store

logger = logging.getLogger(__name__)


def get_backtest_data(
    tickers: list[str],
    start_date: str,
    end_date: str,
    output_path: str | None = None,
    allow_synthetic: bool = False,
) -> str:
    """Extract OHLCV data for the backtest window and save as Parquet.

    Args:
        tickers: List of tickers to include (e.g. ["SPY", "QQQ"]).
        start_date: Start of OOS window (YYYY-MM-DD).
        end_date: End of OOS window (YYYY-MM-DD).
        output_path: Where to save the Parquet file. If None, uses a temp file.
        allow_synthetic: If True, fall back to synthetic data when no real data
            is found. Should only be True in unit tests.

    Returns:
        Path to the Parquet file.
    """
    start_dt = pd.to_datetime(start_date)
    end_dt = pd.to_datetime(end_date)

    query = {"ticker": {"$in": tickers}}
    docs = mongo_store.find_docs(
        "price_history",
        query,
        projection={"ticker": 1, "date": 1, "open": 1, "high": 1, "low": 1, "close": 1, "volume": 1, "_id": 0},
        sort=[("date", 1)]
    )

    filtered_rows = []
    for d in docs:
        raw_dt = d.get("date")
        if not raw_dt:
            continue
        try:
            dt = pd.to_datetime(raw_dt)
            if dt.tzinfo is not None:
                dt = dt.tz_localize(None)
            if start_dt.tzinfo is not None:
                start_dt = start_dt.tz_localize(None)
            if end_dt.tzinfo is not None:
                end_dt = end_dt.tz_localize(None)
            if start_dt <= dt <= end_dt:
                filtered_rows.append({
                    "ticker": d.get("ticker"),
                    "date": dt,
                    "open": d.get("open"),
                    "high": d.get("high"),
                    "low": d.get("low"),
                    "close": d.get("close"),
                    "volume": d.get("volume"),
                })
        except Exception:
            pass

    if not filtered_rows:
        if not allow_synthetic:
            raise ValueError(
                f"No price data for {tickers} between {start_date}–{end_date}. "
                "Pass allow_synthetic=True only for unit tests."
            )
        logger.warning(
            "SYNTHETIC DATA IN USE — evolution scores will be meaningless"
        )
        return _generate_synthetic_data(tickers, start_date, end_date, output_path)

    df = pd.DataFrame(filtered_rows)
    df = df.set_index("date").sort_index()

    if output_path is None:
        fd, output_path = tempfile.mkstemp(suffix=".parquet", prefix="backtest_")
        os.close(fd)

    df.to_parquet(output_path)
    logger.info("Backtest data exported: %d rows → %s", len(df), output_path)
    return output_path


def _generate_synthetic_data(
    tickers: list[str],
    start_date: str,
    end_date: str,
    output_path: str | None = None,
) -> str:
    """Generate synthetic OHLCV data when no real data is available."""
    import numpy as np

    dates = pd.bdate_range(start=start_date, end=end_date)
    n = len(dates)
    if n == 0:
        dates = pd.bdate_range(start="2023-01-01", end="2024-01-01")
        n = len(dates)

    if not tickers:
        tickers = ["SYNTH"]

    frames = []
    for i, symbol in enumerate(tickers):
        np.random.seed(42 + i)
        close = 100 * np.exp(np.cumsum(np.random.normal(0.0003, 0.015, n)))
        high = close * (1 + np.abs(np.random.normal(0, 0.005, n)))
        low = close * (1 - np.abs(np.random.normal(0, 0.005, n)))
        open_price = low + (high - low) * np.random.uniform(0.2, 0.8, n)
        volume = np.random.randint(1_000_000, 50_000_000, n)

        df = pd.DataFrame(
            {
                "ticker": symbol,
                "open": open_price,
                "high": high,
                "low": low,
                "close": close,
                "volume": volume,
            },
            index=dates,
        )
        df.index.name = "date"
        frames.append(df)

    combined = pd.concat(frames)

    if output_path is None:
        fd, output_path = tempfile.mkstemp(suffix=".parquet", prefix="backtest_synth_")
        os.close(fd)

    combined.to_parquet(output_path)
    return output_path
