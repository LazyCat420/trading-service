"""
Decay Monitor and Automated Rollback Service for Specialist Neural Models.

Enforces Item 7:
- Rolling out-of-time evaluation and historical test tracking.
- Critical regression thresholds:
  - CNN Brier score > 0.12 (Calibration decay)
  - RNN 80% coverage < 0.65 (Forecast interval decay)
  - GLiNER extraction F1 < 0.70 (Entity extraction decay)
- Missing, malformed, or stale evidence is marked as non-healthy incidents.
- Deduplication of rollback triggers per model incident.
- Automated execution of model rollback via feature_client to previous verified champion.
- Read-back active model verification after rollback.
- Persistent audit and incident logging in MongoDB.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from app.db import mongo_store

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
    MISSING_EVALUATION_DATA = "MISSING_EVALUATION_DATA"
    MALFORMED_METRICS = "MALFORMED_METRICS"
    STALE_EVALUATION = "STALE_EVALUATION"


class RollbackVerificationError(RuntimeError):
    """Raised when active model read-back indicates rollback failed to restore prior champion."""
    pass


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
    """Continuously monitors rolling specialist performance, persists incidents, and automates verified rollback."""

    COLLECTION_NAME = "specialist_decay_incidents"

    def __init__(self, feature_client: Any, db: Any = None):
        self.feature_client = feature_client
        self.db = db
        self.history: list[dict[str, Any]] = []

    def _get_collection(self) -> Any:
        if self.db is not None:
            if hasattr(self.db, "get_collection"):
                return self.db.get_collection(self.COLLECTION_NAME)
            return self.db[self.COLLECTION_NAME]
        if mongo_store:
            try:
                return mongo_store.get_doc_db()[self.COLLECTION_NAME]
            except Exception:
                if hasattr(mongo_store, "db") and mongo_store.db is not None:
                    return mongo_store.db[self.COLLECTION_NAME]
        return None

    async def record_and_evaluate(self, eval_data: dict[str, Any]) -> DecayEvaluationResult:
        """Evaluate incoming candidate or live rolling metrics and trigger rollback if thresholds are breached."""
        task = eval_data.get("task", "")
        model_id = eval_data.get("model_id", "")
        metrics = eval_data.get("metrics")
        col = self._get_collection()

        triggered = False
        reason = RollbackTriggerReason.HEALTHY
        detail = "Metrics within acceptable boundaries."

        # 1. Missing or non-dict evidence is NEVER healthy
        if metrics is None or not isinstance(metrics, dict) or len(metrics) == 0:
            reason = RollbackTriggerReason.MISSING_EVALUATION_DATA
            detail = f"Missing evaluation metrics for {task} ({model_id}); unmonitored model cannot be certified healthy."
        else:
            # 2. Check for stale evaluation timestamp if provided
            eval_ts = eval_data.get("timestamp") or eval_data.get("evaluated_at")
            if eval_ts:
                try:
                    dt = datetime.fromisoformat(eval_ts)
                    age_h = (datetime.now(timezone.utc) - dt).total_seconds() / 3600.0
                    if age_h > 48.0:
                        reason = RollbackTriggerReason.STALE_EVALUATION
                        detail = f"Evaluation evidence is stale ({age_h:.1f} hours old, max allowed: 48h)."
                except Exception:
                    pass

            t_lower = task.lower()
            # 3. Task-specific metric evaluation
            if "cnn" in t_lower or "regime" in t_lower:
                brier = metrics.get("brier_score")
                if brier is None:
                    reason = RollbackTriggerReason.MISSING_EVALUATION_DATA
                    detail = f"Missing required 'brier_score' metric for {task}."
                elif not isinstance(brier, (int, float)) or isinstance(brier, bool) or math.isnan(brier) or math.isinf(brier):
                    reason = RollbackTriggerReason.MALFORMED_METRICS
                    detail = f"Malformed 'brier_score' value {brier} for {task}."
                elif brier > CRITICAL_CNN_BRIER_MAX:
                    triggered = True
                    reason = RollbackTriggerReason.CALIBRATION_DECAY
                    detail = (
                        f"Market CNN Brier score {brier:.4f} exceeded critical rollback threshold "
                        f"{CRITICAL_CNN_BRIER_MAX:.4f}."
                    )

            elif "rnn" in t_lower or "volatility" in t_lower or "forecast" in t_lower:
                cov = metrics.get("coverage_80")
                if cov is None:
                    reason = RollbackTriggerReason.MISSING_EVALUATION_DATA
                    detail = f"Missing required 'coverage_80' metric for {task}."
                elif not isinstance(cov, (int, float)) or isinstance(cov, bool) or math.isnan(cov) or math.isinf(cov):
                    reason = RollbackTriggerReason.MALFORMED_METRICS
                    detail = f"Malformed 'coverage_80' value {cov} for {task}."
                elif cov < CRITICAL_RNN_COVERAGE_MIN:
                    triggered = True
                    reason = RollbackTriggerReason.COVERAGE_DECAY
                    detail = (
                        f"Timeseries RNN 80% coverage {cov:.4f} fell below critical rollback threshold "
                        f"{CRITICAL_RNN_COVERAGE_MIN:.4f}."
                    )

            elif "gliner" in t_lower or "entity" in t_lower:
                f1 = metrics.get("f1")
                if f1 is None:
                    reason = RollbackTriggerReason.MISSING_EVALUATION_DATA
                    detail = f"Missing required 'f1' metric for {task}."
                elif not isinstance(f1, (int, float)) or isinstance(f1, bool) or math.isnan(f1) or math.isinf(f1):
                    reason = RollbackTriggerReason.MALFORMED_METRICS
                    detail = f"Malformed 'f1' value {f1} for {task}."
                elif f1 < CRITICAL_GLINER_F1_MIN:
                    triggered = True
                    reason = RollbackTriggerReason.EXTRACTION_DECAY
                    detail = (
                        f"GLiNER extraction F1 {f1:.4f} fell below critical rollback threshold "
                        f"{CRITICAL_GLINER_F1_MIN:.4f}."
                    )

        rollback_resp = None
        if triggered:
            # 4. Deduplication: Check if rollback was already initiated for this model
            already_rolled_back = False
            if col is not None:
                prior_rb = col.find_one({
                    "model_id": model_id,
                    "task": task,
                    "triggered_rollback": True,
                    "status": "ROLLED_BACK",
                })
                if prior_rb:
                    already_rolled_back = True
                    logger.warning("[DecayMonitor] Rollback already executed for %s (%s), deduplicating", model_id, task)
                    detail += " [Rollback deduplicated: prior execution recorded]"
            else:
                for h in self.history:
                    if h.get("model_id") == model_id and h.get("triggered_rollback") and h.get("status") == "ROLLED_BACK":
                        already_rolled_back = True
                        detail += " [Rollback deduplicated: prior execution recorded]"
                        break

            if not already_rolled_back:
                logger.error(
                    "[DecayMonitor] CRITICAL REGRESSION detected on %s (%s): %s -> Executing rollback!",
                    model_id, task, detail
                )
                try:
                    rollback_resp = await self.feature_client.rollback_model(model_id)

                    # 5. Read-back active model verification: ensure model was restored
                    if hasattr(self.feature_client, "get_active_model"):
                        active_info = await self.feature_client.get_active_model(task)
                        active_model = active_info.get("model_id") if isinstance(active_info, dict) else str(active_info)
                        if active_model == model_id:
                            err_msg = (
                                f"Rollback failed verification: active model for task '{task}' "
                                f"is still decaying model '{model_id}'"
                            )
                            logger.critical("[DecayMonitor] %s", err_msg)
                            raise RollbackVerificationError(err_msg)

                except RollbackVerificationError:
                    raise
                except Exception as e:
                    logger.critical("[DecayMonitor] Rollback call failed for %s: %s", model_id, e)
                    detail += f" [Rollback call error: {e}]"

        now_iso = datetime.now(timezone.utc).isoformat()
        entry = {
            "task": task,
            "model_id": model_id,
            "metrics": metrics,
            "triggered_rollback": triggered,
            "status": "ROLLED_BACK" if triggered and not rollback_resp is None else ("DEGRADED" if reason != RollbackTriggerReason.HEALTHY else "HEALTHY"),
            "reason": reason.value,
            "detail": detail,
            "rollback_response": rollback_resp,
            "evaluated_at": now_iso,
        }

        if col is not None:
            col.insert_one(dict(entry))
        self.history.append(entry)

        return DecayEvaluationResult(
            task=task,
            model_id=model_id,
            triggered_rollback=triggered,
            reason=reason,
            diagnostic_detail=detail,
            rollback_response=rollback_resp,
            evaluated_at=now_iso,
        )
