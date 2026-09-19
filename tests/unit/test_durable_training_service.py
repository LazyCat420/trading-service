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
                if "$ne" in v and doc.get(k) == v["$ne"]:
                    return False
                if "$lt" in v:
                    doc_val = doc.get(k)
                    if doc_val is None or str(doc_val) >= str(v["$lt"]):
                        return False
                if "$gte" in v:
                    doc_val = doc.get(k)
                    if doc_val is None or str(doc_val) < str(v["$gte"]):
                        return False
            elif doc.get(k) != v:
                return False
        return True

    class MockCollection:
        def __init__(self):
            self.indexes = {}

        def create_index(self, keys, **kwargs):
            name = kwargs.get("name", str(keys))
            self.indexes[name] = {"keys": keys, "kwargs": kwargs}
            return name

        def find_one(self, query, sort=None):
            matched = [dict(d) for d in docs.values() if _matches(d, query)]
            if not matched:
                return None
            if sort:
                key, direction = sort[0]
                matched.sort(key=lambda x: x.get(key, ""), reverse=(direction < 0))
            return matched[0]

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


@pytest.mark.asyncio
async def test_worker_loop_dispatches_without_execute_leased_job_error(service, mock_jetson):
    """
    Submits a job, runs start_worker_loop as a background task.
    Verifies that the worker loop admits and processes the job to PROMOTED without
    failing with AttributeError: execute_leased_job.
    """
    import asyncio
    shutdown = asyncio.Event()

    job = await service.submit_job(
        task="gliner_finetune",
        base_model_id="gliner",
        dataset_manifest_id="manifest-worker-loop-test",
    )

    worker_task = asyncio.create_task(
        service.start_worker_loop(poll_interval_seconds=0.05, shutdown_event=shutdown)
    )

    try:
        # Wait up to 3s for the worker loop to process the job
        for _ in range(60):
            current = await service.get_job(job.job_id)
            if current and current.status == JobStatus.PROMOTED:
                break
            await asyncio.sleep(0.05)

        final_job = await service.get_job(job.job_id)
        assert final_job.status == JobStatus.PROMOTED
        assert final_job.candidate_model_id == "cand-test-1"
    finally:
        shutdown.set()
        worker_task.cancel()
        try:
            await worker_task
        except asyncio.CancelledError:
            pass


@pytest.mark.asyncio
async def test_pre_promotion_cancellation_check(service, mock_jetson, mock_db):
    """
    SABOTAGE / SAFETY GATE:
    Job evaluates successfully, but is cancelled prior to promotion call.
    The service must detect cancellation, abort promotion, and NOT call client.promote_candidate.
    """
    job = await service.submit_job(
        task="gliner_finetune",
        base_model_id="gliner",
        dataset_manifest_id="manifest-cancel-test",
    )
    await service.try_admit_next_job()

    # Intercept evaluate_candidate to cancel the job right before promotion
    orig_evaluate = mock_jetson.evaluate_candidate
    async def cancel_mid_flight(*args, **kwargs):
        res = await orig_evaluate(*args, **kwargs)
        # Simulate operator cancelling the job
        await service.cancel_job(job.job_id)
        return res
    mock_jetson.evaluate_candidate = cancel_mid_flight

    processed = await service.process_admitted_job(job.job_id)
    assert processed.status == JobStatus.CANCELLED
    mock_jetson.promote_candidate.assert_not_called()


@pytest.mark.asyncio
async def test_pre_promotion_lease_loss_check(service, mock_jetson, mock_db):
    """
    SABOTAGE / SAFETY GATE:
    Worker A evaluates job, but Worker B steals/claims the lease right before promotion.
    Worker A must detect lost ownership and abort with RuntimeError without calling promote_candidate.
    """
    job = await service.submit_job(
        task="gliner_finetune",
        base_model_id="gliner",
        dataset_manifest_id="manifest-lease-loss",
    )
    await service.try_admit_next_job()

    orig_evaluate = mock_jetson.evaluate_candidate
    async def steal_lease_mid_flight(*args, **kwargs):
        res = await orig_evaluate(*args, **kwargs)
        # Another worker steals lease
        mock_db["training_jobs"].update_one(
            {"job_id": job.job_id},
            {"$set": {"lease_owner": "worker-thief-99"}}
        )
        return res
    mock_jetson.evaluate_candidate = steal_lease_mid_flight

    with pytest.raises(RuntimeError, match="Cannot promote candidate.*lease held by worker-thief-99"):
        await service.process_admitted_job(job.job_id)

    mock_jetson.promote_candidate.assert_not_called()


@pytest.mark.asyncio
async def test_recovers_interrupted_evaluating_job(service, mock_jetson, mock_db):
    """
    Worker crashes during EVALUATING after remote training has completed (candidate_model_id set).
    Recovering worker reclaims job:
    - Skips submit_training_job and polling
    - Directly executes holdout evaluation and promotion
    """
    now = datetime.datetime.now(datetime.timezone.utc)
    stale_time = (now - datetime.timedelta(minutes=5)).isoformat()
    mock_db["training_jobs"].insert_one({
        "job_id": "job-evaluating-crash",
        "task": "gliner_finetune",
        "base_model_id": "gliner",
        "dataset_manifest_id": "man-eval-crash",
        "status": JobStatus.EVALUATING.value,
        "lease_owner": "dead-worker",
        "lease_expires_at": stale_time,
        "jetson_job_id": "jetson-already-done",
        "candidate_model_id": "cand-already-trained",
    })

    # Reconcile stale leases should clear lease on EVALUATING job
    reclaimed = await service.reconcile_stale_leases()
    assert reclaimed == 1

    mock_jetson.evaluate_candidate.return_value = {
        "model_id": "cand-already-trained",
        "sample_count": 150,
        "metrics": {"f1": 0.94, "precision": 0.94, "recall": 0.94, "latency_p99_ms": 25.0}
    }
    mock_jetson.get_active_model.return_value = {"model_id": "cand-already-trained", "task": "gliner"}

    # Recovering worker processes the job
    processed = await service.process_admitted_job("job-evaluating-crash")
    assert processed.status == JobStatus.PROMOTED
    assert processed.candidate_model_id == "cand-already-trained"

    # Zero remote training submissions or polling
    mock_jetson.submit_training_job.assert_not_called()
    mock_jetson.get_training_job.assert_not_called()
    mock_jetson.evaluate_candidate.assert_called_once_with("cand-already-trained")
    mock_jetson.promote_candidate.assert_called_once_with("cand-already-trained")


@pytest.mark.asyncio
async def test_cannot_process_terminal_job(service, mock_db):
    """
    Attempting to process a job already in a terminal state (PROMOTED, CANCELLED, etc.) raises ValueError.
    """
    mock_db["training_jobs"].insert_one({
        "job_id": "job-already-promoted",
        "task": "gliner_finetune",
        "base_model_id": "gliner",
        "status": JobStatus.PROMOTED.value,
    })

    with pytest.raises(ValueError, match="Cannot process job.*terminal state PROMOTED"):
        await service.process_admitted_job("job-already-promoted")

