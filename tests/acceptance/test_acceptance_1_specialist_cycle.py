"""
Acceptance Test 1: Prove specialists reach GLM through a real cycle.
Exercises run_v3_pipeline across advisory, shadow, and disabled modes:
- Advisory: delivers all 3 specialist outputs (GLiNER, CNN, RNN), renders into SharedDesk,
  and records delivery receipts in agent_telemetry.
- Shadow: persists specialist features on the desk without altering agent prompts.
- Disabled: makes 0 remote specialist calls.
"""

from datetime import datetime, timezone
import pytest
from unittest.mock import AsyncMock, patch, MagicMock

from app.v3.shared_desk import SharedDesk
from app.config import settings
from app.v3.orchestrator import run_v3_pipeline


@pytest.fixture
def mock_feature_client():
    client = MagicMock()
    client.extract_entities = AsyncMock(return_value={
        "entities": [
            {"text": "Apple", "label": "ORGANIZATION", "ticker": "AAPL"},
            {"text": "iPhone 16", "label": "PRODUCT", "ticker": "AAPL"},
        ],
        "model_version": "gliner-bi-encoder-v1",
        "latency_ms": 24.5,
    })
    client.predict_regime = AsyncMock(return_value={
        "predicted_regime": "bullish_expansion",
        "brier_score": 0.048,
        "probabilities": {"bullish_expansion": 0.85, "bearish_contraction": 0.15},
        "model_version": "market_cnn-v1",
    })
    client.forecast_quantiles = AsyncMock(return_value={
        "quantiles": {"p10": -0.012, "p50": 0.025, "p90": 0.065},
        "horizon_days": 5,
        "stop_loss_ref": 142.50,
        "model_version": "timeseries_rnn-v1",
    })
    return client


@pytest.fixture
def sample_data_report():
    # 35 daily bars so both CNN (>=30) and RNN (>=25) have sufficient data
    bars = [
        {"close": 150.0 + i, "open": 149.0 + i, "high": 152.0 + i, "low": 148.0 + i, "volume": 1000000}
        for i in range(35)
    ]
    news = [
        {"title": "Apple announces record Q4 earnings and new AI initiatives", "published_at": datetime.now(timezone.utc).isoformat()},
        {"title": "Analyst upgrades AAPL price target on supply chain strength", "published_at": datetime.now(timezone.utc).isoformat()},
    ]
    return {
        "price_history": bars,
        "news": news,
        "metadata": {"ticker": "AAPL"},
    }


from app.v3.shared_desk import SharedDesk, PhaseOutcome

pytestmark = pytest.mark.real_mongo


@pytest.mark.asyncio
async def test_advisory_mode_delivers_specialists_and_receipts(mock_feature_client, sample_data_report):
    with patch("app.config.settings.SPECIALIST_MODE", "advisory"), \
         patch("app.v3.data_report.build_ticker_data_report", new_callable=AsyncMock) as mock_report, \
         patch("app.services.jetson_feature_client.feature_client", mock_feature_client), \
         patch("app.v3.orchestrator.run_v3_agent", new_callable=AsyncMock) as mock_agent, \
         patch("app.v3.orchestrator.persist_telemetry", new_callable=AsyncMock), \
         patch("app.v3.orchestrator.save_desk") as mock_save:

        mock_report.return_value = sample_data_report
        mock_agent.return_value = PhaseOutcome.SUCCESS

        await run_v3_pipeline(
            ticker="AAPL",
            cycle_id="test_cycle_advisory",
        )

        # 1. Verify remote specialist calls occurred
        mock_feature_client.extract_entities.assert_awaited_once()
        mock_feature_client.predict_regime.assert_awaited_once()
        mock_feature_client.forecast_quantiles.assert_awaited_once()

        # 2. Check specialist features in saved desk
        assert mock_save.called
        saved_desk = mock_save.call_args[0][0]
        spec_features = saved_desk.specialist_features
        assert spec_features.get("mode") == "advisory"
        assert spec_features.get("gliner", {}).get("status") == "AVAILABLE"
        assert spec_features.get("cnn", {}).get("status") == "AVAILABLE"
        assert spec_features.get("rnn", {}).get("status") == "AVAILABLE"

        # 3. Verify delivery receipts in agent_telemetry
        telemetry = saved_desk.agent_telemetry
        reached_roles = {t.get("agent_role"): t.get("specialist_features_reached") for t in telemetry if "specialist_features_reached" in t}
        assert "v3_fundamental_analyst" in reached_roles
        assert "gliner" in reached_roles["v3_fundamental_analyst"]
        assert "v3_technical_analyst" in reached_roles
        assert "cnn" in reached_roles["v3_technical_analyst"]
        assert "v3_quant_analyst" in reached_roles
        assert "rnn" in reached_roles["v3_quant_analyst"]
        assert "v3_board_of_directors" in reached_roles
        assert reached_roles["v3_board_of_directors"] == ["gliner", "cnn", "rnn"]


@pytest.mark.asyncio
async def test_shadow_mode_persists_without_prompt_alteration(mock_feature_client, sample_data_report):
    with patch("app.config.settings.SPECIALIST_MODE", "shadow"), \
         patch("app.v3.data_report.build_ticker_data_report", new_callable=AsyncMock) as mock_report, \
         patch("app.services.jetson_feature_client.feature_client", mock_feature_client), \
         patch("app.v3.orchestrator.run_v3_agent", new_callable=AsyncMock) as mock_agent, \
         patch("app.v3.orchestrator.persist_telemetry", new_callable=AsyncMock), \
         patch("app.v3.orchestrator.save_desk") as mock_save:

        mock_report.return_value = sample_data_report
        mock_agent.return_value = PhaseOutcome.SUCCESS

        await run_v3_pipeline(
            ticker="AAPL",
            cycle_id="test_cycle_shadow",
        )

        # 1. Specialists were called and results persisted
        mock_feature_client.extract_entities.assert_awaited_once()
        mock_feature_client.predict_regime.assert_awaited_once()
        mock_feature_client.forecast_quantiles.assert_awaited_once()

        assert mock_save.called
        saved_desk = mock_save.call_args[0][0]
        spec_features = saved_desk.specialist_features
        assert spec_features.get("mode") == "shadow"

        # 2. Verify that render_specialist_features_context returns empty string in shadow mode
        assert saved_desk.render_specialist_features_context() == ""

        # 3. No advisory delivery receipts logged
        telemetry = saved_desk.agent_telemetry
        reached_roles = [t for t in telemetry if "specialist_features_reached" in t]
        assert len(reached_roles) == 0


@pytest.mark.asyncio
async def test_disabled_mode_makes_zero_specialist_calls(mock_feature_client, sample_data_report):
    with patch("app.config.settings.SPECIALIST_MODE", "disabled"), \
         patch("app.v3.data_report.build_ticker_data_report", new_callable=AsyncMock) as mock_report, \
         patch("app.services.jetson_feature_client.feature_client", mock_feature_client), \
         patch("app.v3.orchestrator.run_v3_agent", new_callable=AsyncMock) as mock_agent, \
         patch("app.v3.orchestrator.persist_telemetry", new_callable=AsyncMock), \
         patch("app.v3.orchestrator.save_desk") as mock_save:

        mock_report.return_value = sample_data_report
        mock_agent.return_value = PhaseOutcome.SUCCESS

        await run_v3_pipeline(
            ticker="AAPL",
            cycle_id="test_cycle_disabled",
        )

        # 0 calls made to remote specialists
        mock_feature_client.extract_entities.assert_not_called()
        mock_feature_client.predict_regime.assert_not_called()
        mock_feature_client.forecast_quantiles.assert_not_called()

        assert mock_save.called
        saved_desk = mock_save.call_args[0][0]
        spec_features = saved_desk.specialist_features
        assert spec_features.get("mode") == "disabled"
        assert spec_features.get("status") == "DISABLED"
