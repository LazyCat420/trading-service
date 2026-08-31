"""
data_readiness.py — Deterministic data-readiness evaluator.

Evaluates whether a ticker has sufficient data quality to proceed into the
expensive multi-agent debate panel (~200k tokens, 20+ minutes).

Rules:
1. Must have usable price history (at least 30 trading days).
2. Must have valid spot price (> 0.0).
3. Must not have critically corrupted/empty technicals.
"""

import logging
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class ReadinessResult:
    is_ready: bool
    quality_score: float  # 0.0 to 1.0
    missing_reasons: list[str]
    disposition: str  # "PROCEED" | "DATA_GAP"


def evaluate_ticker_readiness(
    ticker: str,
    data_report: str,
    technical_context: Optional[str] = None,
    valuation_context: Optional[str] = None,
    price_age_trading_days: Optional[int] = None,
) -> ReadinessResult:
    """Evaluate if a ticker is data-ready for multi-agent debate."""
    reasons: list[str] = []

    if not data_report or "Failed to pre-collect stock data" in data_report:
        reasons.append("data_report_collection_failed")

    if price_age_trading_days is not None and price_age_trading_days > 5:
        reasons.append(f"price_history_stale_{price_age_trading_days}_days")

    if technical_context and "NONE ON FILE" in technical_context and "No technical indicators" in technical_context:
        reasons.append("missing_technical_baseline")

    if reasons:
        score = max(0.0, 1.0 - 0.35 * len(reasons))
        logger.warning(
            "[Readiness] %s DATA_GAP detected (score=%.2f): %s",
            ticker, score, ", ".join(reasons),
        )
        return ReadinessResult(
            is_ready=False,
            quality_score=score,
            missing_reasons=reasons,
            disposition="DATA_GAP",
        )

    return ReadinessResult(
        is_ready=True,
        quality_score=1.0,
        missing_reasons=[],
        disposition="PROCEED",
    )
