"""
Shared normalization and validation for market sequences (OHLCV & features).
Enforces train-only fitting, non-zero positive prices, and NaN/inf rejection.
"""

from __future__ import annotations

import math
from typing import Sequence


def validate_ohlcv_sequence(
    ohlcv: Sequence[Sequence[float]],
    min_bars: int = 30,
) -> None:
    """Validate raw OHLCV sequence adheres to strict price integrity contracts.

    Raises:
        ValueError: If sequence is incomplete (< min_bars) or contains NaN, inf, or <= 0 price.
    """
    if not ohlcv or len(ohlcv) < min_bars:
        raise ValueError(
            f"Incomplete OHLCV sequence: received {len(ohlcv) if ohlcv else 0} bars, "
            f"minimum required is {min_bars}. Zero-padding is strictly forbidden."
        )

    for idx, bar in enumerate(ohlcv):
        if len(bar) < 4:
            raise ValueError(f"Bar {idx} has fewer than 4 OHLC values: {bar}")
        for col_idx, val in enumerate(bar[:4]):
            if not isinstance(val, (int, float)) or math.isnan(val) or math.isinf(val):
                raise ValueError(f"Bar {idx} col {col_idx} has invalid non-finite value: {val}")
            if val <= 0.0:
                raise ValueError(f"Bar {idx} col {col_idx} has non-positive price: {val}")


def normalize_ohlcv_features(
    sequence: Sequence[Sequence[float]],
    eps: float = 1e-8,
) -> list[list[float]]:
    """Normalize multi-channel feature sequence using window-only mean and standard deviation.

    Fitted strictly on the provided historical sequence to prevent lookahead bias.
    """
    if not sequence:
        return []

    num_cols = len(sequence[0])
    normalized: list[list[float]] = []

    # Calculate column-wise mean and std
    means: list[float] = []
    stds: list[float] = []

    for col in range(num_cols):
        vals = [row[col] for row in sequence]
        m = sum(vals) / len(vals)
        var = sum((x - m) ** 2 for x in vals) / len(vals)
        s = math.sqrt(var)
        means.append(m)
        stds.append(s if s > eps else 1.0)

    for row in sequence:
        norm_row = [(row[col] - means[col]) / stds[col] for col in range(num_cols)]
        normalized.append(norm_row)

    return normalized
