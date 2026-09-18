"""
FastAPI router for Jetson Feature Platform training, GLM 5.3 curation, and model lifecycle endpoints.
"""

import logging
from typing import Any, Optional
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from app.services.jetson_feature_client import feature_client
from app.services.jetson_training_orchestrator import (
    JetsonTrainingOrchestrator,
    PromotionDecision,
    TrainingQueueBusyError,
)
from app.services.glm_curator_service import GLMCuratorService
from app.services.dataset_manifest_builder import DatasetManifestBuilder

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/features/training", tags=["Feature Training & Model Lifecycle"])


class CurationRequest(BaseModel):
    task: str = Field(default="gliner_finetune", description="Task type")
    texts: list[str] = Field(description="Raw texts or articles to annotate")
    num_samples: int = Field(default=3, ge=1, le=5, description="Number of consensus sampling passes")
    min_agreement: int = Field(default=2, ge=1, description="Minimum agreeing passes required")
    auto_submit: bool = Field(default=False, description="If true, submits training job to Jetson after manifest creation")


@router.get("/health")
async def get_training_health() -> dict[str, Any]:
    """Returns Jetson Orin training capacity, queue state, and GPU telemetry."""
    try:
        return await feature_client.get_health()
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Failed to query Jetson feature platform: {e}")


@router.get("/jobs")
async def list_training_jobs() -> list[dict[str, Any]]:
    """Lists all submitted training jobs from Jetson."""
    try:
        return await feature_client.list_training_jobs()
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Failed to list training jobs: {e}")


@router.get("/jobs/{job_id}")
async def get_training_job_status(job_id: str) -> dict[str, Any]:
    """Retrieves progress, metrics, and logs for a training job."""
    try:
        return await feature_client.get_training_job(job_id)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Failed to get training job {job_id}: {e}")


@router.post("/jobs/{job_id}/cancel")
async def cancel_training_job(job_id: str) -> dict[str, Any]:
    """Cancels a pending or running training job on Jetson."""
    try:
        return await feature_client.cancel_training_job(job_id)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Failed to cancel training job {job_id}: {e}")


@router.post("/curate-and-train")
async def curate_and_train(req: CurationRequest) -> dict[str, Any]:
    """
    Curates financial training data with GLM-5.3-Flash-EXL3 multi-sample consensus,
    builds an immutable chronological manifest, and optionally submits a training job.
    """
    curator = GLMCuratorService()
    builder = DatasetManifestBuilder()

    annotated_samples = []
    for idx, text in enumerate(req.texts):
        try:
            entities = await curator.annotate_text_with_consensus(
                text, num_samples=req.num_samples, min_agreement=req.min_agreement
            )
            formatted = curator.format_for_gliner_training(text, entities)
            annotated_samples.append({
                "id": f"curated_{idx}",
                "timestamp": None,
                "tokenized_text": formatted["tokenized_text"],
                "ner": formatted["ner"],
            })
        except Exception as e:
            logger.warning("[FeatureTrainingRouter] Curation failed for text %d: %s", idx, e)

    manifest = builder.build_manifest(
        task=req.task,
        samples=annotated_samples,
    )

    result: dict[str, Any] = {
        "manifest_id": manifest["manifest_id"],
        "task": req.task,
        "total_samples": len(annotated_samples),
        "sha256": manifest["sha256"],
        "manifest_path": manifest["manifest_path"],
    }

    if req.auto_submit and annotated_samples:
        orchestrator = JetsonTrainingOrchestrator(client=feature_client)
        try:
            orch_res = await orchestrator.submit_and_orchestrate(
                task=req.task,
                base_model_id="gliner",
                dataset_manifest_id=manifest["manifest_id"],
            )
            result["orchestration"] = {
                "decision": orch_res.decision.value,
                "candidate_model_id": orch_res.candidate_model_id,
                "reason": orch_res.reason,
            }
        except TrainingQueueBusyError as e:
            result["orchestration"] = {"decision": "queued_busy", "reason": str(e)}

    return result


@router.post("/models/{candidate_id}/evaluate")
async def evaluate_candidate(candidate_id: str) -> dict[str, Any]:
    """Runs the frozen test evaluation suite on an immutable candidate artifact."""
    try:
        return await feature_client.evaluate_candidate(candidate_id)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Evaluation failed: {e}")


@router.post("/models/{candidate_id}/promote")
async def promote_candidate(
    candidate_id: str,
    task: str = Query(default="gliner_finetune", description="Task type for threshold evaluation"),
) -> dict[str, Any]:
    """Evaluates candidate model against deterministic policy gates and promotes if passed."""
    orchestrator = JetsonTrainingOrchestrator(client=feature_client)
    try:
        eval_resp = await feature_client.evaluate_candidate(candidate_id)
        metrics = eval_resp.get("metrics", {})
        passed, reason = orchestrator.evaluate_promotion_gate(task, metrics)
        if passed:
            promo_resp = await feature_client.promote_candidate(candidate_id)
            return {
                "decision": PromotionDecision.PROMOTED.value,
                "model_id": candidate_id,
                "reason": reason,
                "promotion_response": promo_resp,
            }
        else:
            return {
                "decision": PromotionDecision.REJECTED.value,
                "model_id": candidate_id,
                "reason": reason,
                "metrics": metrics,
            }
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Promotion failed: {e}")


@router.post("/models/{model_id}/rollback")
async def rollback_model(model_id: str) -> dict[str, Any]:
    """Rolls back the active model to the prior champion in history."""
    try:
        return await feature_client.rollback_model(model_id)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Rollback failed: {e}")
