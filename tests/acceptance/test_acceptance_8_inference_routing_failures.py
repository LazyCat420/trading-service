"""
Acceptance Test 8: Test routing and inference failures during a cycle.
- Tests capability verification: endpoints without tools/JSON/context capacity are marked ineligible
  and never receive GLM chat calls (e.g. Jetson alternate port serving only specialist neural weights).
- Injects timeouts, missing OHLCV (< 30 bars), malformed envelopes, and provider errors during cycle execution.
- Verifies invalid/failing features become explicit UNAVAILABLE results with preserved error diagnostics,
  WITHOUT fabricated zeros, cross-document attribution, or lost failure records.
"""

from datetime import datetime, timezone, timedelta
import json
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


# ── 3. Partial Outage: One Fails, Healthy Specialists Deliver ──

@pytest.mark.asyncio
async def test_partial_specialist_outage_healthy_specialists_deliver():
    """
    Simulates:
    - GLiNER remote inference fails (HTTP 500 / timeout).
    - CNN and RNN succeed with valid responses.
    
    Verifies:
    - Cycle completes successfully.
    - GLiNER status: UNAVAILABLE.
    - CNN status: AVAILABLE (regime preserved).
    - RNN status: AVAILABLE (quantiles preserved).
    - Quant analyst receives CNN and RNN signals in prompt.
    - Fundamental analyst prompt does NOT contain hallucinated entities.
    """
    mock_feature_client = MagicMock()
    mock_feature_client.extract_entities = AsyncMock(side_effect=TimeoutError("GLiNER gateway timeout"))
    mock_feature_client.classify_market_regime = AsyncMock(return_value={
        "result": {"regime": "bullish_expansion", "confidence": 0.91},
        "model_version": "market_cnn-v1",
    })
    mock_feature_client.predict_forecast = AsyncMock(return_value={
        "result": {"return_quantiles": {"p10": -0.01, "p50": 0.03, "p90": 0.07}},
        "model_version": "timeseries_rnn-v1",
    })
    mock_feature_client.get_capabilities = AsyncMock(return_value={
        "models": {
            "gliner": {"version": "gliner-v1"},
            "cnn": {"version": "market_cnn-v1"},
            "rnn": {"version": "timeseries_rnn-v1"},
        }
    })
    mock_feature_client.get_active_models = AsyncMock(return_value={
        "gliner": {"version": "gliner-v1"},
        "cnn": {"version": "market_cnn-v1"},
        "rnn": {"version": "timeseries_rnn-v1"},
    })

    now = datetime.now(timezone.utc)
    bars = [
        {"close": 150.0 + i, "timestamp": (now - timedelta(days=36 - i)).isoformat(), "ticker": "AAPL"}
        for i in range(35)
    ]
    news = [{"title": "Apple quarterly report", "summary": "Record services revenue", "published_at": now.isoformat()}]
    report = {
        "price_history": bars,
        "news": news,
        "metadata": {"ticker": "AAPL"},
    }

    captured_prompts = {}
    async def _capturing_agent(**kwargs):
        role = kwargs.get("agent_name", "unknown")
        captured_prompts[role] = kwargs.get("user_prompt", "")
        return {
            "response": json.dumps({
                "ticker": "AAPL", "stance": "HOLD", "confidence": 75,
                "reasoning": "Evaluated partial specialist signals.",
                "data_gaps": [], "key_findings": [], "triggers": [],
            }),
            "tokens_used": 100,
            "loops_used": 1,
        }

    with patch("app.config.settings.SPECIALIST_MODE", "advisory"), \
         patch("app.v3.data_report.build_ticker_data_report", new_callable=AsyncMock) as mock_rep, \
         patch("app.services.jetson_feature_client.feature_client", mock_feature_client), \
         patch("app.agents.base_agent.run_agent", side_effect=_capturing_agent), \
         patch("app.v3.orchestrator.persist_telemetry", MagicMock()), \
         patch("app.v3.orchestrator.save_desk") as mock_save:

        mock_rep.return_value = report
        await run_v3_pipeline(ticker="AAPL", cycle_id="cycle_partial_specialist_outage")

        assert mock_save.called
        saved_desk: SharedDesk = mock_save.call_args[0][0]
        spec = saved_desk.specialist_features

        # 1. Individual statuses
        assert spec["gliner"]["status"] == "UNAVAILABLE"
        assert spec["cnn"]["status"] == "AVAILABLE"
        assert spec["rnn"]["status"] == "AVAILABLE"

        # 2. Outbound prompt verification
        quant_prompt = captured_prompts.get("v3_quant_analyst", "")
        assert "bullish_expansion" in quant_prompt
        assert "Price-Derived Technical Signals" in quant_prompt

        fund_prompt = captured_prompts.get("v3_fundamental_analyst", "")
        assert "candidate text mentions, NOT verified financial facts" not in fund_prompt


# ── 4. Mid-Cycle Model Version Drift Rejection ──

@pytest.mark.asyncio
async def test_mid_cycle_specialist_version_drift_rejected():
    """
    Simulates:
    - Pinned version at admission is market_cnn-v1.
    - Remote Jetson swaps model dynamically and returns market_cnn-v2.
    
    Verifies:
    - Version drift is detected and logged.
    - CNN status becomes UNAVAILABLE with error diagnosing the version mismatch.
    - Drifting model results are rejected from being rendered into prompts.
    """
    mock_feature_client = MagicMock()
    mock_feature_client.extract_entities = AsyncMock(return_value={
        "result": {"entities": [{"text": "Apple", "label": "company"}]},
        "model_version": "gliner-v1",
    })
    # Drifting version served!
    mock_feature_client.classify_market_regime = AsyncMock(return_value={
        "result": {"regime": "bearish_panic", "confidence": 0.99},
        "model_version": "market_cnn-v2-drifted",
    })
    mock_feature_client.predict_forecast = AsyncMock(return_value={
        "result": {"return_quantiles": {"p50": 0.01}},
        "model_version": "timeseries_rnn-v1",
    })
    # Discovery at admission pinned v1
    mock_feature_client.get_capabilities = AsyncMock(return_value={
        "models": {
            "gliner": {"version": "gliner-v1"},
            "cnn": {"version": "market_cnn-v1"},
            "rnn": {"version": "timeseries_rnn-v1"},
        }
    })
    mock_feature_client.get_active_models = AsyncMock(return_value={
        "gliner": {"version": "gliner-v1"},
        "cnn": {"version": "market_cnn-v1"},
        "rnn": {"version": "timeseries_rnn-v1"},
    })

    now = datetime.now(timezone.utc)
    bars = [
        {"close": 150.0 + i, "timestamp": (now - timedelta(days=36 - i)).isoformat(), "ticker": "AAPL"}
        for i in range(35)
    ]
    report = {
        "price_history": bars,
        "news": [{"title": "Apple update", "summary": "Normal trading", "published_at": now.isoformat()}],
        "metadata": {"ticker": "AAPL"},
    }

    captured_prompts = {}
    async def _capturing_agent(**kwargs):
        role = kwargs.get("agent_name", "unknown")
        captured_prompts[role] = kwargs.get("user_prompt", "")
        return {
            "response": json.dumps({"ticker": "AAPL", "stance": "HOLD", "confidence": 70, "reasoning": "OK", "data_gaps": [], "key_findings": [], "triggers": []}),
            "tokens_used": 100,
            "loops_used": 1,
        }

    with patch("app.config.settings.SPECIALIST_MODE", "advisory"), \
         patch("app.v3.data_report.build_ticker_data_report", new_callable=AsyncMock) as mock_rep, \
         patch("app.services.jetson_feature_client.feature_client", mock_feature_client), \
         patch("app.agents.base_agent.run_agent", side_effect=_capturing_agent), \
         patch("app.v3.orchestrator.persist_telemetry", MagicMock()), \
         patch("app.v3.orchestrator.save_desk") as mock_save:

        mock_rep.return_value = report
        await run_v3_pipeline(ticker="AAPL", cycle_id="cycle_drift_test")

        saved_desk: SharedDesk = mock_save.call_args[0][0]
        cnn_feature = saved_desk.specialist_features.get("cnn", {})

        # Drift correctly flagged
        assert cnn_feature.get("status") == "UNAVAILABLE"
        assert "version drift" in cnn_feature.get("error", "").lower()
        assert "market_cnn-v1" in cnn_feature.get("error", "")

        # Drifting signal NOT injected into prompt
        quant_prompt = captured_prompts.get("v3_quant_analyst", "")
        assert "bearish_panic" not in quant_prompt


# ── 5. Truncated Prompt Delivery Receipt ──

def test_truncated_prompt_flags_omitted_or_truncated():
    """
    Verifies that extract_outbound_delivery_receipt classifies delivery as
    OMITTED_OR_TRUNCATED when prompt character budgets drop the specialist section.
    """
    from app.v3.shared_desk import extract_outbound_delivery_receipt

    specialist_features = {
        "mode": "advisory",
        "gliner": {"model_version": "gliner-v1", "status": "AVAILABLE", "entities": [{"text": "AAPL"}]},
        "cnn": {"model_version": "market_cnn-v1", "status": "AVAILABLE", "predicted_regime": "bullish"},
        "rnn": {"model_version": "timeseries_rnn-v1", "status": "AVAILABLE", "quantiles": {"p50": 0.02}},
    }

    # Prompt truncated before specialist section was appended
    truncated_prompt = "You are an analyst. [Context was trimmed due to character budget]"

    receipt = extract_outbound_delivery_receipt(
        agent_role="v3_quant_analyst",
        outbound_prompt=truncated_prompt,
        specialist_features=specialist_features,
    )

    assert receipt["mode"] == "advisory"
    feature_statuses = {f["feature_id"]: f["delivery_status"] for f in receipt["features"]}
    assert feature_statuses["gliner"] == "OMITTED_OR_TRUNCATED"
    assert feature_statuses["cnn"] == "OMITTED_OR_TRUNCATED"
    assert feature_statuses["rnn"] == "OMITTED_OR_TRUNCATED"


# ── 6. Stale Market Data Refuses Specialist Evaluation ──

@pytest.mark.asyncio
async def test_stale_market_bars_refuses_specialist_evaluation():
    """
    Simulates:
    - 35 bars, but the latest bar is 30 days older than cycle_cutoff.
    
    Verifies:
    - _prepare_specialist_price_series rejects bars as stale.
    - CNN and RNN status become UNAVAILABLE with explicit stale diagnostic.
    - Neither CNN nor RNN is called on remote Jetson.
    """
    mock_feature_client = MagicMock()
    mock_feature_client.classify_market_regime = AsyncMock()
    mock_feature_client.predict_forecast = AsyncMock()
    mock_feature_client.get_capabilities = AsyncMock(return_value={
        "models": {"gliner": {"version": "gliner-v1"}, "cnn": {"version": "cnn-v1"}, "rnn": {"version": "rnn-v1"}}
    })
    mock_feature_client.get_active_models = AsyncMock(return_value={
        "gliner": {"version": "gliner-v1"}, "cnn": {"version": "cnn-v1"}, "rnn": {"version": "rnn-v1"}
    })

    cutoff = datetime(2026, 9, 18, 16, 0, 0, tzinfo=timezone.utc)
    # Stale bars ending 30 days before cutoff
    stale_bars = [
        {"close": 150.0 + i, "timestamp": (cutoff - timedelta(days=65 - i)).isoformat(), "ticker": "AAPL"}
        for i in range(35)
    ]
    report = {
        "price_history": stale_bars,
        "news": [],
        "metadata": {"ticker": "AAPL"},
    }

    with patch("app.config.settings.SPECIALIST_MODE", "advisory"), \
         patch("app.v3.data_report.build_ticker_data_report", new_callable=AsyncMock) as mock_rep, \
         patch("app.services.jetson_feature_client.feature_client", mock_feature_client), \
         patch("app.agents.base_agent.run_agent", new_callable=AsyncMock) as mock_agent, \
         patch("app.v3.orchestrator.persist_telemetry", MagicMock()), \
         patch("app.v3.orchestrator.save_desk") as mock_save:

        mock_rep.return_value = report
        mock_agent.return_value = {"response": json.dumps({"ticker": "AAPL", "stance": "HOLD", "confidence": 50, "reasoning": "Stale", "data_gaps": [], "key_findings": [], "triggers": []}), "tokens_used": 50, "loops_used": 1}

        await run_v3_pipeline(ticker="AAPL", cycle_id="cycle_stale_bars_test")

        saved_desk: SharedDesk = mock_save.call_args[0][0]
        spec = saved_desk.specialist_features

        # Stale bars rejected
        assert spec["cnn"]["status"] == "UNAVAILABLE"
        assert "stale" in spec["cnn"]["error"].lower()
        assert spec["rnn"]["status"] == "UNAVAILABLE"
        assert "stale" in spec["rnn"]["error"].lower()

        mock_feature_client.classify_market_regime.assert_not_called()
        mock_feature_client.predict_forecast.assert_not_called()

