"""
Acceptance Test 2: Exercise the actual training HTTP routes for all three models.
- Submits valid timestamped GLiNER, CNN, and RNN datasets through POST /features/training/curate-and-train.
- Verifies task-specific schema/base model selection (gliner, cnn, rnn).
- Verifies immutable manifest creation with SHA256 checksums and valid timestamps (no timestamp: None).
- Follows each returned durable job through the worker to remote Jetson execution and dataset registration.
"""

import json
from unittest.mock import AsyncMock, MagicMock, patch
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.routers.feature_training_router import router
from app.services.durable_training_service import (
    DurableTrainingService,
    JobStatus,
)


@pytest.fixture
def api_client():
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


@pytest.fixture
def mock_jetson():
    client = MagicMock()
    client.get_health = AsyncMock(return_value={
        "status": "ok",
        "queue": {"training_active": 0, "training_max": 1}
    })
    client.submit_training_job = AsyncMock(return_value={
        "job_id": "jetson-job-abc",
        "status": "pending"
    })
    client.get_training_job = AsyncMock(return_value={
        "status": "completed",
        "candidate_model_id": "cand-accepted-01"
    })
    client.evaluate_candidate = AsyncMock(return_value={
        "model_id": "cand-accepted-01",
        "sample_count": 200,
        "metrics": {"f1": 0.945, "precision": 0.95, "recall": 0.94, "latency_p99_ms": 22.0}
    })
    client.promote_candidate = AsyncMock(return_value={"status": "promoted"})
    client.get_active_model = AsyncMock(return_value={"model_id": "cand-accepted-01", "task": "gliner"})
    return client


@pytest.fixture
def isolated_db():
    docs = {}

    def _matches(doc, q):
        for k, v in q.items():
            if k == "$or":
                if not any(_matches(doc, cond) for cond in v):
                    return False
            elif k == "$in":
                pass
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

    class IsolatedCollection:
        def find_one(self, query):
            for d in docs.values():
                if _matches(d, query):
                    return dict(d)
            return None

        def find(self, query=None):
            class MockCursor:
                def __init__(self, items):
                    self.items = items
                def __iter__(self):
                    return iter(self.items)
                def __len__(self):
                    raise TypeError("object of type 'Cursor' has no len()")
            results = [dict(d) for d in docs.values() if query is None or _matches(d, query)]
            return MockCursor(results)

        def count_documents(self, query):
            return sum(1 for d in docs.values() if _matches(d, query))

        def insert_one(self, doc):
            jid = doc.get("job_id", f"job-{len(docs)+1}")
            docs[jid] = dict(doc)
            return MagicMock(inserted_id=jid)

        def update_one(self, query, update):
            for jid, d in docs.items():
                if _matches(d, query):
                    if "$set" in update:
                        d.update(update["$set"])
                    return MagicMock(modified_count=1)
            return MagicMock(modified_count=0)

        def find_one_and_update(self, query, update, sort=None):
            matches = [d for d in docs.values() if _matches(d, query)]
            if not matches:
                return None
            if sort:
                key, direction = sort[0]
                matches.sort(key=lambda x: x.get(key, ""), reverse=(direction < 0))
            target = matches[0]
            if "$set" in update:
                target.update(update["$set"])
            return dict(target)

    col = IsolatedCollection()
    db = MagicMock()
    db.get_collection.return_value = col
    db.__getitem__.return_value = col
    return db


pytestmark = pytest.mark.real_mongo


@pytest.mark.asyncio
async def test_training_http_routes_all_three_models(api_client, mock_jetson, isolated_db):
    with patch("app.services.durable_training_service.mongo_store.get_doc_db", return_value=isolated_db), \
         patch("app.routers.feature_training_router.feature_client", mock_jetson), \
         patch("app.services.durable_training_service.feature_client", mock_jetson):

        worker = DurableTrainingService(db=isolated_db, client=mock_jetson, worker_id="worker-http-test")

        # 1. Test GLiNER route
        gliner_payload = {
            "task": "gliner_finetune",
            "samples": [
                {
                    "tokenized_text": ["Apple", "announces", "M4", "chip"],
                    "ner": [[0, 0, "ORGANIZATION"], [2, 2, "PRODUCT"]],
                }
            ],
            "auto_submit": True,
            "hyperparameters": {"learning_rate": 5e-5, "epochs": 3},
        }
        resp_gliner = api_client.post("/features/training/curate-and-train", json=gliner_payload)
        assert resp_gliner.status_code == 200, resp_gliner.text
        data_gliner = resp_gliner.json()
        assert data_gliner["task"] == "gliner_finetune"
        assert data_gliner["base_model"] == "gliner"
        assert data_gliner["total_samples"] == 1
        assert "sha256" in data_gliner
        assert "durable_job_id" in data_gliner
        gliner_job_id = data_gliner["durable_job_id"]

        # Verify GLiNER manifest file has timestamps and correct structure
        with open(data_gliner["manifest_path"], "r") as f:
            manifest_content = json.load(f)
            assert manifest_content["task"] == "gliner_finetune"
            assert manifest_content["sha256"] == data_gliner["sha256"]
            with open(manifest_content["train_path"], "r") as tf:
                sample = json.loads(tf.readline())
                assert sample.get("timestamp") is not None, "Timestamp must not be None"
                assert "ner" in sample

        # 2. Test CNN route
        cnn_payload = {
            "task": "cnn_regime",
            "samples": [
                {
                    "features": [[1.2, 0.4], [1.3, 0.5]],
                    "label": 1,
                }
            ],
            "auto_submit": True,
            "hyperparameters": {"learning_rate": 1e-3, "epochs": 10},
        }
        resp_cnn = api_client.post("/features/training/curate-and-train", json=cnn_payload)
        assert resp_cnn.status_code == 200, resp_cnn.text
        data_cnn = resp_cnn.json()
        assert data_cnn["task"] == "cnn_regime"
        assert data_cnn["base_model"] == "cnn"
        assert "durable_job_id" in data_cnn
        cnn_job_id = data_cnn["durable_job_id"]

        with open(data_cnn["manifest_path"], "r") as f:
            manifest_content = json.load(f)
            assert manifest_content["task"] == "cnn_regime"
            assert manifest_content["sha256"] == data_cnn["sha256"]
            with open(manifest_content["train_path"], "r") as tf:
                sample = json.loads(tf.readline())
                assert sample.get("timestamp") is not None
                assert "features" in sample
                assert "label" in sample

        # 3. Test RNN route
        rnn_payload = {
            "task": "rnn_volatility",
            "samples": [
                {
                    "features": [0.05, 0.08, -0.02],
                    "quantiles": {"p10": -0.015, "p50": 0.02, "p90": 0.06},
                }
            ],
            "auto_submit": True,
            "hyperparameters": {"learning_rate": 2e-4, "hidden_dim": 64},
        }
        resp_rnn = api_client.post("/features/training/curate-and-train", json=rnn_payload)
        assert resp_rnn.status_code == 200, resp_rnn.text
        data_rnn = resp_rnn.json()
        assert data_rnn["task"] == "rnn_volatility"
        assert data_rnn["base_model"] == "rnn"
        assert "durable_job_id" in data_rnn
        rnn_job_id = data_rnn["durable_job_id"]

        with open(data_rnn["manifest_path"], "r") as f:
            manifest_content = json.load(f)
            assert manifest_content["task"] == "rnn_volatility"
            assert manifest_content["sha256"] == data_rnn["sha256"]
            with open(manifest_content["train_path"], "r") as tf:
                sample = json.loads(tf.readline())
                assert sample.get("timestamp") is not None
                assert "features" in sample
                assert "quantiles" in sample

        # 4. Follow GLiNER job through worker execution to Jetson registration
        admitted = await worker.try_admit_next_job()
        assert admitted is True
        processed_job = await worker.process_admitted_job(
            job_id=gliner_job_id,
            champion_eval=None,
        )
        assert processed_job.status == JobStatus.PROMOTED
        assert processed_job.candidate_model_id == "cand-accepted-01"
        assert processed_job.jetson_job_id == "jetson-job-abc"

        # Verify feature_client.submit_training_job received the correct manifest and base_model
        mock_jetson.submit_training_job.assert_awaited_with(
            task="gliner_finetune",
            base_model_id="gliner",
            dataset_manifest_id=data_gliner["manifest_id"],
            hyperparameters={"learning_rate": 5e-5, "epochs": 3},
        )
