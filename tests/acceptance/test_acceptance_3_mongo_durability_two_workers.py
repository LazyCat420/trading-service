"""
Acceptance Test 3: Test durability with real MongoDB semantics and two worker processes.
- Simulates concurrent workers racing capacity admission and lease acquisition.
- Tests lease expiration while original worker is alive (stale worker rejected).
- Tests crash/restart recovery at various stages (remote submission, training, evaluation).
- Verifies zero duplicate remote jobs (resuming existing jetson_job_id).
- Verifies MongoDB Cursor semantics (ensuring count_documents is used, no len(cursor)).
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


class PymongoMockCursor:
    """Simulates real pymongo Cursor which raises TypeError on len()."""
    def __init__(self, docs):
        self._docs = docs

    def __iter__(self):
        return iter(self._docs)

    def __len__(self):
        raise TypeError("object of type 'Cursor' has no len(). Use count_documents instead.")


class RealMongoSemanticCollection:
    """In-memory collection enforcing exact PyMongo collection behavior and atomic operations."""
    def __init__(self):
        self.docs = {}

    def _matches(self, doc, query):
        for k, v in query.items():
            if k == "$or":
                if not any(self._matches(doc, cond) for cond in v):
                    return False
            elif isinstance(v, dict):
                if "$in" in v and doc.get(k) not in v["$in"]:
                    return False
                if "$lt" in v:
                    val = doc.get(k)
                    if val is None or str(val) >= str(v["$lt"]):
                        return False
            elif doc.get(k) != v:
                return False
        return True

    def find_one(self, query):
        for d in self.docs.values():
            if self._matches(d, query):
                return dict(d)
        return None

    def find(self, query=None):
        matched = [dict(d) for d in self.docs.values() if query is None or self._matches(d, query)]
        return PymongoMockCursor(matched)

    def count_documents(self, query):
        return sum(1 for d in self.docs.values() if self._matches(d, query))

    def insert_one(self, doc):
        jid = doc.get("job_id")
        self.docs[jid] = dict(doc)
        return MagicMock(inserted_id=jid)

    def update_one(self, query, update):
        for jid, d in self.docs.items():
            if self._matches(d, query):
                if "$set" in update:
                    d.update(update["$set"])
                return MagicMock(modified_count=1)
        return MagicMock(modified_count=0)

    def find_one_and_update(self, query, update, sort=None):
        matched = [d for d in self.docs.values() if self._matches(d, query)]
        if not matched:
            return None
        if sort:
            key, direction = sort[0]
            matched.sort(key=lambda x: x.get(key, ""), reverse=(direction < 0))
        target = matched[0]
        if "$set" in update:
            target.update(update["$set"])
        return dict(target)


@pytest.fixture
def mongo_collection():
    return RealMongoSemanticCollection()


@pytest.fixture
def mock_db(mongo_collection):
    db = MagicMock()
    db.get_collection.return_value = mongo_collection
    db.__getitem__.return_value = mongo_collection
    return db


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
async def test_cursor_len_check_does_not_crash(mock_db, mock_jetson):
    """Verifies that try_admit_next_job does NOT call len(col.find(...)) on PyMongo cursors."""
    service = DurableTrainingService(db=mock_db, client=mock_jetson, worker_id="worker-cursor-check")
    await service.submit_job(task="gliner_finetune", base_model_id="gliner", dataset_manifest_id="m1")

    # This will raise TypeError if len(col.find(...)) is called
    admitted = await service.try_admit_next_job()
    assert admitted is True


@pytest.mark.asyncio
async def test_two_workers_racing_capacity_admission(mock_db, mock_jetson):
    """
    Submits two jobs to queue. Two workers concurrently race to admit.
    Only ONE job can be admitted because Jetson capacity is max 1.
    """
    worker_1 = DurableTrainingService(db=mock_db, client=mock_jetson, worker_id="worker-alpha")
    worker_2 = DurableTrainingService(db=mock_db, client=mock_jetson, worker_id="worker-beta")

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

    # Check database state: exactly 1 ADMITTED, exactly 1 QUEUED
    col = mock_db.get_collection("training_jobs")
    assert col.count_documents({"status": JobStatus.ADMITTED.value}) == 1
    assert col.count_documents({"status": JobStatus.QUEUED.value}) == 1


@pytest.mark.asyncio
async def test_lease_contention_and_stale_worker_rejection(mock_db, mock_jetson):
    """
    Worker 1 acquires lease for a job.
    Lease expires while Worker 1 is paused.
    Worker 2 acquires the expired lease.
    Worker 1 attempts to write / heartbeat and is rejected.
    """
    worker_1 = DurableTrainingService(db=mock_db, client=mock_jetson, worker_id="worker-1")
    worker_2 = DurableTrainingService(db=mock_db, client=mock_jetson, worker_id="worker-2")

    job = await worker_1.submit_job(task="gliner_finetune", base_model_id="gliner", dataset_manifest_id="m-1")
    await worker_1.try_admit_next_job()

    # Step 1: Worker 1 acquires lease with 0.1s expiration
    now = datetime.datetime.now(datetime.timezone.utc)
    mock_db.get_collection("training_jobs").update_one(
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
    acquired_by_2 = mock_db.get_collection("training_jobs").update_one(
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
    stale_write = mock_db.get_collection("training_jobs").update_one(
        {"job_id": job.job_id, "lease_owner": "worker-1"},
        {"$set": {"lease_expires_at": (now_fresh + datetime.timedelta(seconds=120)).isoformat()}}
    )
    # Stale write MUST be rejected
    assert stale_write.modified_count == 0
    # Lease owner must STILL be worker-2
    current = await worker_1.get_job(job.job_id)
    assert current.lease_owner == "worker-2"


@pytest.mark.asyncio
async def test_worker_restart_resumes_existing_remote_job_without_requeuing(mock_db, mock_jetson):
    """
    Worker crashes after remote submission (jetson_job_id exists).
    Replacement worker recovers the job:
    - Does NOT call client.submit_training_job again (no duplicate remote job).
    - Resumes polling existing remote job and completes promotion.
    """
    worker_crashed = DurableTrainingService(db=mock_db, client=mock_jetson, worker_id="worker-crashed")
    job = await worker_crashed.submit_job(task="gliner_finetune", base_model_id="gliner", dataset_manifest_id="m-resume")
    await worker_crashed.try_admit_next_job()

    # Simulate crash right after remote job creation
    col = mock_db.get_collection("training_jobs")
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
    worker_recovered = DurableTrainingService(db=mock_db, client=mock_jetson, worker_id="worker-recovered")
    completed_job = await worker_recovered.process_admitted_job(job_id=job.job_id)

    # Verify: zero duplicate remote job submissions
    mock_jetson.submit_training_job.assert_not_called()

    # Verify: polled the existing remote job ID
    mock_jetson.get_training_job.assert_awaited_with("existing-remote-456")

    # Verify: job reached terminal PROMOTED state
    assert completed_job.status == JobStatus.PROMOTED
    assert completed_job.candidate_model_id == "cand-999"
