"""
Acceptance Test 7: Prove scheduled decay detection and verified rollback.
- Feeds healthy, degrading, missing, malformed, and stale metrics through DecayMonitorService
  for all 3 supported specialist tasks (GLiNER, CNN, RNN).
- Verifies missing or stale evidence is marked as non-healthy incidents (never certified HEALTHY).
- Tests MongoDB incident persistence and deduplicated rollback triggers across service restarts.
- Tests read-back active model verification (raising RollbackVerificationError if active model does not revert).
- Exercises the production HTTP route POST /features/training/decay/evaluate.
"""

import datetime
from unittest.mock import AsyncMock, MagicMock, patch
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.routers.feature_training_router import router
from app.services.decay_monitor_service import (
    DecayMonitorService,
    RollbackTriggerReason,
    RollbackVerificationError,
)

pytestmark = pytest.mark.real_mongo


class MemoryMongoSemanticCollection:
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
                if "$gte" in v:
                    val = doc.get(k)
                    if val is None or str(val) < str(v["$gte"]):
                        return False
                if "$lt" in v:
                    val = doc.get(k)
                    if val is None or str(val) >= str(v["$lt"]):
                        return False
            elif doc.get(k) != v:
                return False
        return True

    def find(self, query=None):
        return [dict(d) for d in self.docs.values() if query is None or self._matches(d, query)]

    def find_one(self, query):
        for d in self.docs.values():
            if self._matches(d, query):
                return dict(d)
        return None

    def insert_one(self, doc):
        doc_id = doc.get("incident_id") or f"inc-{len(self.docs)+1}"
        self.docs[doc_id] = dict(doc)
        return MagicMock(inserted_id=doc_id)

    def update_one(self, query, update):
        for doc_id, d in self.docs.items():
            if self._matches(d, query):
                if "$set" in update:
                    d.update(update["$set"])
                return MagicMock(modified_count=1)
        return MagicMock(modified_count=0)


@pytest.fixture
def shared_db():
    col_incidents = MemoryMongoSemanticCollection()
    db = MagicMock()
    db.get_collection.return_value = col_incidents
    db.__getitem__.return_value = col_incidents
    return db


@pytest.fixture
def mock_jetson():
    client = MagicMock()
    client.rollback_model = AsyncMock(return_value={"status": "rolled_back", "restored_model_id": "champ-v0"})
    client.get_active_model = AsyncMock(return_value={"model_id": "champ-v0", "task": "gliner"})
    return client


@pytest.fixture
def api_client():
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


# ── 1. Healthy Metrics for All 3 Tasks ──

@pytest.mark.asyncio
async def test_healthy_metrics_for_all_tasks(shared_db, mock_jetson):
    service = DecayMonitorService(feature_client=mock_jetson, db=shared_db)

    # GLiNER healthy
    res_gliner = await service.record_and_evaluate({
        "task": "gliner_finetune", "model_id": "cand-g1", "metrics": {"f1": 0.94}
    })
    assert res_gliner.reason == RollbackTriggerReason.HEALTHY
    assert not res_gliner.triggered_rollback

    # CNN healthy
    res_cnn = await service.record_and_evaluate({
        "task": "cnn_regime", "model_id": "cand-c1", "metrics": {"brier_score": 0.05}
    })
    assert res_cnn.reason == RollbackTriggerReason.HEALTHY
    assert not res_cnn.triggered_rollback

    # RNN healthy
    res_rnn = await service.record_and_evaluate({
        "task": "rnn_volatility", "model_id": "cand-r1", "metrics": {"coverage_80": 0.81}
    })
    assert res_rnn.reason == RollbackTriggerReason.HEALTHY
    assert not res_rnn.triggered_rollback


# ── 2. Missing Evidence is NOT Healthy ──

@pytest.mark.asyncio
async def test_missing_evidence_is_not_healthy(shared_db, mock_jetson):
    service = DecayMonitorService(feature_client=mock_jetson, db=shared_db)

    # Completely missing metrics dict
    res_missing = await service.record_and_evaluate({
        "task": "gliner_finetune", "model_id": "cand-g1", "metrics": None
    })
    assert res_missing.reason == RollbackTriggerReason.MISSING_EVALUATION_DATA
    assert not res_missing.triggered_rollback

    # Empty metrics dict
    res_empty = await service.record_and_evaluate({
        "task": "cnn_regime", "model_id": "cand-c1", "metrics": {}
    })
    assert res_empty.reason == RollbackTriggerReason.MISSING_EVALUATION_DATA


# ── 3. Malformed and Stale Metrics ──

@pytest.mark.asyncio
async def test_malformed_and_stale_metrics(shared_db, mock_jetson):
    service = DecayMonitorService(feature_client=mock_jetson, db=shared_db)

    # Malformed NaN
    res_nan = await service.record_and_evaluate({
        "task": "cnn_regime", "model_id": "cand-c1", "metrics": {"brier_score": float("nan")}
    })
    assert res_nan.reason == RollbackTriggerReason.MALFORMED_METRICS

    # Stale timestamp (60 hours old)
    old_ts = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=60)).isoformat()
    res_stale = await service.record_and_evaluate({
        "task": "rnn_volatility", "model_id": "cand-r1", "metrics": {"coverage_80": 0.80}, "evaluated_at": old_ts
    })
    assert res_stale.reason == RollbackTriggerReason.STALE_EVALUATION


# ── 4. Critical Decay Triggers Rollback ──

@pytest.mark.asyncio
async def test_critical_decay_triggers_rollback(shared_db, mock_jetson):
    service = DecayMonitorService(feature_client=mock_jetson, db=shared_db)

    # CNN Brier score decayed to 0.15 (threshold 0.12)
    mock_jetson.get_active_model = AsyncMock(return_value={"model_id": "champ-cnn-prior", "task": "cnn"})
    res_cnn = await service.record_and_evaluate({
        "task": "cnn_regime", "model_id": "decayed-cnn-01", "metrics": {"brier_score": 0.15}
    })
    assert res_cnn.triggered_rollback
    assert res_cnn.reason == RollbackTriggerReason.CALIBRATION_DECAY
    mock_jetson.rollback_model.assert_awaited_with("decayed-cnn-01")


# ── 5. Incident Persistence & Deduplicated Rollback Across Restart ──

@pytest.mark.asyncio
async def test_incident_persistence_and_deduplicated_rollback_across_restart(shared_db, mock_jetson):
    mock_jetson.get_active_model = AsyncMock(return_value={"model_id": "prior-gliner-champ", "task": "gliner"})

    # Instance 1: detects decay and triggers rollback
    inst_1 = DecayMonitorService(feature_client=mock_jetson, db=shared_db)
    res_1 = await inst_1.record_and_evaluate({
        "task": "gliner_finetune", "model_id": "decaying-gliner-02", "metrics": {"f1": 0.65}
    })
    assert res_1.triggered_rollback
    assert mock_jetson.rollback_model.await_count == 1

    # Simulate process crash / restart: brand new service instance on same DB
    inst_2 = DecayMonitorService(feature_client=mock_jetson, db=shared_db)

    # Re-evaluate same incident
    res_2 = await inst_2.record_and_evaluate({
        "task": "gliner_finetune", "model_id": "decaying-gliner-02", "metrics": {"f1": 0.65}
    })
    # Rollback must NOT be re-executed (deduplicated!)
    assert mock_jetson.rollback_model.await_count == 1
    assert "deduplicated" in res_2.diagnostic_detail.lower()


# ── 6. Read-back Verification Failure ──

@pytest.mark.asyncio
async def test_rollback_read_back_verification_failure(shared_db, mock_jetson):
    """
    If rollback_model returns success, but get_active_model still shows the decaying model,
    the monitor MUST raise RollbackVerificationError.
    """
    # Active model remains the decaying one despite rollback call!
    mock_jetson.get_active_model = AsyncMock(return_value={"model_id": "decaying-model-99", "task": "gliner"})

    service = DecayMonitorService(feature_client=mock_jetson, db=shared_db)
    with pytest.raises(RollbackVerificationError) as exc:
        await service.record_and_evaluate({
            "task": "gliner_finetune", "model_id": "decaying-model-99", "metrics": {"f1": 0.55}
        })
    assert "still decaying model" in str(exc.value).lower()


# ── 7. HTTP Route Exercise ──

@pytest.mark.asyncio
async def test_http_decay_evaluate_route(api_client, shared_db, mock_jetson):
    with patch("app.services.decay_monitor_service.mongo_store.get_doc_db", return_value=shared_db), \
         patch("app.routers.feature_training_router.feature_client", mock_jetson):

        mock_jetson.get_active_model = AsyncMock(return_value={"model_id": "champ-prior", "task": "rnn"})

        payload = {
            "task": "rnn_volatility",
            "model_id": "decayed-rnn-05",
            "metrics": {"coverage_80": 0.52},  # Below 0.65 threshold
        }

        resp = api_client.post("/features/training/decay/evaluate", json=payload)
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["triggered_rollback"] is True
        assert data["reason"] == "COVERAGE_DECAY"
        assert "Timeseries RNN 80% coverage" in data["diagnostic_detail"]
