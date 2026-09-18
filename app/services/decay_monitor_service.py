"""
Decay Monitor and Automated Rollback Service for Specialist Neural Models.

Enforces Item 12:
- Rolling out-of-time evaluation and historical test tracking.
- Critical regression thresholds:
  - CNN Brier score > 0.12 (Calibration decay)
  - RNN 80% coverage < 0.65 (Forecast interval decay)
  - GLiNER extraction F1 < 0.70 (Entity extraction decay)
- Automated execution of model rollback via feature_client to previous verified champion.
- Audit and incident logging.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any

logger = logging.getLogger(__name__)

# Critical thresholds triggering automated rollback
CRITICAL_CNN_BRIER_MAX = 0.12
CRITICAL_RNN_COVERAGE_MIN = 0.65
CRITICAL_GLINER_F1_MIN = 0.70


class RollbackTriggerReason(str, Enum):
    HEALTHY = "HEALTHY"
    CALIBRATION_DECAY = "CALIBRATION_DECAY"
    COVERAGE_DECAY = "COVERAGE_DECAY"
    EXTRACTION_DECAY = "EXTRACTION_DECAY"


@dataclass
class DecayEvaluationResult:
    task: str
    model_id: str
    triggered_rollback: bool
    reason: RollbackTriggerReason
    diagnostic_detail: str
    rollback_response: dict[str, Any] | None = None
    evaluated_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


class DecayMonitorService:
    """Continuously monitors rolling specialist performance and automates circuit-breaker rollback."""

    def __init__(self, feature_client: Any, db: Any = None):
        self.feature_client = feature_client
        self.db = db
        self.history: list[dict[str, Any]] = []

    async def record_and_evaluate(self, eval_data: dict[str, Any]) -> DecayEvaluationResult:
        """Evaluate incoming candidate or live rolling metrics and trigger rollback if thresholds are breached."""
        task = eval_data.get("task", "")
        model_id = eval_data.get("model_id", "")
        metrics = eval_data.get("metrics", {})

        triggered = False
        reason = RollbackTriggerReason.HEALTHY
        detail = "Metrics within acceptable boundaries."

        # 1. Market CNN Calibration Check
        if task in ("market_cnn", "cnn_train"):
            brier = metrics.get("brier_score")
            if brier is not None and brier > CRITICAL_CNN_BRIER_MAX:
                triggered = True
                reason = RollbackTriggerReason.CALIBRATION_DECAY
                detail = (
                    f"Market CNN Brier score {brier:.4f} exceeded critical rollback threshold "
                    f"{CRITICAL_CNN_BRIER_MAX:.4f}."
                )

        # 2. Timeseries RNN Interval Coverage Check
        elif task in ("timeseries_rnn", "rnn_train"):
            cov = metrics.get("coverage_80")
            if cov is not None and cov < CRITICAL_RNN_COVERAGE_MIN:
                triggered = True
                reason = RollbackTriggerReason.COVERAGE_DECAY
                detail = (
                    f"Timeseries RNN 80% coverage {cov:.4f} fell below critical rollback threshold "
                    f"{CRITICAL_RNN_COVERAGE_MIN:.4f}."
                )

        # 3. GLiNER Extraction Quality Check
        elif task in ("gliner", "gliner_finetune"):
            f1 = metrics.get("f1")
            if f1 is not None and f1 < CRITICAL_GLINER_F1_MIN:
                triggered = True
                reason = RollbackTriggerReason.EXTRACTION_DECAY
                detail = (
                    f"GLiNER extraction F1 {f1:.4f} fell below critical rollback threshold "
                    f"{CRITICAL_GLINER_F1_MIN:.4f}."
                )

        rollback_resp = None
        if triggered:
            logger.error(
                "[DecayMonitor] CRITICAL REGRESSION detected on %s (%s): %s -> Executing rollback!",
                model_id, task, detail
            )
            try:
                rollback_resp = await self.feature_client.rollback_model(model_id)
            except Exception as e:
                logger.critical("[DecayMonitor] Rollback call failed for %s: %s", model_id, e)
                detail += f" [Rollback call error: {e}]"

        entry = {
            "task": task,
            "model_id": model_id,
            "metrics": metrics,
            "triggered_rollback": triggered,
            "reason": reason.value,
            "detail": detail,
            "rollback_response": rollback_resp,
            "evaluated_at": datetime.now(timezone.utc).isoformat(),
        }
        self.history.append(entry)

        return DecayEvaluationResult(
            task=task,
            model_id=model_id,
            triggered_rollback=triggered,
            reason=reason,
            diagnostic_detail=detail,
            rollback_response=rollback_resp,
        )
