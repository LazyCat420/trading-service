"""
Acceptance Test 4: Attack promotion through every reachable entry point.
Exercises JetsonTrainingOrchestrator and HTTP POST /features/training/models/{candidate_id}/promote:
- Missing sample counts and insufficient samples (< 100).
- Absent candidate identity / mismatched candidate IDs.
- Missing champion evaluation when required.
- Mismatched test suites / manifest checksums between candidate and champion.
- NaN / Infinity / out-of-bounds metric values.
- Omitted critical slices or critical slice regression.
- Candidate beating fixed threshold but regressing against current champion.
- Champion changed between evaluation and promotion (expected_champion_version mismatch).
- Post-promotion verification failure: Jetson returns HTTP 200 but leaves old model active.
"""

import math
from unittest.mock import AsyncMock, MagicMock, patch
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.routers.feature_training_router import router
from app.services.jetson_training_orchestrator import (
    JetsonTrainingOrchestrator,
    PromotionDecision,
    PromotionVerificationError,
)

pytestmark = pytest.mark.real_mongo


@pytest.fixture
def orchestrator():
    client = MagicMock()
    return JetsonTrainingOrchestrator(client=client)


@pytest.fixture
def api_client():
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


# ── 1. Attack on Sample Counts ──

def test_rejects_missing_sample_count(orchestrator):
    meta = {"candidate_model_id": "cand-01", "task": "gliner_finetune"}
    eval_res = {
        "model_id": "cand-01",
        "sample_count": None,  # Missing
        "metrics": {"f1": 0.95, "precision": 0.95, "recall": 0.95, "latency_p99_ms": 25.0},
    }
    allowed, reason = orchestrator.evaluate_promotion_gate(meta, eval_res)
    assert not allowed
    assert "sample count" in reason.lower()


def test_rejects_insufficient_sample_count(orchestrator):
    meta = {"candidate_model_id": "cand-01", "task": "gliner_finetune"}
    eval_res = {
        "model_id": "cand-01",
        "sample_count": 45,  # Min is 100
        "metrics": {"f1": 0.95, "precision": 0.95, "recall": 0.95, "latency_p99_ms": 25.0},
    }
    allowed, reason = orchestrator.evaluate_promotion_gate(meta, eval_res)
    assert not allowed
    assert "sample count 45 below task floor" in reason.lower()


# ── 2. Attack on Candidate Identity ──

def test_rejects_mismatched_candidate_identity(orchestrator):
    meta = {"candidate_model_id": "cand-expected", "task": "gliner_finetune"}
    eval_res = {
        "model_id": "cand-different",  # Mismatched!
        "sample_count": 200,
        "metrics": {"f1": 0.95, "precision": 0.95, "recall": 0.95, "latency_p99_ms": 25.0},
    }
    allowed, reason = orchestrator.evaluate_promotion_gate(meta, eval_res)
    assert not allowed
    assert "candidate identity mismatch" in reason.lower()


# ── 3. Attack on Missing Champion & Manifest Mismatch ──

def test_rejects_missing_champion_when_required(orchestrator):
    meta = {"candidate_model_id": "cand-01", "task": "gliner_finetune"}
    eval_res = {
        "model_id": "cand-01",
        "sample_count": 200,
        "metrics": {"f1": 0.95, "precision": 0.95, "recall": 0.95, "latency_p99_ms": 25.0},
    }
    allowed, reason = orchestrator.evaluate_promotion_gate(
        meta, eval_res, champion_eval=None, require_champion_eval=True
    )
    assert not allowed
    assert "missing champion evaluation" in reason.lower()


def test_rejects_mismatched_evaluation_manifest(orchestrator):
    meta = {"candidate_model_id": "cand-01", "task": "gliner_finetune"}
    cand_eval = {
        "model_id": "cand-01",
        "sample_count": 200,
        "dataset_manifest_id": "manifest-candidate-v1",
        "test_sha256": "hash-abc",
        "metrics": {"f1": 0.95, "precision": 0.95, "recall": 0.95, "latency_p99_ms": 25.0},
    }
    champ_eval = {
        "model_id": "champ-v0",
        "sample_count": 200,
        "dataset_manifest_id": "manifest-champ-DIFFERENT",
        "test_sha256": "hash-xyz",
        "metrics": {"f1": 0.92, "precision": 0.92, "recall": 0.92, "latency_p99_ms": 25.0},
    }
    allowed, reason = orchestrator.evaluate_promotion_gate(
        meta, cand_eval, champion_eval=champ_eval
    )
    assert not allowed
    assert "mismatched dataset manifests" in reason.lower()


# ── 4. Attack on NaN / Infinity / Out-of-Bounds Metrics ──

@pytest.mark.parametrize("bad_val", [float("nan"), float("inf"), -float("inf"), 1.8, -0.2])
def test_rejects_invalid_numeric_metrics(orchestrator, bad_val):
    meta = {"candidate_model_id": "cand-01", "task": "gliner_finetune"}
    eval_res = {
        "model_id": "cand-01",
        "sample_count": 200,
        "metrics": {"f1": bad_val, "precision": 0.95, "recall": 0.95, "latency_p99_ms": 25.0},
    }
    allowed, reason = orchestrator.evaluate_promotion_gate(meta, eval_res)
    assert not allowed
    assert ("non-finite" in reason.lower() or "out of valid range" in reason.lower())


# ── 5. Attack on Critical Slices & Champion Regression ──

def test_rejects_candidate_that_beats_fixed_threshold_but_loses_to_champion(orchestrator):
    # Fixed threshold for F1 is 0.912. Candidate achieves 0.92 (beats 0.912).
    # But champion is 0.95. Candidate MUST be rejected due to regression!
    meta = {"candidate_model_id": "cand-01", "task": "gliner_finetune"}
    cand_eval = {
        "model_id": "cand-01",
        "sample_count": 200,
        "dataset_manifest_id": "m-shared",
        "test_sha256": "hash-123",
        "metrics": {"f1": 0.92, "precision": 0.92, "recall": 0.90, "latency_p99_ms": 25.0},
    }
    champ_eval = {
        "model_id": "champ-v1",
        "sample_count": 200,
        "dataset_manifest_id": "m-shared",
        "test_sha256": "hash-123",
        "metrics": {"f1": 0.95, "precision": 0.95, "recall": 0.93, "latency_p99_ms": 25.0},
    }
    allowed, reason = orchestrator.evaluate_promotion_gate(
        meta, cand_eval, champion_eval=champ_eval
    )
    assert not allowed
    assert "regresses against champion f1" in reason.lower()


def test_rejects_critical_slice_regression(orchestrator):
    meta = {"candidate_model_id": "cand-01", "task": "gliner_finetune"}
    cand_eval = {
        "model_id": "cand-01",
        "sample_count": 200,
        "dataset_manifest_id": "m-shared",
        "test_sha256": "hash-123",
        "metrics": {"f1": 0.95, "precision": 0.95, "recall": 0.95, "latency_p99_ms": 25.0},
        "slices": {
            "earnings_reports": {"f1": 0.82}  # Champ is 0.90 -> regression > 2%
        }
    }
    champ_eval = {
        "model_id": "champ-v1",
        "sample_count": 200,
        "dataset_manifest_id": "m-shared",
        "test_sha256": "hash-123",
        "metrics": {"f1": 0.93, "precision": 0.93, "recall": 0.93, "latency_p99_ms": 25.0},
        "slices": {
            "earnings_reports": {"f1": 0.90}
        }
    }
    allowed, reason = orchestrator.evaluate_promotion_gate(
        meta, cand_eval, champion_eval=champ_eval
    )
    assert not allowed
    assert "regressed on critical slice 'earnings_reports'" in reason.lower()


def test_rejects_changed_champion_version(orchestrator):
    meta = {"candidate_model_id": "cand-01", "task": "gliner_finetune"}
    cand_eval = {
        "model_id": "cand-01",
        "sample_count": 200,
        "dataset_manifest_id": "m-shared",
        "test_sha256": "hash-123",
        "metrics": {"f1": 0.95, "precision": 0.95, "recall": 0.95, "latency_p99_ms": 25.0},
    }
    champ_eval = {
        "model_id": "champ-v2-concurrently-updated",
        "sample_count": 200,
        "dataset_manifest_id": "m-shared",
        "test_sha256": "hash-123",
        "metrics": {"f1": 0.90, "precision": 0.90, "recall": 0.90, "latency_p99_ms": 25.0},
    }
    # We evaluated against champ-v1, but the current champion in system is champ-v2
    allowed, reason = orchestrator.evaluate_promotion_gate(
        meta,
        cand_eval,
        champion_eval=champ_eval,
        expected_champion_version="champ-v1",
    )
    assert not allowed
    assert "champion version mismatch" in reason.lower()


# ── 6. Attack on HTTP Route & Post-Promotion Verification ──

@pytest.mark.asyncio
async def test_http_route_rejects_stale_active_model_post_promotion(api_client):
    """
    Simulates Jetson promote endpoint returning HTTP 200, but get_active_model
    still reports the old model. The system MUST raise PromotionVerificationError.
    """
    mock_client = MagicMock()
    async def _mock_eval(mid):
        if mid == "cand-01":
            return {
                "model_id": "cand-01",
                "dataset_manifest_id": "m-shared",
                "sample_count": 200,
                "metrics": {"f1": 0.95, "precision": 0.95, "recall": 0.95, "latency_p99_ms": 25.0},
            }
        return {
            "model_id": "old-stale-champion",
            "dataset_manifest_id": "m-shared",
            "sample_count": 200,
            "metrics": {"f1": 0.92, "precision": 0.92, "recall": 0.92, "latency_p99_ms": 25.0},
        }

    mock_client.evaluate_candidate = AsyncMock(side_effect=_mock_eval)
    mock_client.promote_candidate = AsyncMock(return_value={"status": "promoted"})
    # Jetson lies: returns status 'promoted', but active model remains the old one!
    mock_client.get_active_model = AsyncMock(return_value={"model_id": "old-stale-champion", "task": "gliner"})

    with patch("app.routers.feature_training_router.feature_client", mock_client):
        resp = api_client.post(
            "/features/training/models/cand-01/promote",
            json={
                "task": "gliner_finetune",
                "dataset_manifest_id": "m-shared",
                "candidate_metrics": {"f1": 0.95, "precision": 0.95, "recall": 0.95, "latency_p99_ms": 25.0},
                "sample_count": 200,
            }
        )
        assert resp.status_code == 500
        assert "promotion verification failed" in resp.json()["detail"].lower()


@pytest.mark.asyncio
async def test_http_route_rejects_when_champion_discovery_fails(api_client):
    """Verifies that champion discovery outage fails closed with HTTP 502."""
    mock_client = MagicMock()
    mock_client.evaluate_candidate = AsyncMock(return_value={
        "model_id": "cand-01",
        "task": "gliner_finetune",
        "dataset_manifest_id": "m-shared",
        "sample_count": 200,
        "metrics": {"f1": 0.95, "precision": 0.95, "recall": 0.95, "latency_p99_ms": 25.0},
    })
    mock_client.get_active_model = AsyncMock(side_effect=RuntimeError("Jetson registry unreachable"))

    with patch("app.routers.feature_training_router.feature_client", mock_client):
        resp = api_client.post(
            "/features/training/models/cand-01/promote",
            json={"task": "gliner_finetune", "dataset_manifest_id": "m-shared"}
        )
        assert resp.status_code == 502
        assert "champion discovery failed" in resp.json()["detail"].lower()


@pytest.mark.asyncio
async def test_http_route_rejects_when_champion_evaluation_fails(api_client):
    """Verifies that failure evaluating the existing champion fails closed with HTTP 502."""
    mock_client = MagicMock()

    async def _mock_eval(mid):
        if mid == "cand-01":
            return {
                "model_id": "cand-01",
                "task": "gliner_finetune",
                "dataset_manifest_id": "m-shared",
                "sample_count": 200,
                "metrics": {"f1": 0.95, "precision": 0.95, "recall": 0.95, "latency_p99_ms": 25.0},
            }
        raise RuntimeError("Champion holdout evaluation crashed on GPU")

    mock_client.evaluate_candidate = AsyncMock(side_effect=_mock_eval)
    mock_client.get_active_model = AsyncMock(return_value={"model_id": "champ-v1", "task": "gliner"})

    with patch("app.routers.feature_training_router.feature_client", mock_client):
        resp = api_client.post(
            "/features/training/models/cand-01/promote",
            json={"task": "gliner_finetune", "dataset_manifest_id": "m-shared"}
        )
        assert resp.status_code == 502
        assert "champion evaluation failed" in resp.json()["detail"].lower()


@pytest.mark.asyncio
async def test_http_route_rejects_concurrent_promotion_cas_race(api_client):
    """
    Simulates a race condition where the active champion changes between
    candidate evaluation and the promote call.
    Must be rejected with HTTP 409 Conflict.
    """
    mock_client = MagicMock()

    async def _mock_eval(mid):
        if mid == "cand-01":
            return {
                "model_id": "cand-01",
                "task": "gliner_finetune",
                "dataset_manifest_id": "m-shared",
                "sample_count": 200,
                "metrics": {"f1": 0.95, "precision": 0.95, "recall": 0.95, "latency_p99_ms": 25.0},
            }
        return {
            "model_id": "champ-v1",
            "task": "gliner_finetune",
            "dataset_manifest_id": "m-shared",
            "sample_count": 200,
            "metrics": {"f1": 0.92, "precision": 0.92, "recall": 0.92, "latency_p99_ms": 25.0},
        }

    mock_client.evaluate_candidate = AsyncMock(side_effect=_mock_eval)
    # First get_active_model returns champ-v1; second call (CAS check before promote) returns champ-v2!
    mock_client.get_active_model = AsyncMock(side_effect=[
        {"model_id": "champ-v1", "task": "gliner"},
        {"model_id": "champ-v2-concurrently-promoted", "task": "gliner"},
    ])

    with patch("app.routers.feature_training_router.feature_client", mock_client):
        resp = api_client.post(
            "/features/training/models/cand-01/promote",
            json={"task": "gliner_finetune", "dataset_manifest_id": "m-shared"}
        )
        assert resp.status_code == 409
        detail = resp.json()["detail"]
        assert "concurrent promotion detected" in detail["reason"].lower()


def test_rejects_champion_with_incomplete_or_nan_metrics(orchestrator):
    """Verifies that champion evaluation missing metrics or containing NaN is rejected."""
    meta = {"candidate_model_id": "cand-01", "task": "gliner_finetune"}
    cand_eval = {
        "model_id": "cand-01",
        "sample_count": 200,
        "dataset_manifest_id": "m-shared",
        "metrics": {"f1": 0.95, "precision": 0.95, "recall": 0.95, "latency_p99_ms": 25.0},
    }
    champ_eval_bad = {
        "model_id": "champ-v1",
        "sample_count": 200,
        "dataset_manifest_id": "m-shared",
        "metrics": {"f1": float("nan"), "precision": 0.92, "recall": 0.92, "latency_p99_ms": 25.0},
    }
    allowed, reason = orchestrator.evaluate_promotion_gate(
        meta, cand_eval, champion_eval=champ_eval_bad, require_champion_eval=True
    )
    assert not allowed
    assert "non-finite" in reason.lower()
