"""
Unit tests for Specialist Feature Wiring into SharedDesk & Contract Enforcement.
Verifies Items 1 & 10:
- Explicit modes: disabled, shadow, advisory.
- Typed feature payloads on SharedDesk (GLiNER, Market CNN, Timeseries RNN).
- Rendering into compressed context only when in advisory mode.
- Lineage tracking: recording which features reached each deciding agent.
- Contract enforcement: rejection of incomplete OHLCV (< 30 bars), rejection of zero-padding,
  quantile monotonicity checks, and document batch chunking.
"""

from unittest.mock import AsyncMock, patch
import pytest

from app.services.jetson_feature_client import JetsonFeatureClient
from app.v3.shared_desk import SharedDesk


@pytest.fixture
def desk():
    return SharedDesk(ticker="NVDA", cycle_id="cycle-test-spec")


@pytest.fixture
def feature_payload():
    return {
        "mode": "advisory",
        "gliner": {
            "model_version": "gliner-trading-v1",
            "entities": [
                {"text": "$26.3B", "label": "financial_metric_value", "ticker": "NVDA"},
                {"text": "record revenue", "label": "event_trigger", "ticker": "NVDA"},
            ],
            "confidence": 0.92,
        },
        "cnn": {
            "model_version": "cnn-regime-v1",
            "predicted_regime": "BULL_TREND",
            "probabilities": {"BULL_TREND": 0.82, "HIGH_VOL_CHOP": 0.12, "BEAR_TREND": 0.06},
            "brier_score": 0.045,
        },
        "rnn": {
            "model_version": "rnn-forecast-v1",
            "horizon_days": 5,
            "quantiles": {"p10": -0.015, "p50": 0.032, "p90": 0.078},
            "stop_loss_ref": 118.2,
        },
    }


def test_specialist_features_disabled_mode(desk, feature_payload):
    payload = dict(feature_payload)
    payload["mode"] = "disabled"
    desk.append_artifact("specialist_features", payload)

    ctx = desk.get_compressed_context()
    assert "Specialist Neural Intelligence" not in ctx


def test_specialist_features_shadow_mode(desk, feature_payload):
    payload = dict(feature_payload)
    payload["mode"] = "shadow"
    desk.append_artifact("specialist_features", payload)

    assert desk.has_artifact("specialist_features")
    ctx = desk.get_compressed_context()
    # In shadow mode, features are stored on desk but NOT injected into agent prompt
    assert "Specialist Neural Intelligence" not in ctx


def test_specialist_features_advisory_mode(desk, feature_payload):
    desk.append_artifact("specialist_features", feature_payload)

    ctx = desk.get_compressed_context()
    assert "Specialist Neural Intelligence" in ctx
    assert "GLiNER Entities" in ctx
    assert "$26.3B" in ctx
    assert "Market CNN Regime: BULL_TREND" in ctx
    assert "Timeseries RNN 5-Day Forecast" in ctx
    assert "p10=-1.5%" in ctx or "-0.015" in ctx


def test_records_features_reached_per_agent(desk):
    desk.record_agent_specialist_features_reached("v3_fundamental_analyst", ["gliner.financial_metrics", "gliner.guidance"])
    desk.record_agent_specialist_features_reached("v3_technical_analyst", ["cnn.market_regime"])
    desk.record_agent_specialist_features_reached("v3_quant_analyst", ["rnn.quantiles_5d"])

    telemetry = desk.agent_telemetry
    assert len(telemetry) == 3
    assert telemetry[0]["agent_role"] == "v3_fundamental_analyst"
    assert "gliner.financial_metrics" in telemetry[0]["specialist_features_reached"]


@pytest.mark.asyncio
async def test_rejects_incomplete_ohlcv_without_zero_padding():
    client = JetsonFeatureClient(base_url="http://10.0.0.30:8002")

    # Incomplete: 15 bars instead of minimum 30
    incomplete_bars = [[100.0, 101.0, 99.0, 100.5, 1000.0] for _ in range(15)]
    with pytest.raises(ValueError) as exc:
        await client.classify_market_regime(
            instrument_id="NVDA",
            bar_interval="1d",
            window_end="2026-09-18T16:00:00Z",
            lookback_bars=30,
            ohlcv=incomplete_bars,
        )
    assert "incomplete" in str(exc.value).lower() or "30" in str(exc.value)


@pytest.mark.asyncio
async def test_rejects_zero_or_nan_in_ohlcv():
    client = JetsonFeatureClient(base_url="http://10.0.0.30:8002")

    bars_with_nan = [[100.0, 101.0, 99.0, 100.5, 1000.0] for _ in range(29)]
    bars_with_nan.append([100.0, float("nan"), 99.0, 100.5, 1000.0])  # NaN bar

    with pytest.raises(ValueError) as exc:
        await client.classify_market_regime(
            instrument_id="NVDA",
            bar_interval="1d",
            window_end="2026-09-18T16:00:00Z",
            lookback_bars=30,
            ohlcv=bars_with_nan,
        )
    assert "nan" in str(exc.value).lower() or "non-finite" in str(exc.value).lower() or "invalid" in str(exc.value).lower()


@pytest.mark.asyncio
async def test_detects_crossed_quantiles_as_degraded():
    client = JetsonFeatureClient(base_url="http://10.0.0.30:8002")

    # Mock response with crossed quantiles: p10 > p50!
    mock_resp = {
        "status": "ok",
        "result": {
            "return_quantiles": {"p10": 0.05, "p50": 0.02, "p90": 0.08},
        },
    }
    with patch.object(client, "_post_with_resilience", new_callable=AsyncMock) as mock_post:
        mock_post.return_value = mock_resp
        seq = [[100.0 + i for _ in range(8)] for i in range(25)]
        resp = await client.predict_forecast(
            instrument_id="NVDA",
            bar_interval="1d",
            lookback_bars=25,
            sequence=seq,
        )
        assert resp["status"] == "DEGRADED"


@pytest.mark.asyncio
async def test_chunks_large_document_batches_for_gliner():
    client = JetsonFeatureClient(base_url="http://10.0.0.30:8002")

    # 55 documents (exceeds Jetson 50 items limit)
    docs = [{"document_id": f"doc_{i}", "text": f"Article number {i}"} for i in range(55)]

    post_calls = []
    async def mock_post_fn(endpoint, payload, **kwargs):
        post_calls.append(len(payload["documents"]))
        return {"status": "ok", "result": {"entities": [{"text": "NVDA", "label": "ticker"}]}}

    with patch.object(client, "_post_with_resilience", side_effect=mock_post_fn):
        res = await client.extract_entities(documents=docs)
        assert len(post_calls) == 2
        assert post_calls[0] <= 45
        assert post_calls[1] <= 45
        assert len(res["result"]["entities"]) == 2
