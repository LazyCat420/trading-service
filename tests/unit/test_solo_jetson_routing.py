"""Unit tests for SOLO_JETSON_MODE routing and decision model contract support."""

import types
import pytest
from app.config.config import settings
import app.services.prism_agent_caller as pac


def _endpoint_stub(**boxes):
    """llm-shaped stub: ._endpoints maps key -> obj with .url/.enabled."""
    eps = {
        k: types.SimpleNamespace(url=f"http://{k}:8000", enabled=True)
        for k in boxes or {"dgx_spark": True, "jetson": True}
    }
    return types.SimpleNamespace(_endpoints=eps)


class TestSoloJetsonRouting:
    @pytest.mark.asyncio
    async def test_solo_jetson_mode_routes_decision_agent_to_jetson(self, monkeypatch):
        """When SOLO_JETSON_MODE is True, decision agents route to jetson/vllm instead of dgx_spark/vllm-2."""
        monkeypatch.setattr(pac, "llm", _endpoint_stub(jetson=True, dgx_spark=True))
        monkeypatch.setattr(settings, "SOLO_JETSON_MODE", True)

        async def serves_nemotron(url, force_refresh=False):
            return "nemotron35"

        monkeypatch.setattr(pac, "get_live_model_from_vllm", serves_nemotron)

        model, provider = await pac.resolve_default_model_for_agent("v3_regime_engine")
        assert provider == "vllm"
        assert model == "nemotron35"

    @pytest.mark.asyncio
    async def test_solo_jetson_mode_false_routes_decision_agent_to_dgx_spark(self, monkeypatch):
        """When SOLO_JETSON_MODE is False, decision agents default to dgx_spark/vllm-2."""
        monkeypatch.setattr(pac, "llm", _endpoint_stub(jetson=True, dgx_spark=True))
        monkeypatch.setattr(settings, "SOLO_JETSON_MODE", False)

        async def serves_deepseek(url, force_refresh=False):
            return "deepseek-v4-flash-0731"

        monkeypatch.setattr(pac, "get_live_model_from_vllm", serves_deepseek)

        model, provider = await pac.resolve_default_model_for_agent("v3_regime_engine")
        assert provider == "vllm-2"
        assert model == "deepseek-v4-flash-0731"

    @pytest.mark.asyncio
    async def test_contract_accepts_nemotron_or_deepseek(self, monkeypatch):
        """Pattern 'deepseek|nemotron' accepts both Nemotron and DeepSeek."""
        monkeypatch.setattr(pac, "llm", _endpoint_stub(jetson=True, dgx_spark=True))
        monkeypatch.setattr(settings, "SOLO_JETSON_MODE", True)
        monkeypatch.setattr(settings, "DECISION_MODEL_PATTERN", "deepseek|nemotron")

        async def serves_nemotron(url, force_refresh=False):
            return "nvidia/NVIDIA-Nemotron-3.5-Lightning-30B-A3B-NVFP4"

        monkeypatch.setattr(pac, "get_live_model_from_vllm", serves_nemotron)

        model, provider = await pac.resolve_default_model_for_agent("v3_portfolio_manager")
        assert "Nemotron" in model
        assert provider == "vllm"

    @pytest.mark.asyncio
    async def test_contract_rejects_unapproved_model_in_solo_jetson_mode(self, monkeypatch):
        """In SOLO_JETSON_MODE, an unapproved model on Jetson raises ModelContractError."""
        monkeypatch.setattr(pac, "llm", _endpoint_stub(jetson=True, dgx_spark=True))
        monkeypatch.setattr(settings, "SOLO_JETSON_MODE", True)
        monkeypatch.setattr(settings, "DECISION_MODEL_PATTERN", "deepseek|nemotron")

        async def serves_random(url, force_refresh=False):
            return "random-unapproved-model"

        monkeypatch.setattr(pac, "get_live_model_from_vllm", serves_random)

        with pytest.raises(pac.ModelContractError) as exc:
            await pac.resolve_default_model_for_agent("v3_portfolio_manager")
        assert "DECISION_MODEL_PATTERN" in str(exc.value)
