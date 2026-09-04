"""Unit tests for dynamic smart routing with bidirectional auto-delegation."""

import types
import pytest
from app.config.config import settings
import app.services.prism_agent_caller as pac


def _endpoint_stub(**boxes):
    """llm-shaped stub: ._endpoints maps key -> obj with .url/.enabled."""
    eps = {
        k: types.SimpleNamespace(url=f"http://{k}:8000", enabled=v)
        for k, v in boxes.items()
    }
    return types.SimpleNamespace(_endpoints=eps)


class TestSmartRouting:
    @pytest.mark.asyncio
    async def test_both_boxes_online_delegates_hard_to_dgx_and_light_to_jetson(self, monkeypatch):
        """When both DGX Spark and Jetson are online, hard tasks go to DGX and light to Jetson."""
        monkeypatch.setattr(pac, "llm", _endpoint_stub(jetson=True, dgx_spark=True))
        monkeypatch.setattr(settings, "SOLO_JETSON_MODE", False)
        monkeypatch.setattr(settings, "DECISION_MODEL_PATTERN", "deepseek|nemotron")

        async def mock_get_live_model(url, force_refresh=False):
            if "dgx_spark" in url or "dgx" in url:
                return "deepseek-v4-flash-0731"
            return "nemotron35"

        monkeypatch.setattr(pac, "get_live_model_from_vllm", mock_get_live_model)

        # Decision / Heavy agent -> dgx_spark / vllm-2
        model, provider = await pac.resolve_default_model_for_agent("v3_regime_engine")
        assert provider == "vllm-2"
        assert model == "deepseek-v4-flash-0731"

        # Collector agent -> jetson / vllm
        model, provider = await pac.resolve_default_model_for_agent("janitor")
        assert provider == "vllm"
        assert model == "nemotron35"

    @pytest.mark.asyncio
    async def test_dgx_offline_automatically_delegates_all_to_jetson(self, monkeypatch):
        """When DGX Spark is offline, hard tasks seamlessly fall back to Jetson."""
        monkeypatch.setattr(pac, "llm", _endpoint_stub(jetson=True, dgx_spark=True))
        monkeypatch.setattr(settings, "SOLO_JETSON_MODE", False)
        monkeypatch.setattr(settings, "DECISION_MODEL_PATTERN", "deepseek|nemotron")

        async def mock_get_live_model(url, force_refresh=False):
            if "dgx_spark" in url or "dgx" in url:
                raise pac.ModelUnavailableError("HTTP 502: DGX Spark down")
            return "nemotron35"

        monkeypatch.setattr(pac, "get_live_model_from_vllm", mock_get_live_model)

        # Decision agent should automatically delegate to Jetson without raising!
        model, provider = await pac.resolve_default_model_for_agent("v3_regime_engine")
        assert provider == "vllm"
        assert model == "nemotron35"

    @pytest.mark.asyncio
    async def test_jetson_offline_automatically_delegates_all_to_dgx(self, monkeypatch):
        """When Jetson is offline, light/collector tasks seamlessly delegate to DGX Spark."""
        monkeypatch.setattr(pac, "llm", _endpoint_stub(jetson=True, dgx_spark=True))
        monkeypatch.setattr(settings, "SOLO_JETSON_MODE", False)
        monkeypatch.setattr(settings, "DECISION_MODEL_PATTERN", "deepseek|nemotron")

        async def mock_get_live_model(url, force_refresh=False):
            if "jetson" in url:
                raise pac.ModelUnavailableError("Connection refused: Jetson down")
            return "deepseek-v4-flash-0731"

        monkeypatch.setattr(pac, "get_live_model_from_vllm", mock_get_live_model)

        # Collector agent should automatically delegate to DGX Spark without raising!
        model, provider = await pac.resolve_default_model_for_agent("janitor")
        assert provider == "vllm-2"
        assert model == "deepseek-v4-flash-0731"

    @pytest.mark.asyncio
    async def test_both_offline_raises_model_unavailable(self, monkeypatch):
        """When both boxes are offline, ModelUnavailableError is raised."""
        monkeypatch.setattr(pac, "llm", _endpoint_stub(jetson=True, dgx_spark=True))
        monkeypatch.setattr(settings, "SOLO_JETSON_MODE", False)

        async def mock_get_live_model(url, force_refresh=False):
            raise pac.ModelUnavailableError("All hosts down")

        monkeypatch.setattr(pac, "get_live_model_from_vllm", mock_get_live_model)

        with pytest.raises(pac.ModelUnavailableError):
            await pac.resolve_default_model_for_agent("v3_regime_engine")

    @pytest.mark.asyncio
    async def test_contract_check_runs_on_fallback_box(self, monkeypatch):
        """When fallback routes to Jetson, Jetson's model is verified against DECISION_MODEL_PATTERN."""
        monkeypatch.setattr(pac, "llm", _endpoint_stub(jetson=True, dgx_spark=True))
        monkeypatch.setattr(settings, "SOLO_JETSON_MODE", False)
        monkeypatch.setattr(settings, "DECISION_MODEL_PATTERN", "deepseek_only")

        async def mock_get_live_model(url, force_refresh=False):
            if "dgx" in url:
                raise pac.ModelUnavailableError("DGX down")
            return "unapproved_model_id"

        monkeypatch.setattr(pac, "get_live_model_from_vllm", mock_get_live_model)

        with pytest.raises(pac.ModelContractError):
            await pac.resolve_default_model_for_agent("v3_regime_engine")

    @pytest.mark.asyncio
    async def test_glm_on_dgx_spark_selected_when_pattern_allows_glm(self, monkeypatch):
        """When pattern includes glm, GLM-5.3-Flash-EXL3 on DGX Spark is successfully selected."""
        monkeypatch.setattr(pac, "llm", _endpoint_stub(jetson=True, dgx_spark=True))
        monkeypatch.setattr(settings, "SOLO_JETSON_MODE", False)
        monkeypatch.setattr(settings, "DECISION_MODEL_PATTERN", "deepseek|nemotron|glm")

        async def mock_get_live_model(url, force_refresh=False):
            if "dgx" in url:
                return "GLM-5.3-Flash-EXL3"
            return "nemotron35"

        monkeypatch.setattr(pac, "get_live_model_from_vllm", mock_get_live_model)

        model, provider = await pac.resolve_default_model_for_agent("v3_regime_engine")
        assert provider == "vllm-2"
        assert model == "GLM-5.3-Flash-EXL3"

    @pytest.mark.asyncio
    async def test_dgx_spark_contract_mismatch_falls_back_to_jetson_nemotron(self, monkeypatch):
        """When DGX Spark has an unapproved model, it seamlessly falls back to Nemotron on Jetson."""
        monkeypatch.setattr(pac, "llm", _endpoint_stub(jetson=True, dgx_spark=True))
        monkeypatch.setattr(settings, "SOLO_JETSON_MODE", False)
        monkeypatch.setattr(settings, "DECISION_MODEL_PATTERN", "deepseek|nemotron|glm")

        async def mock_get_live_model(url, force_refresh=False):
            if "dgx" in url:
                return "unapproved-qwen-model"
            return "nemotron35"

        monkeypatch.setattr(pac, "get_live_model_from_vllm", mock_get_live_model)

        # DGX fails contract -> falls back to Jetson nemotron35 which matches nemotron!
        model, provider = await pac.resolve_default_model_for_agent("v3_regime_engine")
        assert provider == "vllm"
        assert model == "nemotron35"

    @pytest.mark.asyncio
    async def test_both_boxes_fail_contract_raises_contract_error(self, monkeypatch):
        """When all candidate boxes fail the contract pattern, ModelContractError is raised."""
        monkeypatch.setattr(pac, "llm", _endpoint_stub(jetson=True, dgx_spark=True))
        monkeypatch.setattr(settings, "SOLO_JETSON_MODE", False)
        monkeypatch.setattr(settings, "DECISION_MODEL_PATTERN", "deepseek|nemotron")

        async def mock_get_live_model(url, force_refresh=False):
            if "dgx" in url:
                return "unapproved-model-1"
            return "unapproved-model-2"

        monkeypatch.setattr(pac, "get_live_model_from_vllm", mock_get_live_model)

        with pytest.raises(pac.ModelContractError):
            await pac.resolve_default_model_for_agent("v3_regime_engine")
