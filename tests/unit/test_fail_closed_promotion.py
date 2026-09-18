"""
Unit tests for Fail-Closed Promotion Gates & Anti-Regression Guardrails.
Verifies Items 2 & 3:
- Rejection of missing metrics, NaN, infinity, invalid types.
- Rejection of unknown tasks and sample count under floor.
- Rejection of mismatched candidate and evaluation identities.
- Verification of candidate task type extracted from candidate metadata.
- Paired champion non-regression requirement on identical dataset splits.
- Critical slice regression tolerance.
- Optimistic locking against concurrent champion updates.
- Post-promotion active model verification.

Follows TDD green/red discipline with zero static credentials.
"""

import math
from unittest.mock import AsyncMock, MagicMock
import uuid
import pytest

from app.services.jetson_training_orchestrator import (
    JetsonTrainingOrchestrator,
    PromotionDecision,
    PromotionVerificationError,
)


@pytest.fixture
def mock_client():
    client = MagicMock()
    client.get_health = AsyncMock()
    client.submit_training_job = AsyncMock()
    client.get_training_job = AsyncMock()
    client.evaluate_candidate = AsyncMock()
    client.promote_candidate = AsyncMock()
    client.get_active_model = AsyncMock()
    return client


@pytest.fixture
def orchestrator(mock_client):
    return JetsonTrainingOrchestrator(client=mock_client)


def test_rejects_missing_required_metric(orchestrator):
    cand_metadata = {
        "candidate_model_id": "cand-" + uuid.uuid4().hex[:8],
        "task": "gliner_finetune",
    }
    # Missing precision and latency_p99_ms
    cand_eval = {
        "model_id": cand_metadata["candidate_model_id"],
        "sample_count": 200,
        "metrics": {"f1": 0.94, "recall": 0.92},
    }

    passed, reason = orchestrator.evaluate_promotion_gate(cand_metadata, cand_eval)
    assert not passed
    assert "missing" in reason.lower() or "precision" in reason.lower()


def test_rejects_nan_and_inf_metrics(orchestrator):
    cand_metadata = {
        "candidate_model_id": "cand-" + uuid.uuid4().hex[:8],
        "task": "gliner_finetune",
    }
    nan_eval = {
        "model_id": cand_metadata["candidate_model_id"],
        "sample_count": 200,
        "metrics": {
            "f1": float("nan"),
            "precision": 0.94,
            "recall": 0.92,
            "latency_p99_ms": 20.0,
        },
    }

    passed, reason = orchestrator.evaluate_promotion_gate(cand_metadata, nan_eval)
    assert not passed
    assert "nan" in reason.lower() or "non-finite" in reason.lower()

    inf_eval = {
        "model_id": cand_metadata["candidate_model_id"],
        "sample_count": 200,
        "metrics": {
            "f1": 0.95,
            "precision": float("inf"),
            "recall": 0.92,
            "latency_p99_ms": 20.0,
        },
    }
    passed_inf, reason_inf = orchestrator.evaluate_promotion_gate(cand_metadata, inf_eval)
    assert not passed_inf
    assert "inf" in reason_inf.lower() or "non-finite" in reason_inf.lower()


def test_rejects_invalid_metric_types(orchestrator):
    cand_metadata = {
        "candidate_model_id": "cand-" + uuid.uuid4().hex[:8],
        "task": "cnn_regime",
    }
    string_eval = {
        "model_id": cand_metadata["candidate_model_id"],
        "sample_count": 150,
        "metrics": {
            "macro_f1": "0.91",  # string instead of float
            "brier_score": 0.08,
        },
    }

    passed, reason = orchestrator.evaluate_promotion_gate(cand_metadata, string_eval)
    assert not passed
    assert "type" in reason.lower() or "invalid" in reason.lower()


def test_rejects_insufficient_samples(orchestrator):
    cand_metadata = {
        "candidate_model_id": "cand-" + uuid.uuid4().hex[:8],
        "task": "gliner_finetune",
    }
    under_floor_eval = {
        "model_id": cand_metadata["candidate_model_id"],
        "sample_count": 45,  # Floor is 100
        "metrics": {
            "f1": 0.96,
            "precision": 0.95,
            "recall": 0.95,
            "latency_p99_ms": 22.0,
        },
    }

    passed, reason = orchestrator.evaluate_promotion_gate(cand_metadata, under_floor_eval)
    assert not passed
    assert "sample" in reason.lower() or "floor" in reason.lower()


def test_rejects_mismatched_candidate_and_eval_identity(orchestrator):
    cand_metadata = {
        "candidate_model_id": "cand-expected-" + uuid.uuid4().hex[:8],
        "task": "gliner_finetune",
    }
    mismatched_eval = {
        "model_id": "cand-different-" + uuid.uuid4().hex[:8],
        "sample_count": 150,
        "metrics": {
            "f1": 0.95,
            "precision": 0.95,
            "recall": 0.95,
            "latency_p99_ms": 22.0,
        },
    }

    passed, reason = orchestrator.evaluate_promotion_gate(cand_metadata, mismatched_eval)
    assert not passed
    assert "mismatch" in reason.lower() or "identity" in reason.lower()


def test_rejects_unknown_task_metadata(orchestrator):
    cand_metadata = {
        "candidate_model_id": "cand-" + uuid.uuid4().hex[:8],
        "task": "unknown_future_task",
    }
    eval_resp = {
        "model_id": cand_metadata["candidate_model_id"],
        "sample_count": 150,
        "metrics": {"custom_acc": 0.99},
    }

    passed, reason = orchestrator.evaluate_promotion_gate(cand_metadata, eval_resp)
    assert not passed
    assert "unknown task" in reason.lower()


def test_requires_same_dataset_split_for_champion_comparison(orchestrator):
    cand_id = "cand-" + uuid.uuid4().hex[:8]
    champ_id = "champ-" + uuid.uuid4().hex[:8]
    cand_metadata = {"candidate_model_id": cand_id, "task": "gliner_finetune"}

    cand_eval = {
        "model_id": cand_id,
        "dataset_manifest_id": "manifest-v2-test",
        "sample_count": 200,
        "metrics": {"f1": 0.93, "precision": 0.93, "recall": 0.93, "latency_p99_ms": 25.0},
    }
    champ_eval = {
        "model_id": champ_id,
        "dataset_manifest_id": "manifest-v1-test",  # Different dataset!
        "sample_count": 200,
        "metrics": {"f1": 0.92, "precision": 0.92, "recall": 0.92, "latency_p99_ms": 26.0},
    }

    passed, reason = orchestrator.evaluate_promotion_gate(
        cand_metadata, cand_eval, champion_eval=champ_eval
    )
    assert not passed
    assert "manifest" in reason.lower() or "dataset" in reason.lower()


def test_rejects_candidate_that_regresses_against_champion(orchestrator):
    cand_id = "cand-" + uuid.uuid4().hex[:8]
    champ_id = "champ-" + uuid.uuid4().hex[:8]
    manifest_id = "manifest-frozen-" + uuid.uuid4().hex[:8]
    cand_metadata = {"candidate_model_id": cand_id, "task": "cnn_regime"}

    # Candidate has macro_f1 = 0.88, which is above absolute floor (0.865)
    # But Champion has macro_f1 = 0.91! Candidate regresses!
    cand_eval = {
        "model_id": cand_id,
        "dataset_manifest_id": manifest_id,
        "sample_count": 250,
        "metrics": {"macro_f1": 0.88, "brier_score": 0.080},
    }
    champ_eval = {
        "model_id": champ_id,
        "dataset_manifest_id": manifest_id,
        "sample_count": 250,
        "metrics": {"macro_f1": 0.91, "brier_score": 0.075},
    }

    passed, reason = orchestrator.evaluate_promotion_gate(
        cand_metadata, cand_eval, champion_eval=champ_eval
    )
    assert not passed
    assert "regress" in reason.lower() or "champion" in reason.lower()


def test_rejects_critical_slice_regression(orchestrator):
    cand_id = "cand-" + uuid.uuid4().hex[:8]
    champ_id = "champ-" + uuid.uuid4().hex[:8]
    manifest_id = "manifest-frozen-" + uuid.uuid4().hex[:8]
    cand_metadata = {"candidate_model_id": cand_id, "task": "gliner_finetune"}

    # Overall candidate F1 is higher (0.94 vs 0.92)
    # But on high_volatility slice, candidate regresses by 8% (0.84 vs 0.92, tolerance is 2%)
    cand_eval = {
        "model_id": cand_id,
        "dataset_manifest_id": manifest_id,
        "sample_count": 300,
        "metrics": {"f1": 0.94, "precision": 0.94, "recall": 0.94, "latency_p99_ms": 25.0},
        "slices": {
            "high_volatility": {"f1": 0.84},
            "standard": {"f1": 0.96},
        },
    }
    champ_eval = {
        "model_id": champ_id,
        "dataset_manifest_id": manifest_id,
        "sample_count": 300,
        "metrics": {"f1": 0.92, "precision": 0.92, "recall": 0.92, "latency_p99_ms": 26.0},
        "slices": {
            "high_volatility": {"f1": 0.92},
            "standard": {"f1": 0.92},
        },
    }

    passed, reason = orchestrator.evaluate_promotion_gate(
        cand_metadata, cand_eval, champion_eval=champ_eval, slice_regression_tolerance=0.02
    )
    assert not passed
    assert "slice" in reason.lower() or "high_volatility" in reason.lower()


def test_optimistic_lock_rejects_outdated_champion_version(orchestrator):
    cand_id = "cand-" + uuid.uuid4().hex[:8]
    cand_metadata = {"candidate_model_id": cand_id, "task": "timeseries_rnn"}
    manifest_id = "manifest-frozen-" + uuid.uuid4().hex[:8]

    cand_eval = {
        "model_id": cand_id,
        "dataset_manifest_id": manifest_id,
        "sample_count": 600,
        "metrics": {"rmse": 0.019, "coverage_80": 0.80},
    }
    champ_eval = {
        "model_id": "champ-version-B",
        "dataset_manifest_id": manifest_id,
        "sample_count": 600,
        "metrics": {"rmse": 0.021, "coverage_80": 0.79},
    }

    # Job was initiated expecting champ-version-A, but champ-version-B was promoted in the meantime
    passed, reason = orchestrator.evaluate_promotion_gate(
        cand_metadata,
        cand_eval,
        champion_eval=champ_eval,
        expected_champion_version="champ-version-A",
    )
    assert not passed
    assert "optimistic lock" in reason.lower() or "champion version mismatch" in reason.lower()


@pytest.mark.asyncio
async def test_post_promotion_verifies_active_model(orchestrator, mock_client):
    cand_id = "cand-verified-" + uuid.uuid4().hex[:8]

    mock_client.get_health.return_value = {
        "status": "ok",
        "queue": {"training_active": 0, "training_max": 1},
    }
    mock_client.submit_training_job.return_value = {
        "job_id": "job-verif",
        "status": "pending",
    }
    mock_client.get_training_job.return_value = {
        "job_id": "job-verif",
        "status": "completed",
        "candidate_model_id": cand_id,
    }
    mock_client.evaluate_candidate.return_value = {
        "model_id": cand_id,
        "dataset_manifest_id": "man-1",
        "sample_count": 250,
        "metrics": {"f1": 0.94, "precision": 0.94, "recall": 0.94, "latency_p99_ms": 25.0},
    }
    mock_client.promote_candidate.return_value = {"status": "promoted", "model_id": cand_id}

    # If get_active_model returns the wrong model, promotion verification must fail closed
    mock_client.get_active_model.return_value = "old-champion"

    with pytest.raises(PromotionVerificationError):
        await orchestrator.submit_and_orchestrate(
            task="gliner_finetune",
            base_model_id="gliner",
            dataset_manifest_id="man-1",
            poll_interval_s=0.01,
            max_poll_seconds=1.0,
            verify_active_after_promotion=True,
        )
