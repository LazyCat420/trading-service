"""
Unit tests for JetsonFeatureClient training and lifecycle methods.
Follows TDD green/red discipline, testing against mocked Jetson responses.
Zero credential leakage: dynamically generated IDs and tokens.
"""

import secrets
from unittest.mock import AsyncMock, MagicMock, patch
import pytest

from app.services.jetson_feature_client import JetsonFeatureClient, FeatureServiceResponseError


@pytest.fixture
def test_api_key():
    return f"test_key_{secrets.token_hex(8)}"


@pytest.fixture
def client(test_api_key):
    return JetsonFeatureClient(
        base_url="http://10.0.0.30:8002",
        api_key=test_api_key,
        timeout=2.0,
        max_retries=1,
    )


@pytest.mark.asyncio
async def test_submit_training_job_success(client):
    job_id = f"job-{secrets.token_hex(6)}"
    mock_resp = MagicMock()
    mock_resp.is_success = True
    mock_resp.status_code = 202
    mock_resp.json.return_value = {
        "job_id": job_id,
        "task": "gliner_finetune",
        "status": "pending",
        "base_model_id": "gliner",
        "candidate_model_id": None,
        "created_at": "2026-09-18T12:00:00Z",
        "updated_at": "2026-09-18T12:00:00Z",
        "progress_pct": 0.0,
        "metrics": {},
        "logs": [],
        "artifact_ids": [],
    }

    with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
        mock_post.return_value = mock_resp
        result = await client.submit_training_job(
            task="gliner_finetune",
            base_model_id="gliner",
            dataset_manifest_id="manifest-sha256-test",
            hyperparameters={"epochs": 3, "learning_rate": 5e-5},
            proposal_id="prop-001",
        )

        assert result["job_id"] == job_id
        assert result["status"] == "pending"
        assert result["task"] == "gliner_finetune"
        mock_post.assert_called_once()
        call_url = mock_post.call_args[0][0]
        assert call_url.endswith("/v1/training/jobs")


@pytest.mark.asyncio
async def test_list_training_jobs(client):
    mock_resp = MagicMock()
    mock_resp.is_success = True
    mock_resp.status_code = 200
    mock_resp.json.return_value = [
        {
            "job_id": "job-1",
            "task": "gliner_finetune",
            "status": "completed",
            "base_model_id": "gliner",
            "created_at": "2026-09-18T11:00:00Z",
            "updated_at": "2026-09-18T11:05:00Z",
        }
    ]

    with patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get:
        mock_get.return_value = mock_resp
        jobs = await client.list_training_jobs()
        assert len(jobs) == 1
        assert jobs[0]["job_id"] == "job-1"
        assert jobs[0]["status"] == "completed"
        call_url = mock_get.call_args[0][0]
        assert call_url.endswith("/v1/training/jobs")


@pytest.mark.asyncio
async def test_get_training_job_progress(client):
    job_id = f"job-{secrets.token_hex(6)}"
    mock_resp = MagicMock()
    mock_resp.is_success = True
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "job_id": job_id,
        "task": "gliner_finetune",
        "status": "running",
        "base_model_id": "gliner",
        "candidate_model_id": f"cand-{job_id}",
        "created_at": "2026-09-18T12:00:00Z",
        "updated_at": "2026-09-18T12:02:00Z",
        "progress_pct": 50.0,
        "metrics": {"loss": 0.12},
        "logs": ["Step 100/200"],
        "artifact_ids": [],
    }

    with patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get:
        mock_get.return_value = mock_resp
        job = await client.get_training_job(job_id)
        assert job["job_id"] == job_id
        assert job["status"] == "running"
        assert job["progress_pct"] == 50.0
        call_url = mock_get.call_args[0][0]
        assert call_url.endswith(f"/v1/training/jobs/{job_id}")


@pytest.mark.asyncio
async def test_cancel_training_job(client):
    job_id = f"job-{secrets.token_hex(6)}"
    mock_resp = MagicMock()
    mock_resp.is_success = True
    mock_resp.status_code = 200
    mock_resp.json.return_value = {"status": "cancelled", "job_id": job_id}

    with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
        mock_post.return_value = mock_resp
        result = await client.cancel_training_job(job_id)
        assert result["status"] == "cancelled"
        call_url = mock_post.call_args[0][0]
        assert call_url.endswith(f"/v1/training/jobs/{job_id}/cancel")


@pytest.mark.asyncio
async def test_evaluate_candidate(client):
    cand_id = f"cand-{secrets.token_hex(6)}"
    mock_resp = MagicMock()
    mock_resp.is_success = True
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "model_id": cand_id,
        "task": "gliner_finetune",
        "metrics": {"f1": 0.935, "precision": 0.94, "recall": 0.93},
        "gate_ready": True,
    }

    with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
        mock_post.return_value = mock_resp
        eval_result = await client.evaluate_candidate(cand_id)
        assert eval_result["model_id"] == cand_id
        assert eval_result["metrics"]["f1"] == 0.935
        assert eval_result["gate_ready"] is True
        call_url = mock_post.call_args[0][0]
        assert call_url.endswith(f"/v1/models/{cand_id}/evaluate")


@pytest.mark.asyncio
async def test_promote_candidate(client):
    cand_id = f"cand-{secrets.token_hex(6)}"
    mock_resp = MagicMock()
    mock_resp.is_success = True
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "status": "promoted",
        "model_id": cand_id,
        "task": "gliner_finetune",
        "message": "Candidate model successfully promoted to champion.",
    }

    with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
        mock_post.return_value = mock_resp
        promo = await client.promote_candidate(cand_id)
        assert promo["status"] == "promoted"
        assert promo["model_id"] == cand_id
        call_url = mock_post.call_args[0][0]
        assert call_url.endswith(f"/v1/models/{cand_id}/promote")


@pytest.mark.asyncio
async def test_rollback_model(client):
    cand_id = f"cand-{secrets.token_hex(6)}"
    mock_resp = MagicMock()
    mock_resp.is_success = True
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "status": "rolled_back",
        "active_model_id": "gliner-base",
        "message": "Restored prior champion model.",
    }

    with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
        mock_post.return_value = mock_resp
        rb = await client.rollback_model(cand_id)
        assert rb["status"] == "rolled_back"
        assert rb["active_model_id"] == "gliner-base"
        call_url = mock_post.call_args[0][0]
        assert call_url.endswith(f"/v1/models/{cand_id}/rollback")


@pytest.mark.asyncio
async def test_submit_training_job_with_idempotency_key(client):
    idemp_key = f"idemp-{secrets.token_hex(6)}"
    mock_resp = MagicMock()
    mock_resp.is_success = True
    mock_resp.status_code = 202
    mock_resp.json.return_value = {
        "job_id": "job-123",
        "task": "gliner_finetune",
        "status": "pending",
        "base_model_id": "gliner",
    }

    with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
        mock_post.return_value = mock_resp
        result = await client.submit_training_job(
            task="gliner_finetune",
            base_model_id="gliner",
            idempotency_key=idemp_key,
        )
        assert result["job_id"] == "job-123"
        call_kwargs = mock_post.call_args[1]
        assert call_kwargs["headers"].get("Idempotency-Key") == idemp_key
        assert call_kwargs["json"].get("idempotency_key") == idemp_key


@pytest.mark.asyncio
async def test_rollback_model_failure_raises(client):
    cand_id = f"cand-{secrets.token_hex(6)}"
    mock_resp = MagicMock()
    mock_resp.is_success = False
    mock_resp.status_code = 500
    mock_resp.text = "Internal rollback failure"

    with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
        mock_post.return_value = mock_resp
        with pytest.raises(FeatureServiceResponseError) as exc_info:
            await client.rollback_model(cand_id)
        assert exc_info.value.error_code == "ROLLBACK_ERROR"
        assert exc_info.value.status_code == 500


@pytest.mark.asyncio
async def test_get_model_metrics_direct(client):
    model_id = f"cnn-{secrets.token_hex(4)}"
    mock_resp = MagicMock()
    mock_resp.is_success = True
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "model_id": model_id,
        "task": "market_cnn",
        "metrics": {"macro_f1": 0.88, "brier_score": 0.08},
        "sample_count": 120,
        "evaluated_at": "2026-09-18T12:00:00Z",
    }

    with patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get:
        mock_get.return_value = mock_resp
        metrics_res = await client.get_model_metrics(model_id)
        assert metrics_res["model_id"] == model_id
        assert metrics_res["metrics"]["macro_f1"] == 0.88
        assert metrics_res["evaluated_at"] == "2026-09-18T12:00:00Z"


@pytest.mark.asyncio
async def test_get_model_metrics_fallback_to_evaluate(client):
    model_id = f"gliner-{secrets.token_hex(4)}"
    mock_get_resp = MagicMock()
    mock_get_resp.is_success = False
    mock_get_resp.status_code = 404
    mock_get_resp.text = "Not found"

    mock_eval_resp = {
        "model_id": model_id,
        "task": "gliner",
        "metrics": {"f1": 0.93, "precision": 0.94, "recall": 0.92, "latency_p99_ms": 22.0},
        "sample_count": 150,
        "evaluated_at": "2026-09-18T13:00:00Z",
    }

    with patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get, \
         patch.object(client, "evaluate_candidate", new_callable=AsyncMock) as mock_eval:
        mock_get.return_value = mock_get_resp
        mock_eval.return_value = mock_eval_resp

        metrics_res = await client.get_model_metrics(model_id)
        assert metrics_res["model_id"] == model_id
        assert metrics_res["metrics"]["f1"] == 0.93
        mock_eval.assert_awaited_once_with(model_id, timeout=None)


@pytest.mark.asyncio
async def test_get_active_models(client):
    mock_models = [
        {"model_id": "gliner-prod-v1", "task": "gliner", "status": "active"},
        {"model_id": "cnn-prod-v2", "task": "market_cnn", "status": "active"},
        {"model_id": "rnn-prod-v1", "task": "timeseries_rnn", "status": "active"},
        {"model_id": "old-cnn", "task": "market_cnn", "status": "retired"},
    ]
    with patch.object(client, "list_models", new_callable=AsyncMock) as mock_list:
        mock_list.return_value = mock_models
        active_map = await client.get_active_models()
        assert "gliner" in active_map
        assert active_map["gliner"]["model_id"] == "gliner-prod-v1"
        assert "cnn" in active_map
        assert active_map["cnn"]["model_id"] == "cnn-prod-v2"
        assert "rnn" in active_map
        assert active_map["rnn"]["model_id"] == "rnn-prod-v1"
