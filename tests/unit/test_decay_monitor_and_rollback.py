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
