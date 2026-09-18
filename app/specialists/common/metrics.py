"""
Shared metric computation and validation across specialists.
Guarantees fail-closed validation of finite metrics, calibration (Brier score),
and forecast quantile coverage.
"""

from __future__ import annotations

import math
from typing import Sequence


def validate_finite_metrics(metrics: dict[str, float]) -> None:
    """Verify all metrics are valid, finite numeric values.

    Raises:
        ValueError: If any metric is NaN, Inf, None, or not a float/int.
    """
    for k, v in metrics.items():
        if v is None:
            raise ValueError(f"Metric '{k}' is None; promotion gates require complete metrics.")
        if not isinstance(v, (int, float)) or isinstance(v, bool):
            raise ValueError(f"Metric '{k}' has non-numeric type {type(v).__name__}.")
        if math.isnan(v):
            raise ValueError(f"Metric '{k}' is NaN.")
        if math.isinf(v):
            raise ValueError(f"Metric '{k}' is infinite.")


def compute_brier_score(
    predicted_probs: Sequence[dict[str, float]],
    actual_classes: Sequence[str],
) -> float:
    """Compute multiclass Brier calibration score: mean(sum((p_c - y_c)^2)).

    Lower is better. Perfect calibration = 0.0.
    """
    if not predicted_probs or len(predicted_probs) != len(actual_classes):
        raise ValueError("Predicted probabilities and actual classes must be non-empty and equal length.")

    total_error = 0.0
    for probs, actual in zip(predicted_probs, actual_classes):
        # All classes mentioned in probs or actual
        classes = set(probs.keys()) | {actual}
        sample_err = 0.0
        for c in classes:
            p = probs.get(c, 0.0)
            y = 1.0 if c == actual else 0.0
            sample_err += (p - y) ** 2
        total_error += sample_err

    return total_error / len(actual_classes)


def compute_quantile_coverage(
    realized_returns: Sequence[float],
    forecast_intervals: Sequence[tuple[float, float]],
) -> float:
    """Compute empirical coverage of prediction intervals (e.g. [p10, p90]).

    Returns fraction of realized returns that fell within [lower, upper].
    """
    if not realized_returns or len(realized_returns) != len(forecast_intervals):
        raise ValueError("Realized returns and intervals must be non-empty and equal length.")

    covered = 0
    for r, (p_low, p_high) in zip(realized_returns, forecast_intervals):
        if p_low <= r <= p_high:
            covered += 1

    return covered / len(realized_returns)
