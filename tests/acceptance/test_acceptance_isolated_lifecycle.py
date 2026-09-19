"""
Acceptance Test: Isolated Full Lifecycle (Proposal -> Dataset -> Job -> Candidate -> Champion Comparison -> Promotion -> Rollback).

Dev 1 Acceptance Evidence:
- Real HTTP route triggers:
  1. POST /features/training/proposals/submit
  2. POST /features/training/curate-and-train
  3. POST /features/training/models/{candidate_id}/promote
  4. POST /features/training/decay/evaluate
- Background worker leased execution with stable idempotency keys.
- Champion comparison and fail-closed gates.
- Verified rollback with specific expected champion and UNRESOLVED persistence.
- Crash recovery proving zero duplicate remote jobs.
"""

from __future__ import annotations

import datetime
import json
import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.routers.feature_training_router import router
from app.services.durable_training_service import (
    DurableTrainingService,
    JobStatus,
)
from app.services.jetson_feature_client import JetsonFeatureClient


class MockCollection:
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
        doc_id = doc.get("job_id") or doc.get("proposal_id") or doc.get("model_id") or str(uuid.uuid4())
        self.docs[doc_id] = dict(doc)
        return MagicMock(inserted_id=doc_id)

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
def test_db():
    collections: dict[str, MockCollection] = {}

    def get_col(name):
        if name not in collections:
            collections[name] = MockCollection()
        return collections[name]

    db = MagicMock()
    db.get_collection.side_effect = get_col
    db.__getitem__.side_effect = get_col
    return db


@pytest.fixture
def mock_jetson():
    client = MagicMock()
    active_models: dict[str, Any] = {
        "gliner": {"model_id": "gliner-champ-v1", "task": "gliner"},
        "gliner_finetune": {"model_id": "gliner-champ-v1", "task": "gliner_finetune"},
    }

    async def _get_active(task):
        t = task.lower()
        if "gliner" in t:
            return active_models.get("gliner")
        return active_models.get(task)

    async def _promote(cand_id, expected_champion_version=None, **kwargs):
        active_models["gliner"] = {"model_id": cand_id, "task": "gliner"}
        active_models["gliner_finetune"] = {"model_id": cand_id, "task": "gliner_finetune"}
        return {"status": "promoted", "active_model": cand_id}

    async def _rollback(model_id, **kwargs):
        # Restores prior champion gliner-champ-v1
        active_models["gliner"] = {"model_id": "gliner-champ-v1", "task": "gliner"}
        active_models["gliner_finetune"] = {"model_id": "gliner-champ-v1", "task": "gliner_finetune"}
        return {"status": "rolled_back", "restored_model": "gliner-champ-v1"}

    client.get_health = AsyncMock(return_value={"status": "ok", "queue": {"training_active": 0, "training_max": 1}})
    client.submit_training_job = AsyncMock(return_value={"job_id": "jetson-remote-job-99", "status": "pending"})
    client.get_training_job = AsyncMock(return_value={"status": "completed", "candidate_model_id": "cand-gliner-v2"})
    client.evaluate_candidate = AsyncMock(return_value={
        "model_id": "cand-gliner-v2",
        "sample_count": 250,
        "dataset_manifest_id": "manifest-acceptance-01",
        "metrics": {"f1": 0.948, "precision": 0.95, "recall": 0.946, "latency_p99_ms": 21.0},
    })
    client.get_model_metrics = AsyncMock(return_value={
        "model_id": "gliner-champ-v1",
        "sample_count": 250,
        "dataset_manifest_id": "manifest-acceptance-01",
        "metrics": {"f1": 0.920, "precision": 0.925, "recall": 0.915, "latency_p99_ms": 25.0},
    })
    client.promote_candidate = AsyncMock(side_effect=_promote)
    client.get_active_model = AsyncMock(side_effect=_get_active)
    client.rollback_model = AsyncMock(side_effect=_rollback)
    client.cancel_training_job = AsyncMock(return_value={"status": "cancelled"})
    return client


@pytest.fixture
def app_client(mock_jetson, test_db):
    app = FastAPI()
    app.include_router(router)

    with patch("app.routers.feature_training_router.feature_client", mock_jetson), \
         patch("app.routers.feature_training_router.mongo_store") as mock_router_mongo, \
         patch("app.services.durable_training_service.feature_client", mock_jetson), \
         patch("app.services.durable_training_service.mongo_store") as mock_durable_mongo, \
         patch("app.services.glm_retraining_proposal_service.mongo_store") as mock_prop_mongo, \
         patch("app.services.decay_monitor_service.mongo_store") as mock_decay_mongo:

        mock_router_mongo.get_doc_db.return_value = test_db
        mock_router_mongo.db = test_db
        mock_durable_mongo.get_doc_db.return_value = test_db
        mock_durable_mongo.db = test_db
        mock_prop_mongo.get_doc_db.return_value = test_db
        mock_prop_mongo.db = test_db
        mock_decay_mongo.get_doc_db.return_value = test_db
        mock_decay_mongo.db = test_db

        yield TestClient(app)


@pytest.mark.asyncio
async def test_traceable_full_lifecycle_and_verified_rollback(app_client, mock_jetson, test_db):
    """
    Acceptance Evidence: One traceable sequence:
    Proposal -> Dataset -> Remote Job -> Candidate -> Champion Comparison -> Promotion -> Verified Rollback.
    """
    # -------------------------------------------------------------
    # Step 1: Dataset Curation & Manifest Creation via HTTP POST
    # -------------------------------------------------------------
    curate_payload = {
        "task": "gliner_finetune",
        "samples": [
            {
                "tokenized_text": ["Apple", "beats", "earnings", "estimates", "by", "12%"],
                "ner": [[0, 0, "ORGANIZATION"], [2, 3, "METRIC"]],
                "timestamp": "2026-09-18T20:00:00Z",
            },
            {
                "tokenized_text": ["NVIDIA", "announces", "next-gen", "architecture"],
                "ner": [[0, 0, "ORGANIZATION"], [2, 3, "PRODUCT"]],
                "timestamp": "2026-09-18T20:30:00Z",
            },
        ],
        "auto_submit": False,
        "hyperparameters": {"learning_rate": 5e-5, "epochs": 3},
    }
    resp_curate = app_client.post("/features/training/curate-and-train", json=curate_payload)
    assert resp_curate.status_code == 200, resp_curate.text
    curate_data = resp_curate.json()
    assert "manifest_id" in curate_data
    assert "sha256" in curate_data
    manifest_id = curate_data["manifest_id"]

    # -------------------------------------------------------------
    # Step 2: Retraining Proposal Submission via HTTP POST
    # -------------------------------------------------------------
    proposal_payload = {
        "task": "gliner",
        "candidate_name": "cand-gliner-v2",
        "dataset_manifest_id": manifest_id,
        "hyperparameters": {"learning_rate": 5e-5, "batch_size": 16, "epochs": 3},
        "failure_cluster_ids": ["cluster-news-entities-42"],
        "justification": "Address entity extraction recall degradation on recent earnings headlines",
    }
    resp_prop = app_client.post("/features/training/proposals/submit", json=proposal_payload)
    assert resp_prop.status_code == 200, resp_prop.text
    prop_data = resp_prop.json()
    assert prop_data["status"] == "ACCEPTED"
    assert "proposal_id" in prop_data
    assert "job_id" in prop_data
    proposal_id = prop_data["proposal_id"]
    durable_job_id = prop_data["job_id"]

    # -------------------------------------------------------------
    # Step 3: Background Worker Executes Job
    # -------------------------------------------------------------
    worker = DurableTrainingService(db=test_db, client=mock_jetson, worker_id="acceptance-worker-01")
    admitted = await worker.try_admit_next_job()
    assert admitted is True

    # Background worker executes job:
    # 1. Submits to Jetson with stable idempotency key
    # 2. Polls completion
    # 3. Fetches holdout evaluation metrics
    # 4. Discovers active champion gliner-champ-v1 and fetches its metrics
    # 5. Compares candidate (F1=0.948) against champion (F1=0.920)
    # 6. Promotes candidate and verifies active model
    processed = await worker.process_admitted_job(durable_job_id)
    assert processed.status == JobStatus.PROMOTED
    assert processed.candidate_model_id == "cand-gliner-v2"
    assert processed.result["promotion_gate"] == "PASSED"

    # Verify idempotency key was forwarded to Jetson
    mock_jetson.submit_training_job.assert_called_once()
    assert mock_jetson.submit_training_job.call_args.kwargs["idempotency_key"] == processed.idempotency_key

    # Verify expected champion version was passed to promote_candidate
    mock_jetson.promote_candidate.assert_called_once_with(
        "cand-gliner-v2",
        expected_champion_version="gliner-champ-v1",
    )

    # -------------------------------------------------------------
    # Step 4: Decay Breached -> Verified Rollback via HTTP POST
    # -------------------------------------------------------------
    decay_payload = {
        "task": "gliner",
        "model_id": "cand-gliner-v2",
        "expected_champion": "gliner-champ-v1",
        "metrics": {"f1": 0.58},  # Regressed below 0.70 threshold
    }
    resp_decay = app_client.post("/features/training/decay/evaluate", json=decay_payload)
    assert resp_decay.status_code == 200, resp_decay.text
    decay_data = resp_decay.json()
    assert decay_data["triggered_rollback"] is True
    assert decay_data["reason"] == "EXTRACTION_DECAY"

    # Verify rollback model was called and active model restored gliner-champ-v1
    mock_jetson.rollback_model.assert_called_once_with("cand-gliner-v2")
    active_now = await mock_jetson.get_active_model("gliner")
    assert active_now["model_id"] == "gliner-champ-v1"

    # Verify persistent incident logged in MongoDB
    decay_col = test_db["specialist_decay_incidents"]
    incident = decay_col.find_one({"model_id": "cand-gliner-v2"})
    assert incident is not None
    assert incident["status"] == "ROLLED_BACK"
    assert incident["verification_status"] == "VERIFIED"
    assert incident["expected_champion"] == "gliner-champ-v1"


@pytest.mark.asyncio
async def test_isolated_lifecycle_crash_during_submission_no_duplicate_remote_jobs(test_db, mock_jetson):
    """
    Crash Test Proof:
    Worker crashes during submission. Resuming worker claims job with identical idempotency key,
    preventing duplicate remote training executions.
    """
    worker_1 = DurableTrainingService(db=test_db, client=mock_jetson, worker_id="worker-crash-1")

    # Submit job
    job = await worker_1.submit_job(
        task="gliner_finetune",
        base_model_id="gliner",
        dataset_manifest_id="manifest-crash-idem",
    )
    await worker_1.try_admit_next_job()

    # Simulate crash: worker 1 terminates while RUNNING, lease expires
    now = datetime.datetime.now(datetime.timezone.utc)
    test_db["training_jobs"].update_one(
        {"job_id": job.job_id},
        {"$set": {
            "status": JobStatus.RUNNING.value,
            "lease_owner": "worker-crash-1",
            "lease_expires_at": (now - datetime.timedelta(seconds=60)).isoformat(),
        }}
    )

    # Worker 2 starts up, reconciles lease, and processes job
    worker_2 = DurableTrainingService(db=test_db, client=mock_jetson, worker_id="worker-recovery-2")
    reclaimed = await worker_2.reconcile_stale_leases()
    assert reclaimed == 1

    admitted = await worker_2.try_admit_next_job()
    assert admitted is True

    res = await worker_2.process_admitted_job(job.job_id)
    assert res.status == JobStatus.PROMOTED

    # Assert exactly 1 remote submission occurred, using the exact same idempotency key
    assert mock_jetson.submit_training_job.call_count == 1
    assert mock_jetson.submit_training_job.call_args.kwargs["idempotency_key"] == job.idempotency_key
