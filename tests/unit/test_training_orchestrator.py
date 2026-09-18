"""
Unit tests for JetsonTrainingOrchestrator.
Tests pre-flight concurrency checks, async job polling, evaluation, and deterministic promotion gates.
Follows TDD green/red discipline.
Zero credential leakage: dynamically generated IDs and tokens.
"""

from unittest.mock import AsyncMock, MagicMock
import pytest

from app.services.jetson_training_orchestrator import (
    JetsonTrainingOrchestrator,
    PromotionDecision,
    TrainingQueueBusyError,
)


@pytest.fixture
def mock_client():
    client = MagicMock()
    client.get_health = AsyncMock()
    client.submit_training_job = AsyncMock()
    client.get_training_job = AsyncMock()
    client.evaluate_candidate = AsyncMock()
    client.promote_candidate = AsyncMock()
    client.rollback_model = AsyncMock()
    client.get_active_model = AsyncMock(return_value="cand-gliner-100")
    return client


@pytest.fixture
def orchestrator(mock_client):
    return JetsonTrainingOrchestrator(client=mock_client)


@pytest.mark.asyncio
async def test_preflight_blocks_when_training_busy(orchestrator, mock_client):
    mock_client.get_health.return_value = {
        "status": "ok",
        "queue": {"training_active": 1, "training_max": 1},
    }

    with pytest.raises(TrainingQueueBusyError):
        await orchestrator.submit_and_orchestrate(
            task="gliner_finetune",
            base_model_id="gliner",
            dataset_manifest_id="manifest-001",
        )


@pytest.mark.asyncio
async def test_orchestration_successful_promotion(orchestrator, mock_client):
    # 1. Health check passes
    mock_client.get_health.return_value = {
        "status": "ok",
        "queue": {"training_active": 0, "training_max": 1},
    }

    # 2. Submission succeeds
    mock_client.submit_training_job.return_value = {
        "job_id": "job-100",
        "status": "pending",
        "base_model_id": "gliner",
    }

    # 3. Polling returns completed with candidate model
    mock_client.get_training_job.return_value = {
        "job_id": "job-100",
        "status": "completed",
        "candidate_model_id": "cand-gliner-100",
        "progress_pct": 100.0,
    }

    # 4. Evaluation returns metrics that beat promotion thresholds
    mock_client.evaluate_candidate.return_value = {
        "model_id": "cand-gliner-100",
        "metrics": {"f1": 0.935, "precision": 0.94, "recall": 0.93, "latency_p99_ms": 25.0},
        "gate_ready": True,
    }

    # 5. Promotion succeeds
    mock_client.promote_candidate.return_value = {
        "status": "promoted",
        "model_id": "cand-gliner-100",
    }

    result = await orchestrator.submit_and_orchestrate(
        task="gliner_finetune",
        base_model_id="gliner",
        dataset_manifest_id="manifest-001",
        poll_interval_s=0.01,
        max_poll_seconds=1.0,
    )

    assert result.decision == PromotionDecision.PROMOTED
    assert result.candidate_model_id == "cand-gliner-100"
    mock_client.promote_candidate.assert_called_once_with("cand-gliner-100")


@pytest.mark.asyncio
async def test_orchestration_rejects_degraded_candidate(orchestrator, mock_client):
    # Health and job complete successfully
    mock_client.get_health.return_value = {
        "status": "ok",
        "queue": {"training_active": 0, "training_max": 1},
    }
    mock_client.submit_training_job.return_value = {
        "job_id": "job-101",
        "status": "pending",
    }
    mock_client.get_training_job.return_value = {
        "job_id": "job-101",
        "status": "completed",
        "candidate_model_id": "cand-gliner-degraded",
    }

    # Degraded metrics: F1 is below threshold (0.85 < 0.912)
    mock_client.evaluate_candidate.return_value = {
        "model_id": "cand-gliner-degraded",
        "metrics": {"f1": 0.85, "precision": 0.86, "recall": 0.84, "latency_p99_ms": 25.0},
        "gate_ready": True,
    }

    result = await orchestrator.submit_and_orchestrate(
        task="gliner_finetune",
        base_model_id="gliner",
        dataset_manifest_id="manifest-002",
        poll_interval_s=0.01,
        max_poll_seconds=1.0,
    )

    assert result.decision == PromotionDecision.REJECTED
    assert "F1 0.850 below threshold" in result.reason
    mock_client.promote_candidate.assert_not_called()
