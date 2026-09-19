"""
Unit tests for feature_training_router.
Tests API routes for training queue health, job listing, curation triggering, evaluation, and promotion.
Follows TDD green/red discipline.
"""

from unittest.mock import AsyncMock, patch
from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from app.routers.feature_training_router import router


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def test_get_training_health(client):
    with patch("app.services.jetson_feature_client.feature_client.get_health", new_callable=AsyncMock) as mock_health:
        mock_health.return_value = {
            "status": "ok",
            "gpu": {"available": True},
            "queue": {"training_active": 0, "training_max": 1},
        }

        resp = client.get("/features/training/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert data["queue"]["training_active"] == 0


def test_list_training_jobs(client):
    with patch("app.services.jetson_feature_client.feature_client.list_training_jobs", new_callable=AsyncMock) as mock_list:
        mock_list.return_value = [
            {"job_id": "job-1", "task": "gliner_finetune", "status": "completed"}
        ]

        resp = client.get("/features/training/jobs")
        assert resp.status_code == 200
        jobs = resp.json()
        assert len(jobs) == 1
        assert jobs[0]["job_id"] == "job-1"


def test_evaluate_and_promote_candidate(client):
    with patch("app.services.jetson_feature_client.feature_client.evaluate_candidate", new_callable=AsyncMock) as mock_eval, \
         patch("app.services.jetson_feature_client.feature_client.promote_candidate", new_callable=AsyncMock) as mock_promo, \
         patch("app.services.jetson_feature_client.feature_client.get_active_model", new_callable=AsyncMock) as mock_active:
        mock_eval.return_value = {
            "model_id": "cand-01",
            "task": "gliner_finetune",
            "sample_count": 200,
            "metrics": {"f1": 0.935, "precision": 0.94, "recall": 0.93, "latency_p99_ms": 25.0},
            "gate_ready": True,
        }
        mock_promo.return_value = {
            "status": "promoted",
            "model_id": "cand-01",
        }
        # Initial cold start: no champion yet, then post-promotion active model is cand-01
        mock_active.side_effect = [None, {"model_id": "cand-01", "task": "gliner"}]

        # Evaluate
        resp_eval = client.post("/features/training/models/cand-01/evaluate")
        assert resp_eval.status_code == 200
        assert resp_eval.json()["model_id"] == "cand-01"

        # Promote
        resp_promo = client.post("/features/training/models/cand-01/promote?task=gliner_finetune")
        assert resp_promo.status_code == 200
        assert resp_promo.json()["decision"] == "promoted"
