"""The 2026-08-26 model-contract batch.

Measured incident (behavioral audit, 2026-08-26): dgx_spark served
`cyankiwi/Qwen3.6-35B-A3B-AWQ-4bit` during trading windows on 08-25/26 —
prism's memory jobs had it loaded — and model resolution trusts whatever the
box currently serves. Qwen answered the 24k-token regime prompt with a
10-token `{"regime": "NEUTRAL"}`, every follow-up iteration returned null,
and 45 of 74 desks since 08-23 died as `board_degraded_fallback` with no
trade_results row, no Board run, and no page. The LLM pre-flight passed
because the endpoint was alive; the fully-DEGRADED cycle streak never formed
because healthy cycles interleaved.
"""
import inspect
import types

import pytest


def _endpoint_stub(**boxes):
    """llm-shaped stub: ._endpoints maps key -> obj with .url/.enabled."""
    eps = {
        k: types.SimpleNamespace(url=f"http://{k}:8000", enabled=True)
        for k in boxes or {"dgx_spark": True, "jetson": True}
    }
    return types.SimpleNamespace(_endpoints=eps)


# ── 1. the resolver refuses a wrong model on the decision box ───────────────
class TestModelContract:
    @pytest.mark.asyncio
    async def test_qwen_on_dgx_spark_raises(self, monkeypatch):
        import app.services.prism_agent_caller as pac

        monkeypatch.setattr(pac, "llm", _endpoint_stub(dgx_spark=True))

        async def serves_qwen(url, force_refresh=False):
            return "cyankiwi/Qwen3.6-35B-A3B-AWQ-4bit"

        monkeypatch.setattr(pac, "get_live_model_from_vllm", serves_qwen)
        with pytest.raises(pac.ModelContractError) as exc:
            await pac.resolve_default_model_for_agent("v3_regime_engine")
        assert "Qwen3.6" in str(exc.value) and "DECISION_MODEL_PATTERN" in str(exc.value)

    @pytest.mark.asyncio
    async def test_deepseek_on_dgx_spark_passes(self, monkeypatch):
        import app.services.prism_agent_caller as pac

        monkeypatch.setattr(pac, "llm", _endpoint_stub(dgx_spark=True))

        async def serves_deepseek(url, force_refresh=False):
            return "deepseek-v4-flash-0731"

        monkeypatch.setattr(pac, "get_live_model_from_vllm", serves_deepseek)
        model, provider = await pac.resolve_default_model_for_agent("v3_regime_engine")
        assert model == "deepseek-v4-flash-0731" and provider == "vllm-2"

    @pytest.mark.asyncio
    async def test_jetson_leg_is_exempt(self, monkeypatch):
        """Collector/janitor roles are model-agnostic; the Jetson box
        legitimately serves non-decision models."""
        import app.services.prism_agent_caller as pac

        monkeypatch.setattr(pac, "llm", _endpoint_stub(jetson=True))

        async def serves_other(url, force_refresh=False):
            return "some-jetson-model"

        monkeypatch.setattr(pac, "get_live_model_from_vllm", serves_other)
        model, provider = await pac.resolve_default_model_for_agent("ticker_validator")
        assert model == "some-jetson-model" and provider == "vllm"

    @pytest.mark.asyncio
    async def test_empty_pattern_disables_the_guard(self, monkeypatch):
        import app.services.prism_agent_caller as pac
        from app.config.config import settings

        monkeypatch.setattr(pac, "llm", _endpoint_stub(dgx_spark=True))
        monkeypatch.setattr(settings, "DECISION_MODEL_PATTERN", "")

        async def serves_qwen(url, force_refresh=False):
            return "cyankiwi/Qwen3.6-35B-A3B-AWQ-4bit"

        monkeypatch.setattr(pac, "get_live_model_from_vllm", serves_qwen)
        model, _ = await pac.resolve_default_model_for_agent("v3_regime_engine")
        assert "Qwen3.6" in model


# ── 2. the pre-flight treats a contract violation as a dead LLM path ────────
class TestPreflightContract:
    @pytest.mark.asyncio
    async def test_contract_violation_aborts_the_cycle(self, monkeypatch):
        from app.services import llm_preflight as pf
        import app.services.prism_agent_caller as pac

        async def refuses(agent, **kw):
            raise pac.ModelContractError("dgx_spark is serving 'Qwen'")

        monkeypatch.setattr(pac, "resolve_default_model_for_agent", refuses)
        ok, detail = await pf.llm_can_answer()
        assert ok is False and "model contract violated" in detail

    @pytest.mark.asyncio
    async def test_other_resolver_errors_still_fail_open(self, monkeypatch):
        """Broken probe machinery must not block all trading — only the
        POSITIVE contract violation aborts."""
        from app.services import llm_preflight as pf
        import app.services.prism_agent_caller as pac

        async def broken(agent, **kw):
            raise RuntimeError("resolver exploded")

        monkeypatch.setattr(pac, "resolve_default_model_for_agent", broken)
        ok, detail = await pf.llm_can_answer()
        assert ok is True and "probe-skipped" in detail

    def test_the_abort_path_pages(self):
        """An aborted cycle writes no analyses, so the DEGRADED streak never
        forms; the abort branch itself must page."""
        import app.services.pipeline_service as ps

        src = inspect.getsource(ps)
        i = src.index("llm_preflight_failed")
        assert "alert_preflight_abort" in src[i : i + 500]


# ── 3. desk-level mortality pages even when cycles interleave ───────────────
class TestPartialDegradationAlert:
    def _run(self, monkeypatch, verdicts, prior_alert=False):
        from app.services import degraded_alert as da
        from app.db import mongo_store

        def find_docs(coll, q, sort=None, limit=None):
            if coll == "analysis_results":
                return [{"cycle_id": f"c{i}", "thesis_verdict": v}
                        for i, v in enumerate(verdicts)]
            if coll == "fund_alerts":
                return [{"id": "x"}] if prior_alert else []
            return []

        recorded = []
        monkeypatch.setattr(mongo_store, "find_docs", find_docs)
        import app.services.alert_service as als
        monkeypatch.setattr(als, "record_fund_alert",
                            lambda **kw: recorded.append(kw) or {"ok": True})
        return da._maybe_alert_partial_degradation(), recorded

    def test_majority_degraded_across_mixed_cycles_pages(self, monkeypatch):
        fired, recorded = self._run(
            monkeypatch, ["DEGRADED", "HOLD", "DEGRADED", "DEGRADED", "BUY", "DEGRADED"])
        assert fired is True and recorded
        assert recorded[0]["alert_type"] == "llm_degraded_partial"

    def test_minority_degraded_does_not_page(self, monkeypatch):
        fired, recorded = self._run(
            monkeypatch, ["DEGRADED", "HOLD", "HOLD", "HOLD", "BUY", "HOLD"])
        assert fired is False and not recorded

    def test_a_quiet_day_does_not_page_on_one_bad_desk(self, monkeypatch):
        fired, recorded = self._run(monkeypatch, ["DEGRADED", "DEGRADED"])
        assert fired is False and not recorded

    def test_dedupe_suppresses_repeats(self, monkeypatch):
        fired, recorded = self._run(
            monkeypatch,
            ["DEGRADED", "HOLD", "DEGRADED", "DEGRADED", "BUY", "DEGRADED"],
            prior_alert=True)
        assert fired is False and not recorded


# ── 4. the empty-screen return shape (ch.98 A2a) ────────────────────────────
class TestBatchScreenerEmptyFrame:
    @pytest.mark.asyncio
    async def test_an_empty_frame_returns_the_tuple_shape(self, monkeypatch):
        """`return "Failed to fetch data."` (bare string) made callers that
        unpack a 2-tuple raise ValueError, and the cycle silently ran AAPL."""
        import pandas as pd
        import app.utils.batch_screener as bs

        monkeypatch.setattr(bs.yf, "download", lambda *a, **kw: pd.DataFrame())
        out = await bs.get_watchlist_snapshots([{"ticker": "CVX"}])
        assert isinstance(out, tuple) and len(out) == 2
        msg, results = out
        assert results == [] and "Failed" in msg
