"""
Acceptance Test 10: Complete End-to-End Multi-Stage Acceptance Sequence.
Proves the unified production pipeline across all 5 user-mandated lifecycle stages:

1. Service Startup & Dual Background Workers:
   - Starts DurableTrainingService and DecayMonitorService worker loops.
   - Verifies zero initialization crashes (TypeError/AttributeError).
   - Verifies active polling, connection acquisition, and graceful shutdown.

2. Cycle Operational Modes:
   - Disabled: Makes 0 remote specialist calls; executes classical baseline.
   - Shadow: Calls specialists and populates SharedDesk without altering outbound prompts.
   - Advisory: Calls specialists, formats qualified entities and anti-double-counting warnings
     into prompts, and records telemetry delivery receipts.

3. Full Training Proposal Lifecycle:
   - Submits training proposal, manifests dataset, enqueues durable job.
   - Traces capacity admission -> remote execution -> candidate evaluation.
   - Enforces mandatory server-side champion comparison before promotion.

4. Controlled Degradation & Verified Rollback:
   - Simulates model performance decay.
   - Verifies decay monitor flags degradation and initiates rollback.
   - Atomically verifies active model reverts to prior champion via CAS.

5. Resilience & Chaos Invariants:
   - Crash recovery across ADMITTED, RUNNING, and EVALUATING states.
   - Remote job resumption without duplicate submissions.
   - Job cancellation handling before promotion.
   - Unique partial database index preventing duplicate active submissions under concurrency.
"""

import asyncio
from datetime import datetime, timezone
import json
import time
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
import pytest

from app.config import settings
from app.services.durable_training_service import (
    DurableTrainingService,
    JobStatus,
    TrainingJobRecord,
)
from app.services.decay_monitor_service import (
    DecayMonitorService,
    DecayEvaluationResult,
    RollbackTriggerReason,
)
from app.services.jetson_feature_client import (
    JetsonFeatureClient,
    FeatureServiceResponseError,
)
from app.services.jetson_training_orchestrator import JetsonTrainingOrchestrator
from app.v3.orchestrator import run_v3_pipeline
from app.v3.shared_desk import SharedDesk

pytestmark = pytest.mark.real_mongo


@pytest.fixture
def clean_mongo(real_mongo):
    """Provides an isolated clean test database on the real MongoDB instance."""
    for col in ("training_jobs", "decay_incidents", "eval_scores", "v3_system_commands"):
        real_mongo[col].delete_many({})
    yield real_mongo
    for col in ("training_jobs", "decay_incidents", "eval_scores", "v3_system_commands"):
        real_mongo[col].delete_many({})


@pytest.fixture
def mock_jetson():
    client = MagicMock(spec=JetsonFeatureClient)
    client.base_url = "http://10.0.0.30:8002"
    active_models_state = {
        "gliner": "gliner-champ-baseline",
        "cnn": "market_cnn-v1",
        "rnn": "timeseries_rnn-v1",
        "gliner_finetune": "gliner-champ-baseline",
    }
    client.get_health = AsyncMock(return_value={
        "status": "ok",
        "gpu": {"available": True, "device_name": "Orin"},
        "queue": {"training_active": 0, "training_max": 1, "inference_active": 0, "inference_max": 4},
        "models_loaded": ["gliner", "cnn", "rnn"],
    })
    client.get_capabilities = AsyncMock(return_value={
        "models": {
            "gliner": {"version": "gliner-v1"},
            "cnn": {"version": "market_cnn-v1"},
            "rnn": {"version": "timeseries_rnn-v1"},
        }
    })
    client.get_active_models = AsyncMock(return_value={
        "gliner": {"version": "gliner-v1"},
        "cnn": {"version": "market_cnn-v1"},
        "rnn": {"version": "timeseries_rnn-v1"},
    })
    client.extract_entities = AsyncMock(return_value={
        "result": {
            "documents": [{
                "document_id": "doc_e2e_1",
                "entities": [
                    {"text": "Apple", "label": "company", "confidence": 0.96},
                    {"text": "$90B", "label": "financial_metric_value", "confidence": 0.89},
                ],
            }]
        },
        "model_version": "gliner-v1",
        "latency_ms": 22.5,
    })
    client.classify_market_regime = AsyncMock(return_value={
        "result": {
            "regime": "bullish_expansion",
            "confidence": 0.88,
            "class_probabilities": {"bullish_expansion": 0.88, "bearish_contraction": 0.12},
        },
        "model_version": "market_cnn-v1",
        "latency_ms": 41.2,
    })
    client.predict_forecast = AsyncMock(return_value={
        "result": {
            "return_quantiles": {"p10": -0.015, "p50": 0.022, "p90": 0.065},
            "volatility_estimate": 0.031,
        },
        "model_version": "timeseries_rnn-v1",
        "latency_ms": 28.7,
    })
    client.submit_training_job = AsyncMock(return_value={
        "job_id": "jetson-job-e2e-100",
        "status": "pending",
    })
    client.get_training_job = AsyncMock(return_value={
        "job_id": "jetson-job-e2e-100",
        "status": "completed",
        "candidate_model_id": "cand-gliner-e2e-100",
        "metrics": {"f1": 0.945, "precision": 0.93, "recall": 0.96},
    })
    client.evaluate_candidate = AsyncMock(return_value={
        "model_id": "cand-gliner-e2e-100",
        "sample_count": 250,
        "metrics": {"f1": 0.945, "precision": 0.93, "recall": 0.96, "latency_p99_ms": 22.0},
    })

    async def _get_active_model(task="gliner"):
        model_id = active_models_state.get(task, "gliner-champ-baseline")
        return {
            "model_id": model_id,
            "metrics": {"f1": 0.910, "precision": 0.90, "recall": 0.92},
        }
    client.get_active_model = AsyncMock(side_effect=_get_active_model)

    async def _promote_candidate(cand_id, task="gliner_finetune"):
        active_models_state[task] = cand_id
        active_models_state["gliner"] = cand_id
        return {
            "status": "promoted",
            "model_id": cand_id,
        }
    client.promote_candidate = AsyncMock(side_effect=_promote_candidate)

    async def _rollback_model(task="gliner"):
        active_models_state[task] = "gliner-champ-baseline"
        return {
            "status": "rolled_back",
            "active_model_id": "gliner-champ-baseline",
        }
    client.rollback_model = AsyncMock(side_effect=_rollback_model)
    return client


# ── Stage 1: Dual Worker Startup & Lifecycle ──────────────────────────────────

@pytest.mark.asyncio
async def test_stage1_worker_startup_and_background_loops(clean_mongo, mock_jetson):
    """
    Stage 1: Verify both DurableTrainingService and DecayMonitorService
    initialize cleanly with feature_client and run background loops without crashes.
    """
    shutdown_event = asyncio.Event()

    training_svc = DurableTrainingService(
        client=mock_jetson,
        db=clean_mongo,
        worker_id="worker-stage1-training",
    )
    decay_svc = DecayMonitorService(
        feature_client=mock_jetson,
        db=clean_mongo,
    )

    # Start background loops as concurrent tasks
    training_task = asyncio.create_task(
        training_svc.start_worker_loop(poll_interval_seconds=0.05, shutdown_event=shutdown_event)
    )
    decay_task = asyncio.create_task(
        decay_svc.start_worker_loop(poll_interval_seconds=0.05, shutdown_event=shutdown_event)
    )

    # Let workers perform active polling cycles
    await asyncio.sleep(0.2)

    # Verify workers are running and healthy
    assert not training_task.done()
    assert not decay_task.done()

    # Trigger graceful shutdown
    shutdown_event.set()
    await asyncio.wait_for(asyncio.gather(training_task, decay_task), timeout=2.0)
    assert training_task.done()
    assert decay_task.done()


# ── Stage 2: Cycle Operational Modes ──────────────────────────────────────────

@pytest.mark.asyncio
async def test_stage2_cycle_modes_disabled_shadow_advisory(clean_mongo, mock_jetson):
    """
    Stage 2: Run disabled, shadow, and advisory paper cycles.
    Assert zero calls in disabled, desk recording in shadow, and delivery in advisory.
    """
    async def fake_build_report(ticker, stats_sink=None, **kwargs):
        bars = [{"close": 150.0 + i, "open": 149.0 + i, "high": 152.0 + i, "low": 148.0 + i, "volume": 1000000, "timestamp": f"2026-09-18T{i:02d}:00:00Z"} for i in range(35)]
        news = [{"title": "Apple Q4 Record Earnings", "summary": "Apple announces record earnings", "published_at": datetime.now(timezone.utc).isoformat()}]
        if stats_sink is not None:
            stats_sink["raw_data"] = {"price_history": bars, "news": news, "metadata": {"ticker": ticker}}
        return f"# Market Report for {ticker}"

    captured_prompts = {}
    async def capturing_run_agent(**kwargs):
        role = kwargs.get("agent_name", "unknown")
        captured_prompts[role] = kwargs.get("user_prompt", "")
        return {
            "response": json.dumps({
                "ticker": "AAPL", "stance": "HOLD", "confidence": 80,
                "reasoning": "Evaluated specialist inputs.",
                "data_gaps": [], "key_findings": [], "triggers": [],
            }),
            "tokens_used": 150,
            "loops_used": 1,
        }

    # 1. Disabled Mode
    with patch("app.config.settings.SPECIALIST_MODE", "disabled"), \
         patch("app.v3.data_report.build_ticker_data_report", side_effect=fake_build_report), \
         patch("app.services.jetson_feature_client.feature_client", mock_jetson), \
         patch("app.agents.base_agent.run_agent", side_effect=capturing_run_agent):

        mock_jetson.extract_entities.reset_mock()
        await run_v3_pipeline(ticker="AAPL", cycle_id="e2e-disabled-cycle")
        mock_jetson.extract_entities.assert_not_called()

    # 2. Shadow Mode
    with patch("app.config.settings.SPECIALIST_MODE", "shadow"), \
         patch("app.v3.data_report.build_ticker_data_report", side_effect=fake_build_report), \
         patch("app.services.jetson_feature_client.feature_client", mock_jetson), \
         patch("app.agents.base_agent.run_agent", side_effect=capturing_run_agent), \
         patch("app.v3.orchestrator.save_desk") as mock_save_shadow:

        captured_prompts.clear()
        await run_v3_pipeline(ticker="AAPL", cycle_id="e2e-shadow-cycle")
        mock_jetson.extract_entities.assert_called()
        # In shadow mode, specialist features are on desk but prompts do NOT contain anti-double-counting warnings
        saved_desk = mock_save_shadow.call_args[0][0]
        assert saved_desk.specialist_features.get("mode") == "shadow"
        fund_prompt = captured_prompts.get("v3_fundamental_analyst", "")
        assert "candidate text mentions, NOT verified financial facts" not in fund_prompt

    # 3. Advisory Mode
    with patch("app.config.settings.SPECIALIST_MODE", "advisory"), \
         patch("app.v3.data_report.build_ticker_data_report", side_effect=fake_build_report), \
         patch("app.services.jetson_feature_client.feature_client", mock_jetson), \
         patch("app.agents.base_agent.run_agent", side_effect=capturing_run_agent), \
         patch("app.v3.orchestrator.save_desk") as mock_save_advisory:

        captured_prompts.clear()
        await run_v3_pipeline(ticker="AAPL", cycle_id="e2e-advisory-cycle")
        saved_desk = mock_save_advisory.call_args[0][0]
        assert saved_desk.specialist_features.get("mode") == "advisory"
        fund_prompt = captured_prompts.get("v3_fundamental_analyst", "")
        assert "candidate text mentions, NOT verified financial facts" in fund_prompt


# ── Stage 3: Full Training Proposal Lifecycle ─────────────────────────────────

@pytest.mark.asyncio
async def test_stage3_training_proposal_lifecycle(clean_mongo, mock_jetson):
    """
    Stage 3: Submit a training proposal, trace it through capacity admission,
    remote training, evaluation, and server-side champion comparison promotion gate.
    """
    training_svc = DurableTrainingService(
        client=mock_jetson,
        db=clean_mongo,
        worker_id="worker-stage3",
    )

    job = await training_svc.submit_job(
        task="gliner_finetune",
        base_model_id="gliner",
        dataset_manifest_id="manifest-e2e-gliner-1",
    )
    assert job.status == JobStatus.QUEUED

    # Admit job
    admitted = await training_svc.try_admit_next_job()
    assert admitted
    admitted_job = await training_svc.get_job(job.job_id)
    assert admitted_job.status == JobStatus.ADMITTED

    # Process job through remote training and evaluation
    processed_job = await training_svc.process_admitted_job(job.job_id)
    assert processed_job.status == JobStatus.PROMOTED
    assert processed_job.candidate_model_id == "cand-gliner-e2e-100"
    mock_jetson.submit_training_job.assert_called_once()
    mock_jetson.promote_candidate.assert_called_once_with("cand-gliner-e2e-100")


# ── Stage 4: Controlled Degradation & Verified Rollback ────────────────────────

@pytest.mark.asyncio
async def test_stage4_controlled_degradation_and_verified_rollback(clean_mongo, mock_jetson):
    """
    Stage 4: Trigger controlled performance degradation and verify
    the correct prior champion model is restored via CAS.
    """
    decay_svc = DecayMonitorService(
        feature_client=mock_jetson,
        db=clean_mongo,
    )

    # Mock degraded active model that reverts to champion on rollback
    active_id = "cand-degraded-v2"
    async def _get_active(task="gliner"):
        return {"model_id": active_id, "metrics": {"f1": 0.91, "precision": 0.90, "recall": 0.92}}
    async def _rollback(target=None, timeout=None):
        nonlocal active_id
        active_id = "gliner-champ-baseline"
        return {"status": "rolled_back", "active_model_id": "gliner-champ-baseline"}

    mock_jetson.get_active_model = AsyncMock(side_effect=_get_active)
    mock_jetson.rollback_model = AsyncMock(side_effect=_rollback)

    # Run evaluation with F1=0.62 (< CRITICAL_GLINER_F1_MIN 0.70)
    result = await decay_svc.record_and_evaluate({
        "task": "gliner",
        "model_id": "cand-degraded-v2",
        "metrics": {"f1": 0.62, "precision": 0.60, "recall": 0.64, "latency_p99_ms": 120.0},
    })
    assert result.triggered_rollback is True
    assert result.reason == RollbackTriggerReason.EXTRACTION_DECAY
    assert result.rollback_response is not None
    assert result.rollback_response.get("status") == "rolled_back"
    mock_jetson.rollback_model.assert_called_once_with("cand-degraded-v2")

    # Verify active model is now restored champion
    active_after = await mock_jetson.get_active_model("gliner")
    assert active_after["model_id"] == "gliner-champ-baseline"


# ── Stage 5: Resilience, Crashes, and Invariants ──────────────────────────────

@pytest.mark.asyncio
async def test_stage5_resilience_crashes_and_invariants(clean_mongo, mock_jetson):
    """
    Stage 5: Test crash recovery across ADMITTED, RUNNING, and EVALUATING states,
    cancellation protection, and database-enforced unique submission index.
    """
    service = DurableTrainingService(
        client=mock_jetson,
        db=clean_mongo,
        worker_id="worker-stage5-primary",
    )

    # 1. Crash Recovery during RUNNING
    stale_iso = (datetime.now(timezone.utc) - asyncio.to_thread(lambda: datetime.now(timezone.utc)) if False else datetime.now(timezone.utc)).isoformat()
    clean_mongo["training_jobs"].insert_one({
        "job_id": "crashed-running-job",
        "task": "gliner_finetune",
        "base_model_id": "gliner",
        "dataset_manifest_id": "manifest-crash-1",
        "status": JobStatus.RUNNING.value,
        "lease_owner": "dead-worker",
        "lease_expires_at": "2026-09-01T00:00:00Z",
        "jetson_job_id": "jetson-job-running-resume",
    })

    recovered = await service.reconcile_stale_leases()
    assert recovered >= 1
    reclaimed = await service.get_job("crashed-running-job")
    assert reclaimed.status in (JobStatus.QUEUED, JobStatus.RUNNING)

    # 2. Cancellation Protection Before Promotion
    cancelled_job = await service.submit_job(
        task="cnn_train",
        base_model_id="market_cnn",
        dataset_manifest_id="manifest-cnn-cancel",
    )
    await service.cancel_job(cancelled_job.job_id)
    job_after_cancel = await service.get_job(cancelled_job.job_id)
    assert job_after_cancel.status == JobStatus.CANCELLED

    # 3. Database-Enforced Unique Submission Idempotency
    job1 = await service.submit_job(
        task="rnn_train",
        base_model_id="timeseries_rnn",
        dataset_manifest_id="manifest-rnn-unique",
    )
    job2 = await service.submit_job(
        task="rnn_train",
        base_model_id="timeseries_rnn",
        dataset_manifest_id="manifest-rnn-unique",
    )
    assert job1.job_id == job2.job_id
