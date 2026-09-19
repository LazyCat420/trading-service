import datetime
import logging
from typing import Any, Optional
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from app.db import mongo_store
from app.services.jetson_feature_client import feature_client
from app.services.jetson_training_orchestrator import (
    JetsonTrainingOrchestrator,
    PromotionDecision,
    TrainingQueueBusyError,
)
from app.services.glm_curator_service import GLMCuratorService
from app.services.dataset_manifest_builder import DatasetManifestBuilder
from app.services.durable_training_service import DurableTrainingService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/features/training", tags=["Feature Training & Model Lifecycle"])


class CurationRequest(BaseModel):
    task: str = Field(default="gliner_finetune", description="Task type: 'gliner_finetune', 'cnn_regime', or 'rnn_volatility'")
    texts: list[str] = Field(default_factory=list, description="Raw texts or articles to annotate (for gliner)")
    samples: list[dict[str, Any]] = Field(default_factory=list, description="Pre-structured or pre-annotated samples")
    num_samples: int = Field(default=3, ge=1, le=5, description="Number of consensus sampling passes")
    min_agreement: int = Field(default=2, ge=1, description="Minimum agreeing passes required")
    auto_submit: bool = Field(default=False, description="If true, submits durable training job after manifest creation")
    hyperparameters: dict[str, Any] = Field(default_factory=dict, description="Training hyperparameters")


class PromotionRequestBody(BaseModel):
    task: str = Field(default="gliner_finetune", description="Task type")
    candidate_metrics: dict[str, Any] = Field(default_factory=dict, description="Candidate evaluation metrics")
    sample_count: int | None = Field(default=None, description="Sample count evaluated")
    slices: dict[str, Any] = Field(default_factory=dict, description="Evaluated slices")
    dataset_manifest_id: str | None = Field(default=None, description="Dataset manifest ID")
    champion_eval: dict[str, Any] | None = Field(default=None, description="Champion evaluation result")
    expected_champion_version: str | None = Field(default=None, description="Expected champion model ID")
    require_champion_eval: bool = Field(default=False, description="Require champion evaluation check")


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
    Curates financial training data for GLiNER, CNN, or RNN, assigns valid timestamps,
    builds an immutable chronological manifest, and optionally submits a durable training job.
    """
    curator = GLMCuratorService()
    builder = DatasetManifestBuilder()
    now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()

    annotated_samples: list[dict[str, Any]] = []
    t_lower = req.task.lower()

    if "gliner" in t_lower or "entity" in t_lower:
        base_model = "gliner"
        # 1. If explicit texts provided, run GLM consensus annotation
        for idx, text in enumerate(req.texts):
            try:
                entities = await curator.annotate_text_with_consensus(
                    text, num_samples=req.num_samples, min_agreement=req.min_agreement
                )
                formatted = curator.format_for_gliner_training(text, entities)
                annotated_samples.append({
                    "id": f"curated_gliner_{idx}_{now_iso}",
                    "timestamp": now_iso,
                    "tokenized_text": formatted["tokenized_text"],
                    "ner": formatted["ner"],
                })
            except Exception as e:
                logger.warning("[FeatureTrainingRouter] Curation failed for text %d: %s", idx, e)

        # 2. If pre-structured samples provided
        for idx, s in enumerate(req.samples):
            s_ts = s.get("timestamp") or now_iso
            s_id = s.get("id") or f"sample_gliner_{idx}_{now_iso}"
            annotated_samples.append({
                "id": s_id,
                "timestamp": s_ts,
                "tokenized_text": s.get("tokenized_text", []),
                "ner": s.get("ner", []),
            })

    elif "cnn" in t_lower or "regime" in t_lower:
        base_model = "cnn"
        for idx, s in enumerate(req.samples):
            s_ts = s.get("timestamp") or now_iso
            s_id = s.get("id") or f"sample_cnn_{idx}_{now_iso}"
            annotated_samples.append({
                "id": s_id,
                "timestamp": s_ts,
                "features": s.get("features", []),
                "label": s.get("label", 0),
            })

    elif "rnn" in t_lower or "volatility" in t_lower or "forecast" in t_lower:
        base_model = "rnn"
        for idx, s in enumerate(req.samples):
            s_ts = s.get("timestamp") or now_iso
            s_id = s.get("id") or f"sample_rnn_{idx}_{now_iso}"
            annotated_samples.append({
                "id": s_id,
                "timestamp": s_ts,
                "features": s.get("features", []),
                "quantiles": s.get("quantiles", {}),
            })
    else:
        raise HTTPException(status_code=400, detail=f"Unsupported task '{req.task}'")

    if not annotated_samples:
        raise HTTPException(status_code=400, detail="No valid samples could be formatted or curated for task")

    manifest = builder.build_manifest(
        task=req.task,
        samples=annotated_samples,
    )

    result: dict[str, Any] = {
        "manifest_id": manifest["manifest_id"],
        "task": req.task,
        "base_model": base_model,
        "total_samples": len(annotated_samples),
        "sha256": manifest["sha256"],
        "manifest_path": manifest["manifest_path"],
    }

    if req.auto_submit:
        doc_db = None
        try:
            doc_db = mongo_store.get_doc_db()
        except Exception:
            if hasattr(mongo_store, "db"):
                doc_db = mongo_store.db
        durable_svc = DurableTrainingService(db=doc_db, client=feature_client)
        try:
            durable_job = await durable_svc.submit_job(
                task=req.task,
                base_model_id=base_model,
                dataset_manifest_id=manifest["manifest_id"],
                hyperparameters=req.hyperparameters,
            )
            result["durable_job_id"] = durable_job.job_id
            result["status"] = durable_job.status.value
        except Exception as e:
            logger.error("[FeatureTrainingRouter] Durable job submission failed: %s", e)
            result["submission_error"] = str(e)

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
    req: Optional[PromotionRequestBody] = None,
    task: str = Query(default="gliner_finetune", description="Task type for threshold evaluation"),
) -> dict[str, Any]:
    """
    Evaluates candidate model against deterministic fail-closed policy gates,
    checks champion regression, promotes candidate on Jetson, and verifies active model.
    """
    orchestrator = JetsonTrainingOrchestrator(client=feature_client)

    try:
        # Server-side evaluation: NEVER trust caller-supplied metrics directly
        target_task = req.task if req and req.task else task
        eval_resp = await feature_client.evaluate_candidate(candidate_id)
        metrics = eval_resp.get("metrics", {})
        sample_count = eval_resp.get("sample_count")
        slices = eval_resp.get("slices", {})
        manifest_id = eval_resp.get("dataset_manifest_id") or (req.dataset_manifest_id if req else None)

        # Query active champion and evaluate it server-side on the same holdout test slice
        champion_eval = None
        expected_champ = None
        require_champ = False
        try:
            active_info = await feature_client.get_active_model(target_task)
        except Exception as ce:
            logger.error("[FeatureTrainingRouter] Champion discovery failed for %s: %s", target_task, ce)
            raise HTTPException(
                status_code=502,
                detail=f"Champion discovery failed for task '{target_task}': {ce}",
            )

        champ_id = active_info.get("model_id") if isinstance(active_info, dict) else str(active_info) if active_info else None
        if champ_id and champ_id not in ("unknown", "none", "", candidate_id):
            expected_champ = champ_id
            require_champ = True
            try:
                champion_eval = await feature_client.evaluate_candidate(champ_id)
            except Exception as ce:
                logger.error("[FeatureTrainingRouter] Champion evaluation failed for %s (%s): %s", target_task, champ_id, ce)
                raise HTTPException(
                    status_code=502,
                    detail=f"Champion evaluation failed for active champion '{champ_id}' ({target_task}): {ce}",
                )

        cand_metadata = {
            "candidate_model_id": candidate_id,
            "task": target_task,
        }
        cand_eval_payload = {
            "model_id": candidate_id,
            "metrics": metrics,
            "sample_count": sample_count,
            "slices": slices,
            "dataset_manifest_id": manifest_id,
        }

        passed, reason = orchestrator.evaluate_promotion_gate(
            candidate_metadata=cand_metadata,
            candidate_eval=cand_eval_payload,
            champion_eval=champion_eval,
            expected_champion_version=expected_champ,
            require_champion_eval=require_champ,
        )

        if not passed:
            raise HTTPException(
                status_code=400,
                detail={
                    "decision": PromotionDecision.REJECTED.value,
                    "model_id": candidate_id,
                    "reason": reason,
                    "metrics": metrics,
                },
            )

        # Atomic Compare-And-Swap (CAS) recheck immediately prior to promotion
        if expected_champ:
            try:
                current_active_info = await feature_client.get_active_model(target_task)
                current_champ = current_active_info.get("model_id") if isinstance(current_active_info, dict) else str(current_active_info) if current_active_info else None
                if current_champ and current_champ != expected_champ:
                    raise HTTPException(
                        status_code=409,
                        detail={
                            "decision": PromotionDecision.REJECTED.value,
                            "model_id": candidate_id,
                            "reason": f"Concurrent promotion detected: active champion changed from '{expected_champ}' to '{current_champ}' before promotion execution",
                            "expected_champion": expected_champ,
                            "current_champion": current_champ,
                        },
                    )
            except HTTPException:
                raise
            except Exception as ce:
                logger.error("[FeatureTrainingRouter] Pre-promotion champion verification failed for %s: %s", target_task, ce)
                raise HTTPException(
                    status_code=502,
                    detail=f"Pre-promotion champion verification failed: {ce}",
                )

        promo_resp = await feature_client.promote_candidate(candidate_id)

        # Post-promotion verification: active model MUST match candidate_id
        active_info = await feature_client.get_active_model(target_task)
        active_model = active_info.get("model_id") if isinstance(active_info, dict) else str(active_info)
        if active_model != candidate_id:
            raise HTTPException(
                status_code=500,
                detail=f"Promotion verification failed: active model is '{active_model}', expected '{candidate_id}'",
            )

        return {
            "decision": PromotionDecision.PROMOTED.value,
            "model_id": candidate_id,
            "reason": reason,
            "promotion_response": promo_resp,
            "active_model": active_model,
        }

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Promotion failed: {e}")


@router.post("/models/{model_id}/rollback")
async def rollback_model(model_id: str) -> dict[str, Any]:
    """Rolls back the active model to the prior champion in history."""
    try:
        return await feature_client.rollback_model(model_id)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Rollback failed: {e}")


class RetrainingProposalRequest(BaseModel):
    task: str = Field(description="Target specialist task ('gliner', 'market_cnn', 'timeseries_rnn')")
    candidate_name: str = Field(description="Proposed candidate model name")
    dataset_manifest_id: str = Field(description="Manifest ID for training")
    hyperparameters: dict[str, Any] = Field(description="Proposed training hyperparameters")
    failure_cluster_ids: list[str] = Field(default_factory=list, description="Associated failure cluster IDs")
    justification: str = Field(default="", description="GLM reasoning and justification")


@router.post("/proposals/submit")
async def submit_retraining_proposal(req: RetrainingProposalRequest) -> dict[str, Any]:
    """
    Evaluates GLM autonomous retraining proposal against persistent limits (24h budget, cooldown,
    isolated holdouts, hyperparameter bounds) and submits real durable training job when accepted.
    """
    from app.services.glm_retraining_proposal_service import (
        GLMRetrainingProposalService,
        RetrainingProposal,
        ProposalStatus,
    )
    doc_db = None
    try:
        doc_db = mongo_store.get_doc_db()
    except Exception:
        if hasattr(mongo_store, "db"):
            doc_db = mongo_store.db
    proposal_svc = GLMRetrainingProposalService(db=doc_db)
    durable_svc = DurableTrainingService(db=doc_db, client=feature_client)

    proposal = RetrainingProposal(
        task=req.task,
        candidate_name=req.candidate_name,
        dataset_manifest_id=req.dataset_manifest_id,
        hyperparameters=req.hyperparameters,
        failure_cluster_ids=req.failure_cluster_ids,
        justification=req.justification,
    )

    result = await proposal_svc.submit_proposal_to_training(proposal, durable_service=durable_svc)
    if result.status != ProposalStatus.ACCEPTED:
        raise HTTPException(
            status_code=400,
            detail={
                "proposal_id": result.proposal_id,
                "status": result.status.value,
                "reason": result.reason,
            },
        )

    return {
        "proposal_id": result.proposal_id,
        "status": result.status.value,
        "job_id": result.job_id,
        "reason": result.reason,
    }


class DecayEvaluationRequest(BaseModel):
    task: str = Field(description="Specialist task to evaluate ('gliner', 'market_cnn', 'timeseries_rnn')")
    model_id: str = Field(description="Active model ID")
    metrics: dict[str, Any] = Field(description="Rolling evaluation metrics")
    timestamp: str | None = Field(default=None, description="Evaluation timestamp")
    expected_champion: str | None = Field(default=None, description="Expected champion model ID to restore upon rollback")


@router.post("/decay/evaluate")
async def evaluate_specialist_decay(req: DecayEvaluationRequest) -> dict[str, Any]:
    """
    Evaluates rolling metrics against decay thresholds, logs incidents in MongoDB,
    and executes verified rollback with active model read-back verification.
    """
    from app.services.decay_monitor_service import (
        DecayMonitorService,
        RollbackVerificationError,
    )
    doc_db = None
    try:
        doc_db = mongo_store.get_doc_db()
    except Exception:
        if hasattr(mongo_store, "db"):
            doc_db = mongo_store.db
    decay_svc = DecayMonitorService(db=doc_db, feature_client=feature_client)
    try:
        eval_result = await decay_svc.record_and_evaluate({
            "task": req.task,
            "model_id": req.model_id,
            "metrics": req.metrics,
            "timestamp": req.timestamp,
            "expected_champion": req.expected_champion,
        })
        return {
            "task": eval_result.task,
            "model_id": eval_result.model_id,
            "triggered_rollback": eval_result.triggered_rollback,
            "reason": eval_result.reason.value,
            "diagnostic_detail": eval_result.diagnostic_detail,
            "rollback_response": eval_result.rollback_response,
            "evaluated_at": eval_result.evaluated_at,
        }
    except RollbackVerificationError as e:
        raise HTTPException(status_code=500, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Decay evaluation failed: {e}")

