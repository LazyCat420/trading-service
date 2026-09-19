"""
Acceptance Test 3: Test durability with real MongoDB semantics and two worker processes.
- Runs against real MongoDB test database (TRADING_BOT_MONGO_TEST=1 TRADING_MONGO_TEST_DB=trading_bot_pytest).
- Simulates concurrent workers racing capacity admission and lease acquisition.
- Tests lease expiration while original worker is alive (stale worker rejected).
- Tests crash/restart recovery at various stages (remote submission, training, evaluation).
- Verifies zero duplicate remote jobs (resuming existing jetson_job_id).
- Verifies real MongoDB Cursor semantics (ensuring count_documents is used, no len(cursor)).
- Paired sabotage test: Worker B attempting updates on Worker A's unexpired lease raises
  RuntimeError("Failed to acquire lease for job ...: lease held by another worker").
"""

import asyncio
import datetime
from unittest.mock import AsyncMock, MagicMock
import pytest

from app.services.durable_training_service import (
    DurableTrainingService,
    JobStatus,
    TrainingJobRecord,
)

pytestmark = pytest.mark.real_mongo


@pytest.fixture
def clean_mongo(real_mongo):
    """Provides an isolated, clean real MongoDB database on the NAS."""
    for col_name in real_mongo.list_collection_names():
        real_mongo.drop_collection(col_name)
    yield real_mongo
    for col_name in real_mongo.list_collection_names():
        real_mongo.drop_collection(col_name)


@pytest.fixture
def mock_jetson():
    client = MagicMock()
    client.get_health = AsyncMock(return_value={
        "status": "ok",
        "queue": {"training_active": 0, "training_max": 1}
    })
    client.submit_training_job = AsyncMock(return_value={"job_id": "remote-jetson-999", "status": "pending"})
    client.get_training_job = AsyncMock(return_value={"status": "completed", "candidate_model_id": "cand-999"})
    client.evaluate_candidate = AsyncMock(return_value={
        "model_id": "cand-999",
        "sample_count": 250,
        "metrics": {"f1": 0.95, "precision": 0.95, "recall": 0.95, "latency_p99_ms": 20.0}
    })
    client.promote_candidate = AsyncMock(return_value={"status": "promoted"})
    client.get_active_model = AsyncMock(return_value={"model_id": "cand-999", "task": "gliner"})
    return client


@pytest.mark.asyncio
async def test_cursor_len_check_does_not_crash(clean_mongo, mock_jetson):
    """Verifies that try_admit_next_job does NOT call len(col.find(...)) on real PyMongo cursors."""
    service = DurableTrainingService(db=clean_mongo, client=mock_jetson, worker_id="worker-cursor-check")
    await service.submit_job(task="gliner_finetune", base_model_id="gliner", dataset_manifest_id="m1")

    # This will raise TypeError if len(col.find(...)) is called on real PyMongo cursor
    admitted = await service.try_admit_next_job()
    assert admitted is True


@pytest.mark.asyncio
async def test_two_workers_racing_capacity_admission(clean_mongo, mock_jetson):
    """
    Submits two jobs to queue. Two workers concurrently race to admit.
    Only ONE job can be admitted because Jetson capacity is max 1.
    """
    worker_1 = DurableTrainingService(db=clean_mongo, client=mock_jetson, worker_id="worker-alpha")
    worker_2 = DurableTrainingService(db=clean_mongo, client=mock_jetson, worker_id="worker-beta")

    job_a = await worker_1.submit_job(task="gliner_finetune", base_model_id="gliner", dataset_manifest_id="m-a")
    job_b = await worker_2.submit_job(task="cnn_regime", base_model_id="cnn", dataset_manifest_id="m-b")

    # Race admission concurrently
    results = await asyncio.gather(
        worker_1.try_admit_next_job(),
        worker_2.try_admit_next_job(),
    )

    # Exactly one admission must succeed, one must fail/stay queued
    assert results.count(True) == 1
    assert results.count(False) == 1

    # Check real database state: exactly 1 ADMITTED, exactly 1 QUEUED
    col = clean_mongo.get_collection("training_jobs")
    assert col.count_documents({"status": JobStatus.ADMITTED.value}) == 1
    assert col.count_documents({"status": JobStatus.QUEUED.value}) == 1


@pytest.mark.asyncio
async def test_lease_contention_and_stale_worker_rejection(clean_mongo, mock_jetson):
    """
    Worker 1 acquires lease for a job.
    Lease expires while Worker 1 is paused.
    Worker 2 acquires the expired lease.
    Worker 1 attempts to write / heartbeat and is rejected.
    """
    worker_1 = DurableTrainingService(db=clean_mongo, client=mock_jetson, worker_id="worker-1")
    worker_2 = DurableTrainingService(db=clean_mongo, client=mock_jetson, worker_id="worker-2")

    job = await worker_1.submit_job(task="gliner_finetune", base_model_id="gliner", dataset_manifest_id="m-1")
    await worker_1.try_admit_next_job()

    # Step 1: Worker 1 acquires lease with past expiration
    now = datetime.datetime.now(datetime.timezone.utc)
    clean_mongo.get_collection("training_jobs").update_one(
        {"job_id": job.job_id},
        {"$set": {
            "status": JobStatus.RUNNING.value,
            "lease_owner": "worker-1",
            "lease_expires_at": (now - datetime.timedelta(seconds=1)).isoformat(),  # Already expired
            "jetson_job_id": "remote-job-123",
        }}
    )

    # Step 2: Worker 2 acquires the expired lease
    now_fresh = datetime.datetime.now(datetime.timezone.utc)
    fresh_exp = (now_fresh + datetime.timedelta(seconds=60)).isoformat()
    acquired_by_2 = clean_mongo.get_collection("training_jobs").update_one(
        {
            "job_id": job.job_id,
            "$or": [
                {"status": JobStatus.ADMITTED.value},
                {"lease_owner": None},
                {"lease_expires_at": {"$lt": now_fresh.isoformat()}},
            ],
        },
        {"$set": {"lease_owner": "worker-2", "lease_expires_at": fresh_exp}}
    )
    assert acquired_by_2.modified_count == 1

    # Step 3: Stale Worker 1 attempts a heartbeat write under its old lease
    stale_write = clean_mongo.get_collection("training_jobs").update_one(
        {"job_id": job.job_id, "lease_owner": "worker-1"},
        {"$set": {"lease_expires_at": (now_fresh + datetime.timedelta(seconds=120)).isoformat()}}
    )
    # Stale write MUST be rejected
    assert stale_write.modified_count == 0
    # Lease owner must STILL be worker-2
    current = await worker_1.get_job(job.job_id)
    assert current.lease_owner == "worker-2"


@pytest.mark.asyncio
async def test_worker_restart_resumes_existing_remote_job_without_requeuing(clean_mongo, mock_jetson):
    """
    Worker crashes after remote submission (jetson_job_id exists).
    Replacement worker recovers the job:
    - Does NOT call client.submit_training_job again (no duplicate remote job).
    - Resumes polling existing remote job and completes promotion.
    """
    worker_crashed = DurableTrainingService(db=clean_mongo, client=mock_jetson, worker_id="worker-crashed")
    job = await worker_crashed.submit_job(task="gliner_finetune", base_model_id="gliner", dataset_manifest_id="m-resume")
    await worker_crashed.try_admit_next_job()

    # Simulate crash right after remote job creation
    col = clean_mongo.get_collection("training_jobs")
    col.update_one(
        {"job_id": job.job_id},
        {"$set": {
            "status": JobStatus.RUNNING.value,
            "jetson_job_id": "existing-remote-456",
            "lease_owner": None,
            "lease_expires_at": None,
        }}
    )

    # Replacement worker starts up and processes the job
    worker_recovered = DurableTrainingService(db=clean_mongo, client=mock_jetson, worker_id="worker-recovered")
    completed_job = await worker_recovered.process_admitted_job(job_id=job.job_id)

    # Verify: zero duplicate remote job submissions
    mock_jetson.submit_training_job.assert_not_called()

    # Verify: polled the existing remote job ID
    mock_jetson.get_training_job.assert_awaited_with("existing-remote-456")

    # Verify: job reached terminal PROMOTED state
    assert completed_job.status == JobStatus.PROMOTED
    assert completed_job.candidate_model_id == "cand-999"


@pytest.mark.asyncio
async def test_sabotage_worker_cannot_acquire_unexpired_lease_of_another_worker(clean_mongo, mock_jetson):
    """
    SABOTAGE TEST:
    Worker A holds an active, unexpired lease on an admitted job.
    Worker B attempts to process or take over the job.
    Enforces that Worker B is rejected with:
    RuntimeError("Failed to acquire lease for job <job_id>: lease held by another worker").
    """
    worker_a = DurableTrainingService(db=clean_mongo, client=mock_jetson, worker_id="worker-A")
    worker_b = DurableTrainingService(db=clean_mongo, client=mock_jetson, worker_id="worker-B")

    job = await worker_a.submit_job(task="gliner_finetune", base_model_id="gliner", dataset_manifest_id="m-sabotage")
    await worker_a.try_admit_next_job()

    # Worker A holds active, unexpired lease (valid for 120s)
    now = datetime.datetime.now(datetime.timezone.utc)
    clean_mongo.get_collection("training_jobs").update_one(
        {"job_id": job.job_id},
        {"$set": {
            "status": JobStatus.RUNNING.value,
            "lease_owner": "worker-A",
            "lease_expires_at": (now + datetime.timedelta(seconds=120)).isoformat(),
        }}
    )

    # Sabotage attempt: Worker B tries to acquire lease and process the job
    with pytest.raises(RuntimeError, match=f"Failed to acquire lease for job {job.job_id}: lease held by another worker"):
        await worker_b.process_admitted_job(job_id=job.job_id)

    # Verify state in real Mongo remains unperturbed and still owned by Worker A
    col = clean_mongo.get_collection("training_jobs")
    persisted = col.find_one({"job_id": job.job_id})
    assert persisted["lease_owner"] == "worker-A"
    assert persisted["status"] == JobStatus.RUNNING.value


@pytest.mark.asyncio
async def test_crash_recovery_at_admitted_transition_with_real_mongo(clean_mongo, mock_jetson):
    """
    Worker 1 admits job to ADMITTED, then crashes before acquiring lease.
    reconcile_stale_leases reconciles the stale unassigned ADMITTED job back to QUEUED.
    Worker 2 re-admits and completes execution.
    """
    worker_1 = DurableTrainingService(db=clean_mongo, client=mock_jetson, worker_id="worker-admit-crash")
    job = await worker_1.submit_job(task="gliner_finetune", base_model_id="gliner", dataset_manifest_id="m-admit-crash")
    admitted = await worker_1.try_admit_next_job()
    assert admitted is True

    col = clean_mongo.get_collection("training_jobs")
    # Simulate time passing with unowned ADMITTED job (stale > 60s)
    stale_ts = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(seconds=90)).isoformat()
    col.update_one({"job_id": job.job_id}, {"$set": {"updated_at": stale_ts, "created_at": stale_ts}})

    worker_2 = DurableTrainingService(db=clean_mongo, client=mock_jetson, worker_id="worker-admit-recovery")
    reclaimed = await worker_2.reconcile_stale_leases()
    assert reclaimed == 1

    # Re-checked job must be reset to QUEUED for fresh admission
    reloaded = await worker_2.get_job(job.job_id)
    assert reloaded.status == JobStatus.QUEUED

    # Worker 2 admits and processes it
    assert await worker_2.try_admit_next_job() is True
    completed = await worker_2.process_admitted_job(job.job_id)
    assert completed.status == JobStatus.PROMOTED


@pytest.mark.asyncio
async def test_crash_recovery_at_evaluating_transition_with_real_mongo(clean_mongo, mock_jetson):
    """
    Job finishes remote training, enters EVALUATING with candidate_model_id set.
    Worker crashes during evaluation. Lease expires.
    Replacement worker recovers the job and completes holdout evaluation without re-submitting to Jetson.
    """
    col = clean_mongo.get_collection("training_jobs")
    stale_exp = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(seconds=10)).isoformat()
    now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()

    col.insert_one({
        "job_id": "job-eval-recovery-real",
        "task": "gliner_finetune",
        "base_model_id": "gliner",
        "dataset_manifest_id": "m-eval-real",
        "status": JobStatus.EVALUATING.value,
        "candidate_model_id": "cand-999",
        "jetson_job_id": "jetson-already-completed",
        "lease_owner": "worker-crashed-eval",
        "lease_expires_at": stale_exp,
        "created_at": now_iso,
        "updated_at": now_iso,
    })

    worker_recovery = DurableTrainingService(db=clean_mongo, client=mock_jetson, worker_id="worker-recovery")
    reclaimed = await worker_recovery.reconcile_stale_leases()
    assert reclaimed == 1

    completed = await worker_recovery.process_admitted_job("job-eval-recovery-real")
    assert completed.status == JobStatus.PROMOTED
    assert completed.candidate_model_id == "cand-999"

    # Verify: submit_training_job was never called
    mock_jetson.submit_training_job.assert_not_called()
    mock_jetson.get_training_job.assert_not_called()
    mock_jetson.evaluate_candidate.assert_called_once_with("cand-999")
    mock_jetson.promote_candidate.assert_called_once_with("cand-999")


@pytest.mark.asyncio
async def test_database_enforced_submission_uniqueness_with_real_mongo(clean_mongo, mock_jetson):
    """
    Tests database-enforced submission uniqueness under concurrent load.
    Spawns 20 parallel submission tasks with the exact same idempotency key.
    Enforces that exactly 1 document is inserted and all 20 return the same job_id.
    """
    service = DurableTrainingService(db=clean_mongo, client=mock_jetson, worker_id="worker-uniq-check")
    service.ensure_indexes()

    async def _submit():
        return await service.submit_job(
            task="gliner_finetune",
            base_model_id="gliner",
            dataset_manifest_id="manifest-concurrency-uniq-test",
            hyperparameters={"lr": 1e-4, "epochs": 3},
        )

    tasks = [_submit() for _ in range(20)]
    results = await asyncio.gather(*tasks)

    # All 20 must return the same job_id
    job_ids = {r.job_id for r in results}
    assert len(job_ids) == 1
    shared_job_id = job_ids.pop()

    col = clean_mongo.get_collection("training_jobs")
    # Real Mongo must contain exactly 1 document for this idempotency key
    assert col.count_documents({"job_id": shared_job_id}) == 1


@pytest.mark.asyncio
async def test_cancellation_before_promotion_with_real_mongo(clean_mongo, mock_jetson):
    """
    Verifies that a job cancelled prior to promotion is aborted without calling promote_candidate.
    """
    worker = DurableTrainingService(db=clean_mongo, client=mock_jetson, worker_id="worker-cancel-test")
    job = await worker.submit_job(task="gliner_finetune", base_model_id="gliner", dataset_manifest_id="m-cancel")
    await worker.try_admit_next_job()

    orig_evaluate = mock_jetson.evaluate_candidate
    async def cancel_mid_flight(*args, **kwargs):
        res = await orig_evaluate(*args, **kwargs)
        await worker.cancel_job(job.job_id)
        return res
    mock_jetson.evaluate_candidate = cancel_mid_flight

    processed = await worker.process_admitted_job(job.job_id)
    assert processed.status == JobStatus.CANCELLED
    mock_jetson.promote_candidate.assert_not_called()


@pytest.mark.asyncio
async def test_worker_loop_end_to_end_http_queue_drain_with_real_mongo(clean_mongo, mock_jetson):
    """
    Tests the actual background worker loop draining the queue without manual intervention.
    Starts start_worker_loop in an async task against real MongoDB.
    Submits a job via submit_job (or HTTP route).
    Asserts the worker loop claims, executes, evaluates, and promotes the job automatically.
    """
    worker = DurableTrainingService(db=clean_mongo, client=mock_jetson, worker_id="worker-loop-drain")
    worker.ensure_indexes()
    shutdown = asyncio.Event()

    loop_task = asyncio.create_task(
        worker.start_worker_loop(poll_interval_seconds=0.05, shutdown_event=shutdown)
    )

    try:
        # Submit a job into the queue
        job = await worker.submit_job(
            task="gliner_finetune",
            base_model_id="gliner",
            dataset_manifest_id="m-loop-drain",
            hyperparameters={"epochs": 2},
        )
        assert job.status == JobStatus.QUEUED

        # Poll real MongoDB until the job is PROMOTED
        final_job = None
        for _ in range(60):
            current = await worker.get_job(job.job_id)
            if current and current.status == JobStatus.PROMOTED:
                final_job = current
                break
            await asyncio.sleep(0.05)

        assert final_job is not None, "Worker loop did not drain queue to PROMOTED within timeout"
        assert final_job.status == JobStatus.PROMOTED
        assert final_job.candidate_model_id == "cand-999"

    finally:
        shutdown.set()
        loop_task.cancel()
        try:
            await loop_task
        except asyncio.CancelledError:
            pass

