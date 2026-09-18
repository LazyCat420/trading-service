"""Shared schemas and data models for specialist neural intelligence."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class SpecialistTask(str, Enum):
    GLINER = "gliner"
    MARKET_CNN = "market_cnn"
    TIMESERIES_RNN = "timeseries_rnn"


class MarketRegime(str, Enum):
    BULL_TREND = "BULL_TREND"
    BEAR_TREND = "BEAR_TREND"
    HIGH_VOL_CHOP = "HIGH_VOL_CHOP"
    LOW_VOL_CONSOLIDATION = "LOW_VOL_CONSOLIDATION"
    UNKNOWN = "UNKNOWN"


ALLOWED_LABELS = frozenset({
    "ticker",
    "financial_metric_value",
    "corporate_action",
    "event_trigger",
    "guidance",
})


@dataclass
class GlinerEntity:
    text: str
    label: str
    start_char: int
    end_char: int
    ticker: str | None = None
    confidence: float = 1.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "label": self.label,
            "start_char": self.start_char,
            "end_char": self.end_char,
            "ticker": self.ticker,
            "confidence": self.confidence,
        }


@dataclass
class CnnRegimeOutput:
    predicted_regime: str
    probabilities: dict[str, float] = field(default_factory=dict)
    brier_score: float | None = None
    model_version: str = "unknown"

    def to_dict(self) -> dict[str, Any]:
        return {
            "predicted_regime": self.predicted_regime,
            "probabilities": self.probabilities,
            "brier_score": self.brier_score,
            "model_version": self.model_version,
        }


@dataclass
class RnnForecastOutput:
    horizon_days: int
    quantiles: dict[str, float] = field(default_factory=dict)
    stop_loss_ref: float | None = None
    model_version: str = "unknown"

    def to_dict(self) -> dict[str, Any]:
        return {
            "horizon_days": self.horizon_days,
            "quantiles": self.quantiles,
            "stop_loss_ref": self.stop_loss_ref,
            "model_version": self.model_version,
        }
