"""
Lifecycle Durability and Crash Recovery Test Suite (Dev 1 Scope).

Validates:
1. Background worker fetches trusted champion evidence and rejects replacement promotion when unavailable (Work Order Item 1).
2. Server-enforced expected-champion promotion contracts (Work Order Item 2).
3. Dataset registration and manifest/split checksum acknowledgement (Work Order Item 3).
4. Stable remote submission idempotency key across worker restarts (Work Order Item 4).
5. Rollback verification of specific expected champion and UNRESOLVED persistence (Work Order Item 5).
6. Concurrent submissions and process crash simulation during submission, training, evaluation, and promotion (Work Order Item 6).
7. Cancellation and stale-worker fencing (Work Order Item 7).
"""

from __future__ import annotations

import asyncio
import datetime
import math
import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.durable_training_service import (
    DurableTrainingService,
    JobStatus,
    TrainingJobRecord,
)
from app.services.jetson_feature_client import JetsonFeatureClient
from app.services.jetson_training_orchestrator import (
    JetsonTrainingOrchestrator,
    PromotionDecision,
    PromotionVerificationError,
)
from app.services.decay_monitor_service import (
    DecayMonitorService,
    RollbackVerificationError,
    RollbackTriggerReason,
)
from app.services.dataset_manifest_builder import DatasetManifestBuilder


class InMemoryCollection:
    """Mock MongoDB collection supporting atomic lease and index behaviors for testing."""

    def __init__(self):
        self.docs: dict[str, dict] = {}
        self.indexes: dict[str, dict] = {}

    def create_index(self, keys, **kwargs):
        name = kwargs.get("name", str(keys))
        self.indexes[name] = {"keys": keys, "kwargs": kwargs}
        return name

    def _matches(self, doc: dict, query: dict) -> bool:
        for k, v in query.items():
            if k == "$or":
                if not any(self._matches(doc, q) for q in v):
                    return False
            elif isinstance(v, dict):
                doc_val = doc.get(k)
                if "$in" in v and doc_val not in v["$in"]:
                    return False
                if "$ne" in v and doc_val == v["$ne"]:
                    return False
                if "$lt" in v:
                    if doc_val is None or str(doc_val) >= str(v["$lt"]):
                        return False
                if "$gte" in v:
                    if doc_val is None or str(doc_val) < str(v["$gte"]):
                        return False
            elif doc.get(k) != v:
                return False
        return True

    def find_one(self, query, sort=None):
        matched = [dict(d) for d in self.docs.values() if self._matches(d, query)]
        if not matched:
            return None
        if sort:
            key, direction = sort[0]
            matched.sort(key=lambda x: x.get(key, ""), reverse=(direction < 0))
        return matched[0]

    def find(self, query=None):
        if query is None:
            return [dict(d) for d in self.docs.values()]
        return [dict(d) for d in self.docs.values() if self._matches(d, query)]

    def count_documents(self, query):
        return len(self.find(query))

    def insert_one(self, doc):
        job_id = doc.get("job_id", str(uuid.uuid4()))
        self.docs[job_id] = dict(doc)
        return MagicMock(inserted_id=job_id)

    def update_one(self, query, update):
        for doc_id, doc in self.docs.items():
            if self._matches(doc, query):
                if "$set" in update:
                    doc.update(update["$set"])
                return MagicMock(modified_count=1)
        return MagicMock(modified_count=0)

    def find_one_and_update(self, query, update, sort=None):
        matched = [doc_id for doc_id, d in self.docs.items() if self._matches(d, query)]
        if not matched:
            return None
        if sort:
            key, direction = sort[0]
            matched.sort(key=lambda did: self.docs[did].get(key, ""), reverse=(direction < 0))
        target_id = matched[0]
        target = self.docs[target_id]
        if "$set" in update:
            target.update(update["$set"])
        return dict(target)


@pytest.fixture
def mock_db():
    col = InMemoryCollection()
    db = MagicMock()
    db.get_collection.return_value = col
    db.__getitem__.return_value = col
    return db


@pytest.fixture
def mock_client():
    client = MagicMock()
    active_models: dict[str, Any] = {}

    async def _get_active(task):
        return active_models.get(task)

    async def _promote(cand_id, expected_champion_version=None, **kwargs):
        active_models["gliner_finetune"] = {"model_id": cand_id, "task": "gliner_finetune"}
        active_models["market_cnn"] = {"model_id": cand_id, "task": "market_cnn"}
        active_models["timeseries_rnn"] = {"model_id": cand_id, "task": "timeseries_rnn"}
        return {"status": "promoted"}

    client.get_health = AsyncMock(return_value={"status": "ok", "queue": {"training_active": 0, "training_max": 1}})
    client.submit_training_job = AsyncMock(return_value={"job_id": "jetson-job-001", "status": "pending"})
    client.get_training_job = AsyncMock(return_value={"status": "completed", "candidate_model_id": "cand-001"})
    client.evaluate_candidate = AsyncMock(return_value={
        "model_id": "cand-001",
        "sample_count": 200,
        "dataset_manifest_id": "manifest-test-01",
        "metrics": {"f1": 0.95, "precision": 0.95, "recall": 0.95, "latency_p99_ms": 20.0},
    })
    client.promote_candidate = AsyncMock(side_effect=_promote)
    client.get_active_model = AsyncMock(side_effect=_get_active)
    client.get_model_metrics = AsyncMock(return_value=None)
    client.rollback_model = AsyncMock(return_value={"status": "rolled_back"})
    client.cancel_training_job = AsyncMock(return_value={"status": "cancelled"})
    return client


# =========================================================================
# Work Order Item 1: Background Worker Champion Evidence & Fail-Closed Gate
# =========================================================================

@pytest.mark.asyncio
async def test_worker_rejects_replacement_when_champion_evidence_unavailable(mock_db, mock_client):
    """Background worker must reject replacement promotion if active champion evidence is unavailable."""
    service = DurableTrainingService(db=mock_db, client=mock_client, worker_id="worker-1")

    # Mock an existing active champion model
    mock_client.get_active_model.side_effect = None
    mock_client.get_active_model.return_value = {"model_id": "champ-active-v1", "task": "gliner_finetune"}
    # Metric retrieval fails or returns empty
    mock_client.get_model_metrics.side_effect = Exception("Jetson metric store unavailable")

    job = await service.submit_job(
        task="gliner_finetune",
        base_model_id="gliner",
        dataset_manifest_id="manifest-test-01",
    )
    await service.try_admit_next_job()

    result = await service.process_admitted_job(job.job_id)
    assert result.status == JobStatus.REJECTED
    assert "Trusted champion evidence unavailable" in result.result["reason"]
    # Verify candidate was NOT promoted
    mock_client.promote_candidate.assert_not_called()


@pytest.mark.asyncio
async def test_worker_enforces_anti_regression_against_fetched_champion(mock_db, mock_client):
    """Background worker fetches champion metrics and rejects candidate that regresses against champion."""
    service = DurableTrainingService(db=mock_db, client=mock_client, worker_id="worker-1")

    mock_client.get_active_model.side_effect = None
    mock_client.get_active_model.return_value = {"model_id": "champ-active-v1", "task": "gliner_finetune"}
    mock_client.get_model_metrics.return_value = {
        "model_id": "champ-active-v1",
        "sample_count": 200,
        "dataset_manifest_id": "manifest-test-01",
        "metrics": {"f1": 0.96, "precision": 0.96, "recall": 0.96, "latency_p99_ms": 20.0},
    }
    # Candidate achieves F1=0.94 (below champion F1=0.96)
    mock_client.evaluate_candidate.return_value = {
        "model_id": "cand-001",
        "sample_count": 200,
        "dataset_manifest_id": "manifest-test-01",
        "metrics": {"f1": 0.94, "precision": 0.94, "recall": 0.94, "latency_p99_ms": 20.0},
    }

    job = await service.submit_job(
        task="gliner_finetune",
        base_model_id="gliner",
        dataset_manifest_id="manifest-test-01",
    )
    await service.try_admit_next_job()

    result = await service.process_admitted_job(job.job_id)
    assert result.status == JobStatus.REJECTED
    assert "regresses against champion" in result.result["reason"]
    mock_client.promote_candidate.assert_not_called()


# =========================================================================
# Work Order Item 2: Expected-Champion Promotion Contracts & CAS
# =========================================================================

@pytest.mark.asyncio
async def test_promote_candidate_passes_expected_champion_headers(mock_client):
    """Client passes expected champion version via header and payload."""
    client = JetsonFeatureClient(base_url="http://10.0.0.30:8002")
    with patch("httpx.AsyncClient.post") as mock_post:
        mock_resp = MagicMock(status_code=200, is_success=True)
        mock_resp.json.return_value = {"status": "promoted"}
        mock_post.return_value = mock_resp

        res = await client.promote_candidate("cand-001", expected_champion_version="champ-v1")
        assert res["status"] == "promoted"

        call_kwargs = mock_post.call_args.kwargs
        assert call_kwargs["json"]["expected_champion"] == "champ-v1"
        assert call_kwargs["headers"]["X-Expected-Champion"] == "champ-v1"


# =========================================================================
# Work Order Item 3: Dataset Registration & Checksum Acknowledgement
# =========================================================================

@pytest.mark.asyncio
async def test_dataset_registration_acknowledges_split_checksums_and_gap():
    """Client register_dataset calculates splits and handles 404 capability gap gracefully."""
    client = JetsonFeatureClient(base_url="http://10.0.0.30:8002")

    manifest = {
        "manifest_id": "manifest-chk-001",
        "task": "gliner_finetune",
        "sha256": "aggregate_sha256_hash",
        "splits": {
            "train_sha256": "hash_train_123",
            "val_sha256": "hash_val_456",
            "test_sha256": "hash_test_789",
            "train_count": 80,
            "val_count": 10,
            "test_count": 10,
        },
        "created_at": "2026-09-18T23:00:00Z",
    }

    # Simulate Jetson server returning 404 on /v1/datasets
    with patch("httpx.AsyncClient.post") as mock_post:
        mock_resp = MagicMock(status_code=404, is_success=False)
        mock_post.return_value = mock_resp

        result = await client.register_dataset(manifest)
        assert result["status"] == "acknowledged_local_only"
        assert result["manifest_id"] == "manifest-chk-001"
        assert result["splits"]["train_sha256"] == "hash_train_123"
        assert result["splits"]["test_sha256"] == "hash_test_789"
        assert result["capability_gap"] == "JETSON_LACKS_DATASET_REGISTRATION_ENDPOINT"


# =========================================================================
# Work Order Item 4: Stable Remote Submission Idempotency Key
# =========================================================================

@pytest.mark.asyncio
async def test_stable_idempotency_key_forwarded_to_jetson(mock_db, mock_client):
    """Worker forwards job.idempotency_key on submission and preserves across resumption."""
    service = DurableTrainingService(db=mock_db, client=mock_client, worker_id="worker-1")

    job = await service.submit_job(
        task="gliner_finetune",
        base_model_id="gliner",
        dataset_manifest_id="manifest-idempotent-01",
        hyperparameters={"epochs": 3, "learning_rate": 5e-5},
    )
    expected_idem = job.idempotency_key
    assert len(expected_idem) == 16

    await service.try_admit_next_job()
    await service.process_admitted_job(job.job_id)

    mock_client.submit_training_job.assert_called_once()
    call_kwargs = mock_client.submit_training_job.call_args.kwargs
    assert call_kwargs["idempotency_key"] == expected_idem


# =========================================================================
# Work Order Item 5: Verified Rollback with Specific Expected Champion & UNRESOLVED Persistence
# =========================================================================

@pytest.mark.asyncio
async def test_rollback_verification_failure_persists_unresolved(mock_client, mock_db):
    """When rollback fails to restore the expected champion, incident is persisted as UNRESOLVED in DB."""
    decay_svc = DecayMonitorService(feature_client=mock_client, db=mock_db)

    # Jetson rollback succeeds on wire, but read-back returns wrong champion
    mock_client.rollback_model.return_value = {"status": "rolled_back"}
    mock_client.get_active_model.side_effect = None
    mock_client.get_active_model.return_value = {"model_id": "cand-decaying-01", "task": "gliner"}

    eval_data = {
        "task": "gliner",
        "model_id": "cand-decaying-01",
        "expected_champion": "champ-prior-v1",
        "metrics": {"f1": 0.50},  # Breaches CRITICAL_GLINER_F1_MIN (0.70)
    }

    with pytest.raises(RollbackVerificationError):
        await decay_svc.record_and_evaluate(eval_data)

    # Assert incident was persisted as UNRESOLVED in MongoDB
    col = mock_db["specialist_decay_incidents"]
    saved = col.find_one({"model_id": "cand-decaying-01"})
    assert saved is not None
    assert saved["status"] == "UNRESOLVED"
    assert saved["verification_status"] == "FAILED"
    assert saved["expected_champion"] == "champ-prior-v1"
    assert "Rollback failed verification" in saved["detail"]


@pytest.mark.asyncio
async def test_rollback_verification_success_restores_expected_champion(mock_client, mock_db):
    """When rollback successfully restores the expected champion, status is ROLLED_BACK and VERIFIED."""
    decay_svc = DecayMonitorService(feature_client=mock_client, db=mock_db)

    mock_client.rollback_model.return_value = {"status": "rolled_back"}
    mock_client.get_active_model.side_effect = None
    mock_client.get_active_model.return_value = {"model_id": "champ-prior-v1", "task": "gliner"}

    eval_data = {
        "task": "gliner",
        "model_id": "cand-decaying-01",
        "expected_champion": "champ-prior-v1",
        "metrics": {"f1": 0.50},
    }

    result = await decay_svc.record_and_evaluate(eval_data)
    assert result.triggered_rollback is True

    col = mock_db["specialist_decay_incidents"]
    saved = col.find_one({"model_id": "cand-decaying-01"})
    assert saved is not None
    assert saved["status"] == "ROLLED_BACK"
    assert saved["verification_status"] == "VERIFIED"


# =========================================================================
# Work Order Item 6: Concurrent Submissions & Process Termination Tests
# =========================================================================

@pytest.mark.asyncio
async def test_concurrent_submissions_deduplicate_to_single_active_job(mock_db, mock_client):
    """10 concurrent submissions for same task and manifest yield exactly 1 active queued job."""
    service = DurableTrainingService(db=mock_db, client=mock_client, worker_id="worker-1")

    tasks = [
        service.submit_job(
            task="market_cnn",
            base_model_id="cnn",
            dataset_manifest_id="manifest-concurrent-01",
            hyperparameters={"learning_rate": 1e-4},
        )
        for _ in range(10)
    ]
    results = await asyncio.gather(*tasks)

    # All returned job records must share the identical job_id
    job_ids = {r.job_id for r in results}
    assert len(job_ids) == 1


@pytest.mark.asyncio
async def test_process_crash_during_training_recovers_without_duplicate_remote_job(mock_db, mock_client):
    """
    Worker crashes after submitting remote job to Jetson.
    Recovery worker acquires expired lease, sees jetson_job_id, resumes polling,
    and PROVES ZERO duplicate remote jobs are submitted.
    """
    # 1. Simulate crashed worker state in DB: RUNNING with active jetson_job_id and expired lease
    now = datetime.datetime.now(datetime.timezone.utc)
    expired_lease = (now - datetime.timedelta(seconds=120)).isoformat()

    col = mock_db["training_jobs"]
    col.insert_one({
        "job_id": "job-crash-training",
        "task": "gliner_finetune",
        "base_model_id": "gliner",
        "dataset_manifest_id": "manifest-crash-train",
        "idempotency_key": "idem-crash-key",
        "status": JobStatus.RUNNING.value,
        "jetson_job_id": "jetson-already-running-007",
        "lease_owner": "crashed-worker-pid-999",
        "lease_expires_at": expired_lease,
        "created_at": (now - datetime.timedelta(minutes=5)).isoformat(),
        "updated_at": (now - datetime.timedelta(minutes=5)).isoformat(),
    })

    # 2. Recovery worker starts up
    recovering_worker = DurableTrainingService(db=mock_db, client=mock_client, worker_id="recovering-worker-pid-1001")

    # Reconcile stale leases
    reclaimed = await recovering_worker.reconcile_stale_leases()
    assert reclaimed == 1

    # Recovery worker executes the admitted job
    res = await recovering_worker.process_admitted_job("job-crash-training")
    assert res.status == JobStatus.PROMOTED
    assert res.jetson_job_id == "jetson-already-running-007"

    # CRITICAL PROOF: Zero duplicate submit_training_job calls!
    mock_client.submit_training_job.assert_not_called()
    mock_client.get_training_job.assert_called_with("jetson-already-running-007")


@pytest.mark.asyncio
async def test_process_crash_during_evaluation_skips_training_resumes_eval(mock_db, mock_client):
    """
    Worker crashes during holdout evaluation (candidate_model_id already populated).
    Recovery worker skips training and polling, directly evaluating candidate.
    """
    now = datetime.datetime.now(datetime.timezone.utc)
    expired_lease = (now - datetime.timedelta(seconds=90)).isoformat()

    col = mock_db["training_jobs"]
    col.insert_one({
        "job_id": "job-crash-eval",
        "task": "market_cnn",
        "base_model_id": "cnn",
        "dataset_manifest_id": "manifest-crash-eval",
        "idempotency_key": "idem-eval-crash",
        "status": JobStatus.EVALUATING.value,
        "jetson_job_id": "jetson-finished-job",
        "candidate_model_id": "cand-already-trained-009",
        "lease_owner": "dead-eval-worker",
        "lease_expires_at": expired_lease,
    })

    worker = DurableTrainingService(db=mock_db, client=mock_client, worker_id="recovery-eval-worker")
    await worker.reconcile_stale_leases()

    mock_client.evaluate_candidate.return_value = {
        "model_id": "cand-already-trained-009",
        "sample_count": 150,
        "metrics": {"macro_f1": 0.89, "brier_score": 0.08},
    }

    res = await worker.process_admitted_job("job-crash-eval")
    assert res.status == JobStatus.PROMOTED

    mock_client.submit_training_job.assert_not_called()
    mock_client.get_training_job.assert_not_called()
    mock_client.evaluate_candidate.assert_called_once_with("cand-already-trained-009")


# =========================================================================
# Work Order Item 7: Cancellation & Stale-Worker Protection
# =========================================================================

@pytest.mark.asyncio
async def test_job_cancellation_calls_remote_jetson_and_marks_cancelled(mock_db, mock_client):
    """Cancelling an active job invokes Jetson /v1/training/jobs/{id}/cancel."""
    service = DurableTrainingService(db=mock_db, client=mock_client, worker_id="worker-1")

    col = mock_db["training_jobs"]
    col.insert_one({
        "job_id": "job-to-cancel",
        "task": "gliner_finetune",
        "base_model_id": "gliner",
        "status": JobStatus.RUNNING.value,
        "jetson_job_id": "jetson-job-to-cancel-888",
        "lease_owner": "worker-1",
    })

    cancelled = await service.cancel_job("job-to-cancel")
    assert cancelled.status == JobStatus.CANCELLED
    mock_client.cancel_training_job.assert_called_once_with("jetson-job-to-cancel-888")


@pytest.mark.asyncio
async def test_stale_worker_rejected_when_lease_reclaimed_by_another(mock_db, mock_client):
    """A zombie worker whose lease expired cannot promote or mutate job state."""
    service_zombie = DurableTrainingService(db=mock_db, client=mock_client, worker_id="zombie-worker")

    now = datetime.datetime.now(datetime.timezone.utc)
    col = mock_db["training_jobs"]
    col.insert_one({
        "job_id": "job-leased-to-other",
        "task": "gliner_finetune",
        "base_model_id": "gliner",
        "status": JobStatus.RUNNING.value,
        "lease_owner": "active-worker-alive",  # Owned by someone else
        "lease_expires_at": (now + datetime.timedelta(seconds=60)).isoformat(),
    })

    with pytest.raises(RuntimeError, match="lease held by another worker"):
        await service_zombie.process_admitted_job("job-leased-to-other")
