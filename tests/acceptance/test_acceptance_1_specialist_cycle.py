"""
Acceptance Test 1: Prove specialists reach GLM through a real cycle.
Exercises run_v3_pipeline across advisory, shadow, disabled, and sabotage modes:
- Advisory: delivers all 3 specialist outputs (GLiNER, CNN, RNN), renders into SharedDesk,
  verifies outbound prompts to GLM contain qualified entities and anti-double-counting warnings,
  and records delivery receipts in agent_telemetry.
- Shadow: persists specialist features on the desk without altering agent prompts.
- Disabled: makes 0 remote specialist calls.
- Sabotage: severs specialist connections and proves pipeline falls back to UNAVAILABLE
  without injecting fake features into prompts.
"""

from datetime import datetime, timezone
import json
import pytest
from unittest.mock import AsyncMock, patch, MagicMock

from app.v3.shared_desk import SharedDesk, PhaseOutcome
from app.config import settings
from app.v3.orchestrator import run_v3_pipeline

pytestmark = pytest.mark.real_mongo


@pytest.fixture
def mock_feature_client():
    client = MagicMock()
    client.extract_entities = AsyncMock(return_value={
        "result": {
            "entities": [
                {"text": "Apple", "label": "company", "ticker": "AAPL", "confidence": 0.95},
                {"text": "iPhone 16", "label": "event_trigger", "ticker": "AAPL", "confidence": 0.92},
            ]
        },
        "model_version": "gliner-bi-encoder-v1",
        "latency_ms": 24.5,
    })
    client.classify_market_regime = AsyncMock(return_value={
        "result": {
            "regime": "bullish_expansion",
            "confidence": 0.85,
            "class_probabilities": {"bullish_expansion": 0.85, "bearish_contraction": 0.15},
        },
        "model_version": "market_cnn-v1",
    })
    client.predict_forecast = AsyncMock(return_value={
        "result": {
            "horizon": "5d",
            "return_quantiles": {"p10": -0.012, "p50": 0.025, "p90": 0.065},
            "volatility_estimate": 0.035,
            "direction_probability_up": 0.65,
        },
        "model_version": "timeseries_rnn-v1",
    })
    client.get_capabilities = AsyncMock(return_value={
        "models": {
            "gliner": {"version": "gliner-bi-encoder-v1"},
            "cnn": {"version": "market_cnn-v1"},
            "rnn": {"version": "timeseries_rnn-v1"},
        }
    })
    return client


@pytest.fixture
def fake_build_report():
    async def _build(ticker, stats_sink=None, **kwargs):
        bars = [
            {"close": 150.0 + i, "open": 149.0 + i, "high": 152.0 + i, "low": 148.0 + i, "volume": 1000000, "timestamp": f"2026-09-18T{i:02d}:00:00Z"}
            for i in range(35)
        ]
        news = [
            {"title": "Apple announces record Q4 earnings and new AI initiatives", "summary": "Apple announces record Q4 earnings and new AI initiatives", "published_at": datetime.now(timezone.utc).isoformat()},
            {"title": "Analyst upgrades AAPL price target on supply chain strength", "summary": "Analyst upgrades AAPL price target on supply chain strength", "published_at": datetime.now(timezone.utc).isoformat()},
        ]
        if stats_sink is not None:
            stats_sink["raw_data"] = {
                "price_history": bars,
                "news": news,
                "metadata": {"ticker": ticker},
            }
        return f"# Market Report for {ticker}\n\nPrice history: 35 bars. Apple announces record earnings."
    return _build


def _fake_run_agent_response(agent_name: str, **kwargs):
    return {
        "response": json.dumps({
            "ticker": "AAPL",
            "summary": "Analysis completed for " + agent_name,
            "confidence": 75,
            "stance": "HOLD",
            "action": "HOLD",
            "decision": "HOLD",
            "reasoning": "Specialist indicators evaluated thoroughly.",
            "data_gaps": [],
            "key_findings": ["Specialist features examined"],
            "triggers": [{"condition": "price > 200", "action": "monitor"}],
        }),
        "tokens_used": 120,
        "loops_used": 1,
    }


@pytest.mark.asyncio
async def test_advisory_mode_delivers_specialists_and_receipts(mock_feature_client, fake_build_report):
    captured_prompts = {}

    async def _capturing_run_agent(**kwargs):
        role = kwargs.get("agent_name", "unknown")
        captured_prompts[role] = {
            "user_prompt": kwargs.get("user_prompt", ""),
            "system_prompt": kwargs.get("system_prompt", ""),
        }
        return _fake_run_agent_response(**kwargs)

    with patch("app.config.settings.SPECIALIST_MODE", "advisory"), \
         patch("app.v3.data_report.build_ticker_data_report", side_effect=fake_build_report), \
         patch("app.services.jetson_feature_client.feature_client", mock_feature_client), \
         patch("app.agents.base_agent.run_agent", side_effect=_capturing_run_agent), \
         patch("app.v3.orchestrator.persist_telemetry", new_callable=MagicMock), \
         patch("app.v3.orchestrator.save_desk") as mock_save:

        await run_v3_pipeline(
            ticker="AAPL",
            cycle_id="test_cycle_advisory",
        )

        # 1. Verify remote specialist calls occurred with real methods
        mock_feature_client.extract_entities.assert_awaited_once()
        mock_feature_client.classify_market_regime.assert_awaited_once()
        mock_feature_client.predict_forecast.assert_awaited_once()

        # 2. Check specialist features in saved desk
        assert mock_save.called
        saved_desk = mock_save.call_args[0][0]
        spec_features = saved_desk.specialist_features
        assert spec_features.get("mode") == "advisory"
        assert spec_features.get("gliner", {}).get("status") == "AVAILABLE"
        assert spec_features.get("cnn", {}).get("status") == "AVAILABLE"
        assert spec_features.get("rnn", {}).get("status") == "AVAILABLE"

        # 3. Prove outbound GLM prompt received qualified specialists
        # Fundamental analyst received GLiNER with fact qualification warning
        fund_prompt = captured_prompts.get("v3_fundamental_analyst", {}).get("user_prompt", "")
        assert "candidate text mentions, NOT verified financial facts" in fund_prompt
        assert "Apple" in fund_prompt

        # Quant received CNN/RNN under correlated dimensions warning
        quant_prompt = captured_prompts.get("v3_quant_analyst", {}).get("user_prompt", "")
        assert "Price-Derived Technical Signals (Shared OHLCV Source — Correlated Dimensions)" in quant_prompt
        assert "bullish_expansion" in quant_prompt

        # 4. Verify delivery receipts in agent_telemetry
        telemetry = saved_desk.agent_telemetry
        reached_roles = {t.get("agent_role"): t.get("specialist_features_reached") for t in telemetry if "specialist_features_reached" in t}
        assert "v3_fundamental_analyst" in reached_roles
        assert "gliner" in reached_roles["v3_fundamental_analyst"]
        assert "v3_quant_analyst" in reached_roles
        assert "cnn" in reached_roles["v3_quant_analyst"]
        assert "rnn" in reached_roles["v3_quant_analyst"]


@pytest.mark.asyncio
async def test_shadow_mode_persists_without_prompt_alteration(mock_feature_client, fake_build_report):
    captured_prompts = {}

    async def _capturing_run_agent(**kwargs):
        role = kwargs.get("agent_name", "unknown")
        captured_prompts[role] = kwargs.get("user_prompt", "")
        return _fake_run_agent_response(**kwargs)

    with patch("app.config.settings.SPECIALIST_MODE", "shadow"), \
         patch("app.v3.data_report.build_ticker_data_report", side_effect=fake_build_report), \
         patch("app.services.jetson_feature_client.feature_client", mock_feature_client), \
         patch("app.agents.base_agent.run_agent", side_effect=_capturing_run_agent), \
         patch("app.v3.orchestrator.persist_telemetry", new_callable=MagicMock), \
         patch("app.v3.orchestrator.save_desk") as mock_save:

        await run_v3_pipeline(
            ticker="AAPL",
            cycle_id="test_cycle_shadow",
        )

        # 1. Specialists were called and results persisted
        mock_feature_client.extract_entities.assert_awaited_once()
        mock_feature_client.classify_market_regime.assert_awaited_once()
        mock_feature_client.predict_forecast.assert_awaited_once()

        assert mock_save.called
        saved_desk = mock_save.call_args[0][0]
        spec_features = saved_desk.specialist_features
        assert spec_features.get("mode") == "shadow"

        # 2. Verify that render_specialist_features_context returns empty string in shadow mode
        assert saved_desk.render_specialist_features_context() == ""

        # 3. Outbound prompts do NOT contain specialist sections
        for role, prompt in captured_prompts.items():
            assert "Price-Derived Technical Signals" not in prompt
            assert "Specialist Neural Intelligence" not in prompt


@pytest.mark.asyncio
async def test_disabled_mode_makes_zero_specialist_calls(mock_feature_client, fake_build_report):
    with patch("app.config.settings.SPECIALIST_MODE", "disabled"), \
         patch("app.v3.data_report.build_ticker_data_report", side_effect=fake_build_report), \
         patch("app.services.jetson_feature_client.feature_client", mock_feature_client), \
         patch("app.agents.base_agent.run_agent", side_effect=_fake_run_agent_response), \
         patch("app.v3.orchestrator.persist_telemetry", new_callable=MagicMock), \
         patch("app.v3.orchestrator.save_desk") as mock_save:

        await run_v3_pipeline(
            ticker="AAPL",
            cycle_id="test_cycle_disabled",
        )

        # 0 calls made to remote specialists
        mock_feature_client.extract_entities.assert_not_called()
        mock_feature_client.classify_market_regime.assert_not_called()
        mock_feature_client.predict_forecast.assert_not_called()

        assert mock_save.called
        saved_desk = mock_save.call_args[0][0]
        spec_features = saved_desk.specialist_features
        assert spec_features.get("mode") == "disabled"
        assert spec_features.get("status") == "DISABLED"


@pytest.mark.asyncio
async def test_sabotage_severed_specialist_falls_back_safely(mock_feature_client, fake_build_report):
    """SABOTAGE GATE: Proves that when specialist client fails, pipeline does not crash or fake features."""
    mock_feature_client.classify_market_regime.side_effect = Exception("Jetson CNN offline: Connection refused")
    mock_feature_client.predict_forecast.side_effect = Exception("Jetson RNN offline: 503 Service Unavailable")

    captured_prompts = {}

    async def _capturing_run_agent(**kwargs):
        role = kwargs.get("agent_name", "unknown")
        captured_prompts[role] = kwargs.get("user_prompt", "")
        return _fake_run_agent_response(**kwargs)

    with patch("app.config.settings.SPECIALIST_MODE", "advisory"), \
         patch("app.v3.data_report.build_ticker_data_report", side_effect=fake_build_report), \
         patch("app.services.jetson_feature_client.feature_client", mock_feature_client), \
         patch("app.agents.base_agent.run_agent", side_effect=_capturing_run_agent), \
         patch("app.v3.orchestrator.persist_telemetry", new_callable=MagicMock), \
         patch("app.v3.orchestrator.save_desk") as mock_save:

        # Cycle must complete safely despite severed specialists
        await run_v3_pipeline(
            ticker="AAPL",
            cycle_id="test_cycle_sabotage",
        )

        assert mock_save.called
        saved_desk = mock_save.call_args[0][0]
        spec_features = saved_desk.specialist_features
        assert spec_features["cnn"]["status"] == "UNAVAILABLE"
        assert spec_features["rnn"]["status"] == "UNAVAILABLE"

        # Quant analyst prompt must NOT contain hallucinated regime
        quant_prompt = captured_prompts.get("v3_quant_analyst", "")
        assert "bullish_expansion" not in quant_prompt
