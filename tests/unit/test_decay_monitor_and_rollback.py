"""
Unit tests for Decay Monitor and Automated Rollback Circuit Breaker.
Verifies Item 12:
- Rolling out-of-time evaluation for GLiNER, Market CNN, Timeseries RNN.
- Automated rollback trigger when CNN Brier score > 0.12.
- Automated rollback trigger when RNN interval coverage < 0.65.
- Automated rollback trigger when GLiNER extraction F1 < 0.70.
- Healthy metrics pass without triggering rollback.
- Recording incident details upon rollback execution.
"""

from unittest.mock import AsyncMock, patch
import pytest

from app.services.decay_monitor_service import (
    DecayMonitorService,
    DecayEvaluationResult,
    RollbackTriggerReason,
)


@pytest.fixture
def decay_service():
    mock_client = AsyncMock()
    mock_client.rollback_model.return_value = {
        "status": "ok",
        "message": "Model rolled back to prior champion",
        "active_model": "cnn-champion-v1",
    }
    return DecayMonitorService(feature_client=mock_client)


@pytest.mark.asyncio
async def test_cnn_brier_decay_triggers_rollback(decay_service):
    # CNN Brier score of 0.135 exceeds 0.12 threshold
    eval_data = {
        "task": "market_cnn",
        "model_id": "cnn-canary-v2",
        "metrics": {
            "brier_score": 0.135,
            "macro_f1": 0.81,
            "samples": 120,
        },
    }

    result = await decay_service.record_and_evaluate(eval_data)
    assert result.triggered_rollback is True
    assert result.reason == RollbackTriggerReason.CALIBRATION_DECAY
    assert "0.135" in result.diagnostic_detail
    decay_service.feature_client.rollback_model.assert_awaited_once_with("cnn-canary-v2")


@pytest.mark.asyncio
async def test_rnn_coverage_decay_triggers_rollback(decay_service):
    # RNN interval coverage of 0.58 is below 0.65 threshold
    eval_data = {
        "task": "timeseries_rnn",
        "model_id": "rnn-canary-v2",
        "metrics": {
            "coverage_80": 0.58,
            "rmse": 0.031,
            "samples": 250,
        },
    }

    result = await decay_service.record_and_evaluate(eval_data)
    assert result.triggered_rollback is True
    assert result.reason == RollbackTriggerReason.COVERAGE_DECAY
    assert "0.58" in result.diagnostic_detail
    decay_service.feature_client.rollback_model.assert_awaited_once_with("rnn-canary-v2")


@pytest.mark.asyncio
async def test_gliner_f1_decay_triggers_rollback(decay_service):
    # GLiNER F1 of 0.66 is below 0.70 threshold
    eval_data = {
        "task": "gliner",
        "model_id": "gliner-canary-v2",
        "metrics": {
            "f1": 0.66,
            "precision": 0.68,
            "recall": 0.64,
            "samples": 150,
        },
    }

    result = await decay_service.record_and_evaluate(eval_data)
    assert result.triggered_rollback is True
    assert result.reason == RollbackTriggerReason.EXTRACTION_DECAY
    assert "0.66" in result.diagnostic_detail
    decay_service.feature_client.rollback_model.assert_awaited_once_with("gliner-canary-v2")


@pytest.mark.asyncio
async def test_healthy_models_do_not_trigger_rollback(decay_service):
    # Healthy CNN metrics (Brier = 0.052 <= 0.12)
    cnn_eval = {
        "task": "market_cnn",
        "model_id": "cnn-healthy-v1",
        "metrics": {
            "brier_score": 0.052,
            "macro_f1": 0.91,
            "samples": 200,
        },
    }
    res_cnn = await decay_service.record_and_evaluate(cnn_eval)
    assert res_cnn.triggered_rollback is False
    decay_service.feature_client.rollback_model.assert_not_called()

    # Healthy RNN metrics (coverage = 0.81 >= 0.65)
    rnn_eval = {
        "task": "timeseries_rnn",
        "model_id": "rnn-healthy-v1",
        "metrics": {
            "coverage_80": 0.81,
            "rmse": 0.018,
            "samples": 300,
        },
    }
    res_rnn = await decay_service.record_and_evaluate(rnn_eval)
    assert res_rnn.triggered_rollback is False
    decay_service.feature_client.rollback_model.assert_not_called()


def test_decay_service_startup_default_client():
    """Verifies service initializes gracefully with default singleton feature_client."""
    svc = DecayMonitorService()
    assert svc.feature_client is not None
    assert hasattr(svc.feature_client, "get_model_metrics")


@pytest.mark.asyncio
async def test_timestamp_preserved_in_evaluation_result(decay_service):
    """Verifies the original evaluation timestamp is strictly preserved."""
    original_ts = "2026-09-18T10:30:00+00:00"
    eval_data = {
        "task": "market_cnn",
        "model_id": "cnn-test-v1",
        "metrics": {"brier_score": 0.06, "macro_f1": 0.89},
        "evaluated_at": original_ts,
    }
    res = await decay_service.record_and_evaluate(eval_data)
    assert res.evaluated_at == original_ts
    assert decay_service.history[-1]["evaluated_at"] == original_ts


@pytest.mark.asyncio
async def test_stale_evaluation_marks_degraded(decay_service):
    """Verifies that evidence older than 48 hours results in a visible DEGRADED state."""
    stale_ts = "2026-09-10T00:00:00+00:00"  # Well over 48h old
    eval_data = {
        "task": "market_cnn",
        "model_id": "cnn-stale-v1",
        "metrics": {"brier_score": 0.06, "macro_f1": 0.89},
        "evaluated_at": stale_ts,
    }
    res = await decay_service.record_and_evaluate(eval_data)
    assert res.reason == RollbackTriggerReason.STALE_EVALUATION
    assert decay_service.history[-1]["status"] == "DEGRADED"
    assert "stale" in res.diagnostic_detail.lower()


@pytest.mark.asyncio
async def test_missing_metrics_marks_degraded(decay_service):
    """Verifies that missing or null metrics produce a visible DEGRADED incident."""
    eval_data = {
        "task": "timeseries_rnn",
        "model_id": "rnn-unmonitored-v1",
        "metrics": None,
    }
    res = await decay_service.record_and_evaluate(eval_data)
    assert res.reason == RollbackTriggerReason.MISSING_EVALUATION_DATA
    assert decay_service.history[-1]["status"] == "DEGRADED"


@pytest.mark.asyncio
async def test_scheduled_evaluation_worker_loop(decay_service):
    """Verifies one scheduled evaluation cycle runs against feature_client and logs incidents."""
    import asyncio
    shutdown = asyncio.Event()

    mock_client = decay_service.feature_client
    mock_client.get_active_model = AsyncMock(side_effect=lambda task: f"{task}-active-v1")
    original_ts = "2026-09-18T14:00:00+00:00"
    mock_client.get_model_metrics = AsyncMock(return_value={
        "metrics": {"brier_score": 0.05, "macro_f1": 0.90, "coverage_80": 0.80, "f1": 0.92},
        "evaluated_at": original_ts,
        "sample_count": 200,
    })

    # Trigger shutdown immediately after one loop execution
    async def _trigger_stop():
        await asyncio.sleep(0.05)
        shutdown.set()

    stop_task = asyncio.create_task(_trigger_stop())
    await decay_service.start_worker_loop(poll_interval_seconds=0.01, shutdown_event=shutdown)
    await stop_task

    assert len(decay_service.history) >= 3
    tasks_monitored = {h["task"] for h in decay_service.history}
    assert "gliner" in tasks_monitored
    assert "market_cnn" in tasks_monitored
    assert "timeseries_rnn" in tasks_monitored
    assert decay_service.history[0]["evaluated_at"] == original_ts
