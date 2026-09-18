"""
JetsonTrainingOrchestrator — Asynchronous training job lifecycle manager and deterministic promotion gatekeeper.

Orchestrates the lifecycle of candidate model training on Jetson Orin (Port 8002):
1. Pre-flight concurrency gate: validates Jetson training queue is not busy.
2. Submits asynchronous training job.
3. Asynchronously polls job execution until terminal status.
4. Evaluates candidate model on frozen holdout suite.
5. Enforces fail-closed mathematical promotion criteria and anti-regression guardrails.
6. Promotes candidate to active champion or rejects candidate with logged reasoning.
7. Verifies active model state post-promotion.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from enum import Enum
import logging
import math
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


class PromotionVerificationError(Exception):
    """Raised when active model verification fails after promotion."""
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

    def evaluate_promotion_gate(
        self,
        candidate_metadata: dict[str, Any] | str,
        candidate_eval: dict[str, Any],
        champion_eval: dict[str, Any] | None = None,
        expected_champion_version: str | None = None,
        slice_regression_tolerance: float = 0.02,
        primary_margin: float = 0.0,
    ) -> tuple[bool, str]:
        """
        Applies deterministic fail-closed mathematical promotion rules.
        Rejects missing metrics, NaN/infinite values, invalid types, unknown tasks,
        insufficient sample sizes, identity mismatches, champion regression,
        and optimistic locking violations.
        """
        # 1. Normalize metadata and evaluation payloads
        if isinstance(candidate_metadata, str):
            task = candidate_metadata
            cand_id = candidate_eval.get("model_id") or candidate_eval.get("candidate_model_id")
        else:
            task = candidate_metadata.get("task") or ""
            cand_id = (
                candidate_metadata.get("candidate_model_id")
                or candidate_metadata.get("model_id")
            )

        if not task:
            return False, "Unknown or missing task in candidate metadata"

        # Check candidate identity match if model_id is present in evaluation
        eval_model_id = candidate_eval.get("model_id") or candidate_eval.get("candidate_model_id")
        if cand_id and eval_model_id and cand_id != eval_model_id:
            return False, f"Candidate identity mismatch (metadata '{cand_id}' vs eval '{eval_model_id}')"

        # Extract metrics dictionary
        metrics = candidate_eval.get("metrics")
        if metrics is None and isinstance(candidate_eval, dict):
            # Backward compatibility if metrics were passed at root
            metrics = candidate_eval
        if not isinstance(metrics, dict):
            return False, "Candidate evaluation missing valid 'metrics' dictionary"

        # 2. Enforce sample size floor
        sample_count = candidate_eval.get("sample_count")
        if sample_count is None and "samples_evaluated" in metrics:
            sample_count = metrics["samples_evaluated"]

        t_lower = task.lower()
        if "gliner" in t_lower or "entity" in t_lower:
            task_type = "gliner"
            min_samples = 100
            required_keys = ["f1", "precision", "recall", "latency_p99_ms"]
        elif "cnn" in t_lower or "regime" in t_lower:
            task_type = "cnn"
            min_samples = 100
            required_keys = ["macro_f1", "brier_score"]
        elif "rnn" in t_lower or "forecast" in t_lower:
            task_type = "rnn"
            min_samples = 500
            required_keys = ["rmse", "coverage_80"]
        else:
            return False, f"Unknown task type '{task}' in candidate metadata"

        if sample_count is not None and sample_count < min_samples:
            return False, f"Sample count {sample_count} below task floor of {min_samples}"

        # 3. Sanitize metrics: reject missing, non-numeric, NaN, infinity
        clean_metrics: dict[str, float] = {}
        for req_key in required_keys:
            if req_key not in metrics or metrics[req_key] is None:
                return False, f"Missing required metric '{req_key}' for task {task_type}"

            raw_val = metrics[req_key]
            # Strict type check: float or int
            if not isinstance(raw_val, (int, float)) or isinstance(raw_val, bool):
                return False, f"Invalid type for metric '{req_key}': expected float/int, got {type(raw_val).__name__}"

            val = float(raw_val)
            if math.isnan(val) or math.isinf(val):
                return False, f"Metric '{req_key}' has non-finite value: {val}"

            clean_metrics[req_key] = val

        # 4. Check absolute quality thresholds
        if task_type == "gliner":
            f1 = clean_metrics["f1"]
            prec = clean_metrics["precision"]
            rec = clean_metrics["recall"]
            p99 = clean_metrics["latency_p99_ms"]

            if f1 < 0.912:
                return False, f"F1 {f1:.3f} below threshold 0.912"
            if prec < 0.90:
                return False, f"Precision {prec:.3f} below threshold 0.90"
            if rec < 0.88:
                return False, f"Recall {rec:.3f} below threshold 0.88"
            if p99 > 50.0:
                return False, f"P99 latency {p99:.1f}ms exceeds cap 50.0ms"

        elif task_type == "cnn":
            macro_f1 = clean_metrics["macro_f1"]
            brier = clean_metrics["brier_score"]

            if macro_f1 < 0.865:
                return False, f"Macro F1 {macro_f1:.3f} below threshold 0.865"
            if brier > 0.095:
                return False, f"Brier score {brier:.3f} exceeds threshold 0.095"

        elif task_type == "rnn":
            rmse = clean_metrics["rmse"]
            cov = clean_metrics["coverage_80"]

            if rmse > 0.024:
                return False, f"RMSE {rmse:.4f} exceeds threshold 0.024"
            if not (0.75 <= cov <= 0.85):
                return False, f"Coverage {cov:.3f} outside bounds [0.75, 0.85]"

        # 5. Anti-Regression Against Current Champion (Item 3)
        if champion_eval is not None:
            # A. Enforce identical dataset manifest
            cand_manifest = candidate_eval.get("dataset_manifest_id")
            champ_manifest = champion_eval.get("dataset_manifest_id")
            if cand_manifest and champ_manifest and cand_manifest != champ_manifest:
                return (
                    False,
                    f"Candidate and Champion evaluated on different dataset manifests ('{cand_manifest}' vs '{champ_manifest}')",
                )

            # B. Optimistic locking on champion version
            champ_model_id = champion_eval.get("model_id") or champion_eval.get("champion_model_id")
            if expected_champion_version and champ_model_id and champ_model_id != expected_champion_version:
                return (
                    False,
                    f"Champion version mismatch (expected '{expected_champion_version}', got '{champ_model_id}'): optimistic lock failed",
                )

            # C. Check primary metric regression
            champ_metrics = champion_eval.get("metrics", champion_eval)
            if task_type == "gliner":
                champ_f1 = float(champ_metrics.get("f1", 0.0))
                if clean_metrics["f1"] < champ_f1 + primary_margin:
                    return (
                        False,
                        f"Candidate F1 ({clean_metrics['f1']:.3f}) regresses against champion F1 ({champ_f1:.3f})",
                    )
            elif task_type == "cnn":
                champ_f1 = float(champ_metrics.get("macro_f1", 0.0))
                if clean_metrics["macro_f1"] < champ_f1 + primary_margin:
                    return (
                        False,
                        f"Candidate Macro F1 ({clean_metrics['macro_f1']:.3f}) regresses against champion ({champ_f1:.3f})",
                    )
            elif task_type == "rnn":
                champ_rmse = float(champ_metrics.get("rmse", 1.0))
                if clean_metrics["rmse"] > champ_rmse - primary_margin:
                    return (
                        False,
                        f"Candidate RMSE ({clean_metrics['rmse']:.4f}) regresses against champion ({champ_rmse:.4f})",
                    )

            # D. Critical slice regression check
            cand_slices = candidate_eval.get("slices", {})
            champ_slices = champion_eval.get("slices", {})
            if isinstance(cand_slices, dict) and isinstance(champ_slices, dict):
                for slice_name, c_slice in cand_slices.items():
                    if slice_name in champ_slices:
                        ch_slice = champ_slices[slice_name]
                        # Compare F1 for gliner/cnn, or RMSE for rnn
                        if task_type in ("gliner", "cnn"):
                            metric_name = "f1" if "f1" in c_slice else "macro_f1"
                            c_val = float(c_slice.get(metric_name, 0.0))
                            ch_val = float(ch_slice.get(metric_name, 0.0))
                            if c_val < ch_val - slice_regression_tolerance:
                                return (
                                    False,
                                    f"Candidate regressed on critical slice '{slice_name}': {c_val:.3f} vs champion {ch_val:.3f}",
                                )
                        elif task_type == "rnn":
                            c_rmse = float(c_slice.get("rmse", 1.0))
                            ch_rmse = float(ch_slice.get("rmse", 1.0))
                            if c_rmse > ch_rmse + slice_regression_tolerance:
                                return (
                                    False,
                                    f"Candidate regressed on critical slice '{slice_name}': RMSE {c_rmse:.4f} vs champion {ch_rmse:.4f}",
                                )

        return True, f"Candidate beat baseline and champion thresholds for {task_type}"

    async def submit_and_orchestrate(
        self,
        task: str,
        base_model_id: str,
        dataset_manifest_id: str | None = None,
        hyperparameters: dict[str, Any] | None = None,
        proposal_id: str | None = None,
        poll_interval_s: float = 5.0,
        max_poll_seconds: float = 600.0,
        champion_eval: dict[str, Any] | None = None,
        expected_champion_version: str | None = None,
        verify_active_after_promotion: bool = True,
    ) -> OrchestrationResult:
        """
        Full orchestration loop:
        1. Pre-flight capacity check
        2. Submit training job
        3. Poll for completion
        4. Evaluate candidate on frozen holdout suite
        5. Verify fail-closed deterministic promotion gate
        6. Promote or reject
        7. Post-promotion active model verification
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
        metrics = eval_resp.get("metrics", eval_resp)

        # 5. Deterministic promotion gate
        cand_metadata = {"candidate_model_id": cand_model_id, "task": task}
        passed, reason = self.evaluate_promotion_gate(
            candidate_metadata=cand_metadata,
            candidate_eval=eval_resp,
            champion_eval=champion_eval,
            expected_champion_version=expected_champion_version,
        )

        # 6. Promote or reject
        if passed:
            await self.client.promote_candidate(cand_model_id)
            logger.info("[TrainingOrchestrator] Candidate %s PROMOTED: %s", cand_model_id, reason)

            # 7. Post-promotion active model verification
            if verify_active_after_promotion:
                active_model = await self.client.get_active_model(task)
                if active_model != cand_model_id:
                    raise PromotionVerificationError(
                        f"Post-promotion verification failed: active model for task '{task}' is '{active_model}', expected '{cand_model_id}'"
                    )

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
