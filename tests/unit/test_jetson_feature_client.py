"""
Unit tests for JetsonFeatureClient and FeatureLineageStore.

Verifies contract compliance with Jetson Feature Platform on port 8002:
- Bounded timeouts & retries
- Circuit breaker trip and recovery
- Input hashing and request envelope construction
- Response parsing and typed error translation
- MongoDB feature lineage recording and query isolation
"""

import asyncio
import json
import secrets
import time
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from app.services.jetson_feature_client import (
    CircuitBreaker,
    CircuitState,
    DEFAULT_GLINER_LABELS,
    FeatureServiceCircuitOpenError,
    FeatureServiceConnectionError,
    FeatureServiceError,
    FeatureServiceResponseError,
    FeatureServiceTimeoutError,
    JetsonFeatureClient,
)
from app.services import feature_lineage_store


@pytest.fixture
def test_api_key():
    return f"test_key_{secrets.token_hex(8)}"


@pytest.fixture
def client(test_api_key):
    return JetsonFeatureClient(
        base_url="http://10.0.0.30:8002",
        api_key=test_api_key,
        timeout=1.5,
        max_retries=1,
        trip_count=2,
        reset_seconds=0.5,
    )


def test_input_hash_deterministic(client):
    data1 = {"b": 2, "a": 1}
    data2 = {"a": 1, "b": 2}
    hash1 = client.compute_input_hash(data1)
    hash2 = client.compute_input_hash(data2)
    assert hash1 == hash2
    assert len(hash1) == 64


def test_headers_injection(client, test_api_key):
    trace_id = f"trace_{secrets.token_hex(4)}"
    span_id = f"span_{secrets.token_hex(4)}"
    headers = client._headers(trace_id=trace_id, span_id=span_id)
    assert headers["Authorization"] == f"Bearer {test_api_key}"
    assert headers["X-Trace-Id"] == trace_id
    assert headers["X-Span-Id"] == span_id
    assert headers["Content-Type"] == "application/json"


@pytest.mark.asyncio
async def test_get_health_success(client):
    mock_resp = MagicMock()
    mock_resp.is_success = True
    mock_resp.json.return_value = {
        "status": "healthy",
        "gpu_available": True,
        "models_loaded": ["gliner", "market_cnn", "timeseries_rnn"],
    }

    with patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get:
        mock_get.return_value = mock_resp
        health = await client.get_health()
        assert health["status"] == "healthy"
        assert health["gpu_available"] is True


@pytest.mark.asyncio
async def test_get_capabilities_caching(client):
    mock_resp = MagicMock()
    mock_resp.is_success = True
    mock_resp.json.return_value = {
        "tasks": ["entities", "market-regime", "forecast"],
        "schema_version": "1",
    }

    with patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get:
        mock_get.return_value = mock_resp
        # First call fetches from network
        cap1 = await client.get_capabilities()
        assert "entities" in cap1["tasks"]
        assert mock_get.call_count == 1

        # Second call returns from in-memory cache
        cap2 = await client.get_capabilities()
        assert cap2 == cap1
        assert mock_get.call_count == 1  # No additional network hit

        # Force refresh fetches from network
        cap3 = await client.get_capabilities(force_refresh=True)
        assert mock_get.call_count == 2


@pytest.mark.asyncio
async def test_extract_entities_success(client):
    req_id = f"req_{secrets.token_hex(4)}"
    mock_resp = MagicMock()
    mock_resp.is_success = True
    mock_resp.json.return_value = {
        "request_id": req_id,
        "model_id": "gliner",
        "model_version": "gliner-trading-v1",
        "schema_version": "1",
        "input_hash": "testhash",
        "created_at": "2026-09-18T18:00:00Z",
        "latency_ms": 35,
        "result": {
            "doc1": [
                {
                    "span": "NVDA",
                    "start_offset": 0,
                    "end_offset": 4,
                    "label": "ticker",
                    "score": 0.99,
                },
                {
                    "span": "raised full-year guidance",
                    "start_offset": 20,
                    "end_offset": 45,
                    "label": "event_trigger",
                    "score": 0.94,
                },
            ]
        },
    }

    with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
        mock_post.return_value = mock_resp
        docs = [{"document_id": "doc1", "text": "NVDA shares rose as raised full-year guidance was announced."}]
        res = await client.extract_entities(documents=docs, threshold=0.8)

        assert res["request_id"] == req_id
        assert res["model_id"] == "gliner"
        assert "doc1" in res["result"]
        assert len(res["result"]["doc1"]) == 2
        assert res["result"]["doc1"][0]["label"] == "ticker"
        assert client.circuit_breaker.state == CircuitState.CLOSED


@pytest.mark.asyncio
async def test_circuit_breaker_tripping_and_recovery(client):
    # trip_count = 2, reset_seconds = 0.5
    with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
        mock_post.side_effect = httpx.ConnectError("Connection refused")

        # Failure 1
        with pytest.raises(FeatureServiceConnectionError):
            await client.extract_entities(documents=[{"document_id": "1", "text": "test"}])
        assert client.circuit_breaker.state == CircuitState.CLOSED
        assert client.circuit_breaker.failure_count == 1

        # Failure 2 -> should trip circuit to OPEN
        with pytest.raises(FeatureServiceConnectionError):
            await client.extract_entities(documents=[{"document_id": "1", "text": "test"}])
        assert client.circuit_breaker.state == CircuitState.OPEN

        # Immediate request while OPEN should be blocked without network attempt
        mock_post.reset_mock()
        with pytest.raises(FeatureServiceCircuitOpenError) as exc_info:
            await client.extract_entities(documents=[{"document_id": "1", "text": "test"}])
        assert "circuit breaker is OPEN" in str(exc_info.value)
        assert mock_post.call_count == 0

        # Wait for cooldown reset_seconds (0.5s)
        await asyncio.sleep(0.55)

        # Probe request allowed (HALF_OPEN). Let it succeed.
        mock_success = MagicMock()
        mock_success.is_success = True
        mock_success.json.return_value = {
            "request_id": "probe",
            "model_id": "gliner",
            "model_version": "v1",
            "schema_version": "1",
            "input_hash": "hash",
            "result": {},
        }
        mock_post.side_effect = None
        mock_post.return_value = mock_success

        res = await client.extract_entities(documents=[{"document_id": "1", "text": "test"}])
        assert res["request_id"] == "probe"
        # Circuit is now reset to CLOSED
        assert client.circuit_breaker.state == CircuitState.CLOSED
        assert client.circuit_breaker.failure_count == 0


@pytest.mark.asyncio
async def test_typed_error_rejection_no_retry_on_4xx(client):
    mock_resp = MagicMock()
    mock_resp.is_success = False
    mock_resp.status_code = 400
    mock_resp.json.return_value = {
        "error_code": "UNSUPPORTED_SCHEMA",
        "detail": "Schema version 99 not supported",
    }

    with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
        mock_post.return_value = mock_resp
        with pytest.raises(FeatureServiceResponseError) as exc_info:
            await client.extract_entities(documents=[])

        assert exc_info.value.error_code == "UNSUPPORTED_SCHEMA"
        assert exc_info.value.status_code == 400
        # 4xx error should NOT retry
        assert mock_post.call_count == 1


@pytest.mark.asyncio
async def test_classify_market_regime_payload_contract(client):
    mock_resp = MagicMock()
    mock_resp.is_success = True
    mock_resp.json.return_value = {
        "request_id": "cnn-1",
        "model_id": "market_cnn",
        "model_version": "cnn-regime-v1",
        "schema_version": "1",
        "input_hash": "hash-cnn",
        "result": {
            "regime": "range_bound",
            "class_probabilities": {"range_bound": 0.82, "breakout_candidate": 0.12},
            "confidence": 0.82,
            "ood_score": 0.01,
            "input_quality": "valid",
        },
    }

    with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
        mock_post.return_value = mock_resp
        res = await client.classify_market_regime(
            instrument_id="NVDA",
            bar_interval="1d",
            window_end="2026-09-18T20:00:00Z",
            lookback_bars=128,
            ohlcv=[{"open": 100, "high": 105, "low": 98, "close": 102, "volume": 1000}],
        )
        assert res["model_id"] == "market_cnn"
        assert res["result"]["regime"] == "range_bound"
        assert res["result"]["confidence"] == 0.82

        # Verify sent payload
        call_args = mock_post.call_args[1]
        sent_json = call_args["json"]
        assert sent_json["instrument_id"] == "NVDA"
        assert sent_json["lookback_bars"] == 128
        assert "input_hash" in sent_json


@pytest.mark.asyncio
async def test_predict_forecast_payload_contract(client):
    mock_resp = MagicMock()
    mock_resp.is_success = True
    mock_resp.json.return_value = {
        "request_id": "rnn-1",
        "model_id": "timeseries_rnn",
        "model_version": "rnn-forecast-v1",
        "schema_version": "1",
        "input_hash": "hash-rnn",
        "result": {
            "horizon": "5d",
            "return_quantiles": {"p10": -0.05, "p50": 0.02, "p90": 0.08},
            "volatility_estimate": 0.035,
            "direction_probability_up": 0.61,
            "prediction_interval_coverage_target": 0.80,
            "ood_score": 0.0,
        },
    }

    with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
        mock_post.return_value = mock_resp
        res = await client.predict_forecast(
            instrument_id="AAPL",
            bar_interval="1d",
            cutoff="2026-09-18T20:00:00Z",
            lookback_bars=60,
        )
        assert res["model_id"] == "timeseries_rnn"
        assert res["result"]["horizon"] == "5d"
        assert res["result"]["return_quantiles"]["p50"] == 0.02


def test_feature_lineage_store_recording_and_querying():
    mock_db = MagicMock()
    mock_col = MagicMock()
    mock_db.__getitem__.return_value = mock_col

    with patch("app.db.mongo_store.get_doc_db", return_value=mock_db):
        feature_id = feature_lineage_store.record_feature(
            cycle_id="cycle-test-101",
            instrument_id="NVDA",
            model_id="gliner",
            payload={"entities": [{"span": "NVDA", "label": "ticker"}]},
            input_hash="hash123",
            document_id="doc-abc",
            mode="shadow",
        )
        assert feature_id.startswith("feat-")
        assert mock_col.insert_one.call_count == 1
        inserted_doc = mock_col.insert_one.call_args[0][0]
        assert inserted_doc["cycle_id"] == "cycle-test-101"
        assert inserted_doc["instrument_id"] == "NVDA"
        assert inserted_doc["model_id"] == "gliner"
        assert inserted_doc["mode"] == "shadow"
        assert inserted_doc["payload"]["entities"][0]["span"] == "NVDA"

        # Query testing
        mock_col.find.return_value = [inserted_doc]
        results = feature_lineage_store.get_features_for_cycle("cycle-test-101")
        assert len(results) == 1
        assert results[0]["feature_id"] == feature_id
