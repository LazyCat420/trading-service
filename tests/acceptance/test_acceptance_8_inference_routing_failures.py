"""
Acceptance Test 8: Test routing and inference failures during a cycle.
- Tests capability verification: endpoints without tools/JSON/context capacity are marked ineligible
  and never receive GLM chat calls (e.g. Jetson alternate port serving only specialist neural weights).
- Injects timeouts, missing OHLCV (< 30 bars), malformed envelopes, and provider errors during cycle execution.
- Verifies invalid/failing features become explicit UNAVAILABLE results with preserved error diagnostics,
  WITHOUT fabricated zeros, cross-document attribution, or lost failure records.
"""

from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock, MagicMock, patch
import pytest

from app.services.model_capabilities import validate_endpoint
from app.v3.orchestrator import run_v3_pipeline
from app.v3.shared_desk import PhaseOutcome, SharedDesk

pytestmark = pytest.mark.real_mongo


# ── 1. Capability Verification Gate ──

@pytest.mark.asyncio
async def test_specialist_only_endpoint_rejected_from_glm_chat():
    """
    Simulates Jetson alternate port returning HTTP 200, but failing chat tool-calling
    or structured JSON output probes. The endpoint MUST be marked eligible=False
    and rejected from receiving GLM chat calls.
    """
    mock_ep = MagicMock()
    mock_ep.url = "http://10.0.0.16:5001"  # Alternate Jetson specialist port
    mock_ep.max_model_len = 8192

    mock_llm = MagicMock()
    mock_llm._endpoints = {"jetson_specialist_alt": mock_ep}

    # Simulate client responding HTTP 200, but plain text without tool_calls or structured json
    mock_resp_tools = MagicMock()
    mock_resp_tools.is_success = True
    mock_resp_tools.status_code = 200
    mock_resp_tools.raise_for_status = MagicMock()
    mock_resp_tools.json.return_value = {
        "choices": [{"message": {"content": "Specialist feature service running, chat tools not supported"}}]
    }

    mock_resp_json = MagicMock()
    mock_resp_json.is_success = True
    mock_resp_json.status_code = 200
    mock_resp_json.raise_for_status = MagicMock()
    mock_resp_json.json.return_value = {
        "choices": [{"message": {"content": "I cannot guarantee JSON format"}}]
    }

    with patch("app.services.prism_agent_caller.llm", mock_llm), \
         patch("httpx.AsyncClient.post", side_effect=[mock_resp_tools, mock_resp_json]):

        receipt = await validate_endpoint("jetson_specialist_alt", "specialist-neural-v1", force=True)
        assert receipt["eligible"] is False
        assert receipt["tools"] is False
        assert "unverified" in receipt["reason"].lower() or "probe failed" in receipt["reason"].lower()


@pytest.mark.asyncio
async def test_zero_context_capacity_rejected():
    """An endpoint with 0 or None max_model_len cannot be eligible for GLM chat."""
    mock_ep = MagicMock()
    mock_ep.url = "http://10.0.0.16:5591/gold-spark"
    mock_ep.max_model_len = 0  # Missing / zero capacity

    mock_llm = MagicMock()
    mock_llm._endpoints = {"gold_spark": mock_ep}

    with patch("app.services.prism_agent_caller.llm", mock_llm):
        receipt = await validate_endpoint("gold_spark", "GLM-5.3-Flash-EXL3", force=True)
        assert receipt["eligible"] is False


# ── 2. Injected Inference Failures During Cycle ──

@pytest.mark.asyncio
async def test_injected_inference_failures_become_explicit_unavailable_without_fabricated_zeros():
    """
    Simulates:
    1. GLiNER timing out.
    2. Data report having only 10 bars (< 30 required by CNN, < 25 required by RNN).
    3. RNN throwing an unexpected remote exception.
    
    Verifies:
    - gliner status: UNAVAILABLE (no hallucinated entities).
    - cnn status: UNAVAILABLE (no fabricated regime or zero brier score).
    - rnn status: UNAVAILABLE (no fabricated quantiles).
    - Desk records the explicit UNAVAILABLE features in specialist_features and artifacts.
    """
    mock_feature_client = MagicMock()
    mock_feature_client.extract_entities = AsyncMock(side_effect=TimeoutError("Remote GLiNER inference timed out after 10s"))
    mock_feature_client.predict_regime = AsyncMock()
    mock_feature_client.classify_market_regime = AsyncMock()
    mock_feature_client.forecast_quantiles = AsyncMock(side_effect=RuntimeError("GPU OOM on specialist RNN"))
    mock_feature_client.predict_forecast = AsyncMock(side_effect=RuntimeError("GPU OOM on specialist RNN"))

    now = datetime.now(timezone.utc)
    short_data_report = {
        "price_history": [
            {"close": 100.0 + i, "timestamp": (now - timedelta(days=26 - i)).isoformat(), "ticker": "MSFT"}
            for i in range(26)
        ],  # < 30 for CNN (26 bars), >= 25 for RNN (26 bars)
        "news": [{"title": "Short update"}],
        "metadata": {"ticker": "MSFT"},
    }

    with patch("app.config.settings.SPECIALIST_MODE", "advisory"), \
         patch("app.v3.data_report.build_ticker_data_report", new_callable=AsyncMock) as mock_report, \
         patch("app.services.jetson_feature_client.feature_client", mock_feature_client), \
         patch("app.v3.orchestrator.run_v3_agent", new_callable=AsyncMock) as mock_agent, \
         patch("app.v3.orchestrator.persist_telemetry", MagicMock()), \
         patch("app.v3.orchestrator.save_desk") as mock_save:

        mock_report.return_value = short_data_report
        mock_agent.return_value = PhaseOutcome.SUCCESS

        await run_v3_pipeline(ticker="MSFT", cycle_id="cycle_failure_injection")

        assert mock_save.called
        saved_desk: SharedDesk = mock_save.call_args[0][0]
        spec = saved_desk.specialist_features

        # 1. GLiNER failed cleanly
        assert spec.get("gliner", {}).get("status") == "UNAVAILABLE"
        assert "timed out" in spec.get("gliner", {}).get("error", "").lower()
        assert "entities" not in spec.get("gliner", {})

        # 2. CNN refused due to insufficient bars (not called remotely)
        assert spec.get("cnn", {}).get("status") == "UNAVAILABLE"
        assert "insufficient" in spec.get("cnn", {}).get("error", "").lower() and "bars" in spec.get("cnn", {}).get("error", "").lower()
        assert "probabilities" not in spec.get("cnn", {})
        assert "brier_score" not in spec.get("cnn", {})
        mock_feature_client.predict_regime.assert_not_called()

        # 3. RNN failed cleanly
        assert spec.get("rnn", {}).get("status") == "UNAVAILABLE"
        assert "gpu oom" in spec.get("rnn", {}).get("error", "").lower()
        assert "quantiles" not in spec.get("rnn", {})

        # 4. Verified SharedDesk rendering
        rendered_context = saved_desk.render_specialist_features_context()
        assert "UNAVAILABLE" in rendered_context
        # Zero fabricated entities or probabilities
        assert "bullish_expansion" not in rendered_context
        assert "p50" not in rendered_context
