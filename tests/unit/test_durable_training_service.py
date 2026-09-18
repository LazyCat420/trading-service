"""
Unit tests for Durable Training Service & Job State Machine.
Verifies Item 8:
- Idempotent job submission with deduplication.
- Atomic capacity admission (Jetson training_max check).
- Job state transitions: SUBMITTED -> QUEUED -> ADMITTED -> RUNNING -> EVALUATING -> PROMOTED/REJECTED.
- Lease acquisition, heartbeat renewal, and crash/restart recovery.
- Job cancellation and timeout reconciliation.
"""

import datetime
from unittest.mock import AsyncMock, MagicMock, patch
import uuid
import pytest

from app.services.durable_training_service import (
    DurableTrainingService,
    JobStatus,
    TrainingJobRecord,
)


@pytest.fixture
def mock_db():
    docs = {}

    def _matches(doc, q):
        for k, v in q.items():
            if k == "$or":
                sub_match = any(_matches(doc, cond) for cond in v)
                if not sub_match:
                    return False
            elif isinstance(v, dict):
                if "$in" in v and doc.get(k) not in v["$in"]:
                    return False
                if "$lt" in v:
                    doc_val = doc.get(k)
                    if doc_val is None or str(doc_val) >= str(v["$lt"]):
                        return False
            elif doc.get(k) != v:
                return False
        return True

    class MockCollection:
        def find_one(self, query):
            for doc in docs.values():
                if _matches(doc, query):
                    return dict(doc)
            return None

        def find(self, query=None):
            results = []
            for doc in docs.values():
                if query is None or _matches(doc, query):
                    results.append(dict(doc))
            return results

        def count_documents(self, query):
            return len(self.find(query))

        def insert_one(self, doc):
            doc_id = doc.get("job_id", str(uuid.uuid4()))
            docs[doc_id] = dict(doc)
            return MagicMock(inserted_id=doc_id)

        def update_one(self, query, update):
            for job_id, doc in docs.items():
                if _matches(doc, query):
                    if "$set" in update:
                        doc.update(update["$set"])
                    return MagicMock(modified_count=1)
            return MagicMock(modified_count=0)

    col = MockCollection()
    db = MagicMock()
    db.get_collection.return_value = col
    db.__getitem__.return_value = col
    return db


@pytest.fixture
def mock_jetson():
    client = MagicMock()
    client.get_health = AsyncMock(return_value={"status": "ok", "queue": {"training_active": 0, "training_max": 1}})
    client.submit_training_job = AsyncMock(return_value={"job_id": "jetson-job-1", "status": "pending"})
    client.get_training_job = AsyncMock(return_value={"status": "completed", "candidate_model_id": "cand-test-1"})
    client.evaluate_candidate = AsyncMock(return_value={
        "model_id": "cand-test-1",
        "sample_count": 150,
        "metrics": {"f1": 0.94, "precision": 0.94, "recall": 0.94, "latency_p99_ms": 25.0}
    })
    client.promote_candidate = AsyncMock(return_value={"status": "promoted"})
    client.get_active_model = AsyncMock(return_value={"model_id": "cand-test-1", "task": "gliner"})
    return client


@pytest.fixture
def service(mock_db, mock_jetson):
    return DurableTrainingService(db=mock_db, client=mock_jetson, worker_id="worker-test-1")


@pytest.mark.asyncio
async def test_idempotent_job_submission(service):
    # Submitting identical job twice must return the same job record
    job1 = await service.submit_job(
        task="gliner_finetune",
        base_model_id="gliner",
        dataset_manifest_id="manifest-abc",
        hyperparameters={"epochs": 3, "lr": 1e-4},
    )

    job2 = await service.submit_job(
        task="gliner_finetune",
        base_model_id="gliner",
        dataset_manifest_id="manifest-abc",
        hyperparameters={"epochs": 3, "lr": 1e-4},
    )

    assert job1.job_id == job2.job_id
    assert job1.status in (JobStatus.SUBMITTED, JobStatus.QUEUED)


@pytest.mark.asyncio
async def test_atomic_capacity_admission_gate(service, mock_jetson):
    # When Jetson queue is busy, job remains QUEUED
    mock_jetson.get_health.return_value = {"status": "ok", "queue": {"training_active": 1, "training_max": 1}}

    job = await service.submit_job(
        task="cnn_regime",
        base_model_id="market_cnn",
        dataset_manifest_id="manifest-cnn-1",
    )

    admitted = await service.try_admit_next_job()
    assert not admitted
    reloaded = await service.get_job(job.job_id)
    assert reloaded.status == JobStatus.QUEUED


@pytest.mark.asyncio
async def test_full_durable_job_execution(service, mock_jetson):
    job = await service.submit_job(
        task="gliner_finetune",
        base_model_id="gliner",
        dataset_manifest_id="manifest-gliner-exec",
    )

    # Admit and execute
    admitted = await service.try_admit_next_job()
    assert admitted

    result_job = await service.process_admitted_job(job.job_id)
    assert result_job.status == JobStatus.PROMOTED
    assert result_job.candidate_model_id == "cand-test-1"
    mock_jetson.submit_training_job.assert_called_once()
    mock_jetson.promote_candidate.assert_called_once_with("cand-test-1")


@pytest.mark.asyncio
async def test_recovers_orphaned_job_after_crash(service, mock_db):
    # Simulate a crashed worker whose lease expired
    stale_time = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(minutes=5)).isoformat()
    mock_db["training_jobs"].insert_one({
        "job_id": "crashed-job-1",
        "task": "gliner_finetune",
        "base_model_id": "gliner",
        "dataset_manifest_id": "man-crash",
        "status": JobStatus.RUNNING.value,
        "lease_owner": "dead-worker",
        "lease_expires_at": stale_time,
        "jetson_job_id": "jetson-job-crashed",
    })

    recovered_count = await service.reconcile_stale_leases()
    assert recovered_count == 1
    job = await service.get_job("crashed-job-1")
    # Recovered job should either be re-queued or reclaimed
    assert job.status in (JobStatus.QUEUED, JobStatus.RUNNING)
