"""
Live integration test against the real Jetson Feature Platform running at http://10.0.0.30:8002.
Skips automatically if the live service is unreachable.
"""

import os
import urllib.request
import pytest
from app.services.jetson_feature_client import JetsonFeatureClient


def _is_jetson_live() -> bool:
    try:
        with urllib.request.urlopen("http://10.0.0.30:8002/health", timeout=2.0) as resp:
            return resp.status == 200
    except Exception:
        return False


JETSON_AVAILABLE = _is_jetson_live()


@pytest.mark.skipif(not JETSON_AVAILABLE, reason="Jetson Feature Platform at 10.0.0.30:8002 is not reachable")
@pytest.mark.asyncio
async def test_live_jetson_health_and_capabilities(live_http):
    client = JetsonFeatureClient(base_url="http://10.0.0.30:8002")
    health = await client.get_health()
    assert health.get("status") == "ok"
    assert health.get("gpu", {}).get("available") is True
    assert "gliner" in health.get("models_loaded", [])

    caps = await client.get_capabilities()
    assert caps.get("service") == "jetson-feature-platform"
    task_names = [t.get("task") for t in caps.get("tasks", [])]
    assert "entity_extraction" in task_names
    assert "market_regime" in task_names
    assert "forecast" in task_names


@pytest.mark.skipif(not JETSON_AVAILABLE, reason="Jetson Feature Platform at 10.0.0.30:8002 is not reachable")
@pytest.mark.asyncio
async def test_live_gliner_extraction(live_http):
    async with JetsonFeatureClient(base_url="http://10.0.0.30:8002", shadow_mode=True) as client:
        docs = [
            {
                "document_id": "test_live_01",
                "text": "Apple reported 56% revenue growth this quarter in USD, while NVIDIA raised full-year guidance.",
            }
        ]
        resp = await client.extract_entities(documents=docs, threshold=0.5)
        assert resp["model_id"] == "gliner"
        assert resp["schema_version"] == "1"
        assert "documents" in resp["result"]
        doc_res = resp["result"]["documents"][0]
        assert doc_res["document_id"] == "test_live_01"

        entities = doc_res["entities"]
        texts = [e["text"] for e in entities]
        assert "Apple" in texts
        assert "NVIDIA" in texts

        # Test build_lineage_record
        lineage = client.build_lineage_record(
            cycle_id="cycle-live-test",
            decision_id="dec-live-test",
            instrument_id="NVDA",
            source_id="test_live_01",
            response_envelope=resp,
        )
        assert lineage["model_id"] == "gliner"
        assert lineage["mode"] == "shadow"
        assert lineage["instrument_id"] == "NVDA"


@pytest.mark.skipif(not JETSON_AVAILABLE, reason="Jetson Feature Platform at 10.0.0.30:8002 is not reachable")
@pytest.mark.asyncio
async def test_live_cnn_market_regime(live_http):
    async with JetsonFeatureClient(base_url="http://10.0.0.30:8002") as client:
        ohlcv = [[100.0 + i * 0.5, 102.0 + i * 0.5, 99.0 + i * 0.5, 101.5 + i * 0.5, 50000.0] for i in range(30)]
        resp = await client.classify_market_regime(
            instrument_id="NVDA",
            bar_interval="1d",
            window_end="2026-09-18T16:00:00Z",
            lookback_bars=30,
            ohlcv=ohlcv,
        )
        assert resp["model_id"] == "market_cnn"
        result = resp["result"]
        assert "regime" in result
        assert "confidence" in result
        assert "class_probabilities" in result


@pytest.mark.skipif(not JETSON_AVAILABLE, reason="Jetson Feature Platform at 10.0.0.30:8002 is not reachable")
@pytest.mark.asyncio
async def test_live_rnn_forecast(live_http):
    async with JetsonFeatureClient(base_url="http://10.0.0.30:8002") as client:
        sequence = [[150.0 + 0.2 * i for _ in range(8)] for i in range(25)]
        resp = await client.predict_forecast(
            instrument_id="NVDA",
            bar_interval="1d",
            lookback_bars=25,
            sequence=sequence,
        )
        assert resp["model_id"] == "timeseries_rnn"
        result = resp["result"]
        assert result["horizon"] == "5d"
        assert "return_quantiles" in result
        assert "p10" in result["return_quantiles"]
        assert "p50" in result["return_quantiles"]
        assert "p90" in result["return_quantiles"]
