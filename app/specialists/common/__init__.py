"""Common contracts, normalization, schemas, and metrics for specialist models."""

from app.specialists.common.schemas import (
    SpecialistTask,
    MarketRegime,
    ALLOWED_LABELS,
    GlinerEntity,
    CnnRegimeOutput,
    RnnForecastOutput,
)
from app.specialists.common.normalization import normalize_ohlcv_features, validate_ohlcv_sequence
from app.specialists.common.metrics import (
    compute_brier_score,
    compute_quantile_coverage,
    validate_finite_metrics,
)

__all__ = [
    "SpecialistTask",
    "MarketRegime",
    "ALLOWED_LABELS",
    "GlinerEntity",
    "CnnRegimeOutput",
    "RnnForecastOutput",
    "normalize_ohlcv_features",
    "validate_ohlcv_sequence",
    "compute_brier_score",
    "compute_quantile_coverage",
    "validate_finite_metrics",
]
