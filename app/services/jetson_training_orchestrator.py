"""
JetsonTrainingOrchestrator — Asynchronous training job lifecycle manager and deterministic promotion gatekeeper.

Orchestrates the lifecycle of candidate model training on Jetson Orin (Port 8002):
1. Pre-flight concurrency gate: validates Jetson training queue is not busy.
2. Submits asynchronous training job.
3. Asynchronously polls job execution until terminal status.
4. Evaluates candidate model on frozen holdout suite.
5. Enforces mathematical promotion criteria before promotion.
6. Promotes candidate to active champion or rejects candidate with logged reasoning.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from enum import Enum
import logging
import time
from typing import Any, Optional

from app.services.jetson_feature_client import JetsonFeatureClient, feature_client

logger = logging.getLogger(__name__)


class PromotionDecision(str, Enum):
    PROMOTED = "promoted"
    REJECTED = "rejected"
    FAILED = "failed"


class TrainingQueueBusyError(Exception):
    """Raised when Jetson Orin training queue is already occupied."""
    pass


class TrainingJobFailedError(Exception):
    """Raised when training job fails or is cancelled on Jetson."""
    pass


@dataclass
class OrchestrationResult:
    job_id: str
    decision: PromotionDecision
    candidate_model_id: Optional[str]
    metrics: dict[str, Any]
    reason: str


class JetsonTrainingOrchestrator:
    """Manages training jobs, status polling, holdout evaluation, and policy promotion."""

    def __init__(self, client: JetsonFeatureClient | None = None):
        self.client = client or feature_client

    async def check_training_capacity(self) -> None:
        """Verifies Jetson training queue has available capacity (training_active < training_max)."""
        health = await self.client.get_health()
        queue = health.get("queue", {})
        active = queue.get("training_active", 0)
        max_active = queue.get("training_max", 1)

        if active >= max_active:
            raise TrainingQueueBusyError(
                f"Jetson training queue busy ({active}/{max_active} active jobs). Retry later."
            )

    def evaluate_promotion_gate(self, task: str, metrics: dict[str, Any]) -> tuple[bool, str]:
        """
        Applies deterministic mathematical promotion rules.
        GLM 5.3 does not vote on promotion; only frozen holdout metrics decide.
        """
        if "gliner" in task.lower():
            f1 = float(metrics.get("f1", 0.0))
            precision = float(metrics.get("precision", 0.0))
            recall = float(metrics.get("recall", 0.0))
            p99_latency = float(metrics.get("latency_p99_ms", 0.0))

            min_f1 = 0.912
            min_prec = 0.90
            min_rec = 0.88
            max_lat = 50.0

            if f1 < min_f1:
                return False, f"F1 {f1:.3f} below threshold {min_f1:.3f}"
            if precision < min_prec:
                return False, f"Precision {precision:.3f} below threshold {min_prec:.3f}"
            if recall < min_rec:
                return False, f"Recall {recall:.3f} below threshold {min_rec:.3f}"
            if p99_latency > max_lat:
                return False, f"P99 latency {p99_latency:.1f}ms exceeds cap {max_lat:.1f}ms"

            return True, f"Candidate beat baseline with F1={f1:.3f}, Precision={precision:.3f}, Recall={recall:.3f}"

        elif "cnn" in task.lower() or "regime" in task.lower():
            macro_f1 = float(metrics.get("macro_f1", 0.0))
            brier = float(metrics.get("brier_score", 1.0))
            if macro_f1 < 0.865:
                return False, f"Macro F1 {macro_f1:.3f} below threshold 0.865"
            if brier > 0.095:
                return False, f"Brier score {brier:.3f} exceeds threshold 0.095"
            return True, f"Candidate beat baseline with Macro F1={macro_f1:.3f}, Brier={brier:.3f}"

        elif "rnn" in task.lower() or "forecast" in task.lower():
            rmse = float(metrics.get("rmse", 1.0))
            cov = float(metrics.get("coverage_80", 0.0))
            if rmse > 0.024:
                return False, f"RMSE {rmse:.4f} exceeds threshold 0.024"
            if not (0.75 <= cov <= 0.85):
                return False, f"Coverage {cov:.3f} outside bounds [0.75, 0.85]"
            return True, f"Candidate beat baseline with RMSE={rmse:.4f}, Coverage={cov:.3f}"

        # Default: require gate_ready flag
        return metrics.get("gate_ready", False), "Gate evaluated by task default"

    async def submit_and_orchestrate(
        self,
        task: str,
        base_model_id: str,
        dataset_manifest_id: str | None = None,
        hyperparameters: dict[str, Any] | None = None,
        proposal_id: str | None = None,
        poll_interval_s: float = 5.0,
        max_poll_seconds: float = 600.0,
    ) -> OrchestrationResult:
        """
        Full orchestration loop:
        1. Pre-flight capacity check
        2. Submit training job
        3. Poll for completion
        4. Evaluate candidate on frozen holdout suite
        5. Verify deterministic promotion gate
        6. Promote or reject
        """
        # 1. Capacity check
        await self.check_training_capacity()

        # 2. Submit job
        sub = await self.client.submit_training_job(
            task=task,
            base_model_id=base_model_id,
            dataset_manifest_id=dataset_manifest_id,
            hyperparameters=hyperparameters,
            proposal_id=proposal_id,
        )
        job_id = sub["job_id"]
        logger.info("[TrainingOrchestrator] Job %s submitted for task '%s'", job_id, task)

        # 3. Poll for completion
        start_time = time.monotonic()
        cand_model_id = None
        while time.monotonic() - start_time < max_poll_seconds:
            status_resp = await self.client.get_training_job(job_id)
            status = status_resp.get("status")

            if status == "completed":
                cand_model_id = status_resp.get("candidate_model_id")
                logger.info("[TrainingOrchestrator] Job %s completed. Candidate: %s", job_id, cand_model_id)
                break
            elif status in ("failed", "cancelled"):
                err_msg = status_resp.get("error_message") or f"Job terminated with status: {status}"
                return OrchestrationResult(
                    job_id=job_id,
                    decision=PromotionDecision.FAILED,
                    candidate_model_id=None,
                    metrics={},
                    reason=err_msg,
                )

            await asyncio.sleep(poll_interval_s)

        if not cand_model_id:
            return OrchestrationResult(
                job_id=job_id,
                decision=PromotionDecision.FAILED,
                candidate_model_id=None,
                metrics={},
                reason=f"Training job timed out after {max_poll_seconds}s",
            )

        # 4. Evaluate candidate
        eval_resp = await self.client.evaluate_candidate(cand_model_id)
        metrics = eval_resp.get("metrics", {})

        # 5. Deterministic promotion gate
        passed, reason = self.evaluate_promotion_gate(task, metrics)

        # 6. Promote or reject
        if passed:
            promo_resp = await self.client.promote_candidate(cand_model_id)
            logger.info("[TrainingOrchestrator] Candidate %s PROMOTED: %s", cand_model_id, reason)
            return OrchestrationResult(
                job_id=job_id,
                decision=PromotionDecision.PROMOTED,
                candidate_model_id=cand_model_id,
                metrics=metrics,
                reason=reason,
            )
        else:
            logger.warning("[TrainingOrchestrator] Candidate %s REJECTED: %s", cand_model_id, reason)
            return OrchestrationResult(
                job_id=job_id,
                decision=PromotionDecision.REJECTED,
                candidate_model_id=cand_model_id,
                metrics=metrics,
                reason=reason,
            )
