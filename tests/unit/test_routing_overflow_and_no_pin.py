"""One routing mechanism: preference → overflow → fallback, and NO static pin.

Measured 2026-09-02/03: deploy.sh appended SOLO_JETSON_MODE=true to the
container's .env and `resolve_default_model_for_agent` made the Jetson the
only candidate. Every decision agent for two days ran on nemotron35
(v3_agent_telemetry: 191 SUCCESS + 9 SCHEMA_INVALID on vllm, ZERO rows on the
DGX's GLM), and the fundamental analyst returned EMPTY after 13 loops /
490k input tokens / 565 s on the 128K box. A static pin cannot let the DGX
back in when it returns; the fallback loop already handles a box being gone.

Every test here uses the REAL resolver with a stub `llm` whose endpoints
carry the metrics the overflow rule reads. The negative control at the end
sets the old flag (and the never-declared ROUTING_MODE) and asserts they do
nothing — it is red at 93330e6 and at 21bc4b6, green after the flag's removal.
"""
import types

import pytest

from app.config.config import settings
import app.services.prism_agent_caller as pac


def _box(key, *, enabled=True, running=0, waiting=0, cap=6):
    return types.SimpleNamespace(
        name=key, url=f"http://{key}:8000", enabled=enabled,
        requests_running=running, requests_waiting=waiting,
        max_concurrent=cap, model=None,
    )


def _llm(**boxes):
    return types.SimpleNamespace(_endpoints=boxes)


def _serving(dgx="GLM-5.3-Flash-EXL3", jetson="nemotron35"):
    async def live(url, force_refresh=False):
        if "dgx" in url:
            if isinstance(dgx, Exception):
                raise dgx
            return dgx
        if isinstance(jetson, Exception):
            raise jetson
        return jetson
    return live


@pytest.fixture(autouse=True)
def _pattern(monkeypatch):
    monkeypatch.setattr(settings, "DECISION_MODEL_PATTERN", "deepseek|nemotron|glm")


class TestSaturation:
    def test_full_box_is_saturated(self):
        assert pac.box_is_saturated(_box("dgx_spark", running=6, waiting=0, cap=6))
        assert pac.box_is_saturated(_box("dgx_spark", running=6, waiting=2, cap=6))

    def test_one_free_slot_is_not(self):
        assert not pac.box_is_saturated(_box("dgx_spark", running=5, waiting=0, cap=6))

    def test_a_box_with_no_declared_capacity_never_reads_as_saturated(self):
        assert not pac.box_is_saturated(_box("dgx_spark", running=99, waiting=99, cap=0))

    def test_reads_the_fields_not_getattr_defaults(self):
        """A stub without the metrics must FAIL, not read as idle."""
        with pytest.raises(AttributeError):
            pac.box_is_saturated(types.SimpleNamespace(max_concurrent=6))


class TestOverflow:
    @pytest.mark.asyncio
    async def test_dgx_saturated_overflows_a_decision_agent_to_jetson(self, monkeypatch):
        monkeypatch.setattr(pac, "llm", _llm(
            dgx_spark=_box("dgx_spark", running=6, waiting=2),
            jetson=_box("jetson", cap=8),
        ))
        monkeypatch.setattr(pac, "get_live_model_from_vllm", _serving())
        model, provider = await pac.resolve_default_model_for_agent("v3_regime_engine")
        assert (model, provider) == ("nemotron35", "vllm")

    @pytest.mark.asyncio
    async def test_one_below_capacity_stays_on_dgx(self, monkeypatch):
        monkeypatch.setattr(pac, "llm", _llm(
            dgx_spark=_box("dgx_spark", running=5, waiting=0),
            jetson=_box("jetson", cap=8),
        ))
        monkeypatch.setattr(pac, "get_live_model_from_vllm", _serving())
        model, provider = await pac.resolve_default_model_for_agent("v3_regime_engine")
        assert (model, provider) == ("GLM-5.3-Flash-EXL3", "vllm-2")

    @pytest.mark.asyncio
    async def test_both_saturated_stays_queued_on_dgx(self, monkeypatch):
        """The one deliberate asymmetry: a 490k-token prompt must not hit the 128K box."""
        monkeypatch.setattr(pac, "llm", _llm(
            dgx_spark=_box("dgx_spark", running=6, waiting=3),
            jetson=_box("jetson", running=8, waiting=1, cap=8),
        ))
        monkeypatch.setattr(pac, "get_live_model_from_vllm", _serving())
        model, provider = await pac.resolve_default_model_for_agent("v3_regime_engine")
        assert (model, provider) == ("GLM-5.3-Flash-EXL3", "vllm-2")

    @pytest.mark.asyncio
    async def test_the_overflow_target_is_still_contract_checked(self, monkeypatch):
        monkeypatch.setattr(pac, "llm", _llm(
            dgx_spark=_box("dgx_spark", running=6, waiting=2),
            jetson=_box("jetson", cap=8),
        ))
        monkeypatch.setattr(pac, "get_live_model_from_vllm",
                            _serving(jetson="cyankiwi/Qwen3.6-35B-A3B-AWQ-4bit"))
        model, provider = await pac.resolve_default_model_for_agent("v3_regime_engine")
        assert (model, provider) == ("GLM-5.3-Flash-EXL3", "vllm-2"), \
            "an unapproved model on the overflow box must fall through to the DGX queue"

    @pytest.mark.asyncio
    async def test_overflow_never_applies_to_collectors(self, monkeypatch):
        monkeypatch.setattr(pac, "llm", _llm(
            dgx_spark=_box("dgx_spark", running=6, waiting=2),
            jetson=_box("jetson", running=8, waiting=4, cap=8),
        ))
        monkeypatch.setattr(pac, "get_live_model_from_vllm", _serving())
        model, provider = await pac.resolve_default_model_for_agent("janitor")
        assert (model, provider) == ("nemotron35", "vllm")

    @pytest.mark.asyncio
    async def test_overflow_never_applies_to_an_endpoint_override(self, monkeypatch):
        monkeypatch.setattr(pac, "llm", _llm(
            dgx_spark=_box("dgx_spark", running=6, waiting=2),
            jetson=_box("jetson", cap=8),
        ))
        monkeypatch.setattr(pac, "get_live_model_from_vllm", _serving())
        model, provider = await pac.resolve_default_model_for_agent(
            "v3_regime_engine", endpoint_override="dgx_spark")
        assert (model, provider) == ("GLM-5.3-Flash-EXL3", "vllm-2")

    @pytest.mark.asyncio
    async def test_stale_saturation_on_a_dead_dgx_still_falls_back(self, monkeypatch):
        """`_poll_all_metrics` leaves the last values when a poll fails, so a DGX
        that died while full keeps running=6. The loop must not depend on the
        metrics being zeroed: the model probe fails and the Jetson answers."""
        monkeypatch.setattr(pac, "llm", _llm(
            dgx_spark=_box("dgx_spark", running=6, waiting=0),
            jetson=_box("jetson", running=8, waiting=0, cap=8),
        ))
        monkeypatch.setattr(pac, "get_live_model_from_vllm",
                            _serving(dgx=pac.ModelUnavailableError("HTTP 502")))
        model, provider = await pac.resolve_default_model_for_agent("v3_regime_engine")
        assert (model, provider) == ("nemotron35", "vllm")


class TestTranslatorIsLightWork:
    @pytest.mark.asyncio
    async def test_translator_prefers_jetson_with_the_dgx_idle(self, monkeypatch):
        """The foreign-feed translator resolved as a DECISION agent until
        2026-09-03 (its name matched no collector keyword) and would have
        taken a 1M-context GLM slot for a three-sentence translation."""
        monkeypatch.setattr(pac, "llm", _llm(
            dgx_spark=_box("dgx_spark"), jetson=_box("jetson", cap=8)))
        monkeypatch.setattr(pac, "get_live_model_from_vllm", _serving())
        for name in ("translator", "Translator"):
            model, provider = await pac.resolve_default_model_for_agent(name)
            assert (model, provider) == ("nemotron35", "vllm"), name

    def test_one_keyword_table(self):
        import inspect
        src = inspect.getsource(pac.resolve_default_model_for_agent)
        assert "collector_keywords" not in src, \
            "the resolver must read COLLECTOR_KEYWORDS, not carry its own copy"
        assert pac.is_collector_agent("summarizer_news")
        assert not pac.is_collector_agent("v3_decision_synthesizer")


class TestNoStaticPin:
    """The flag is gone from the code, from the settings object, and from the
    deploy script. These are the three places 2026-09-02 armed it."""

    @pytest.mark.asyncio
    async def test_a_stray_solo_setting_cannot_pin_the_jetson(self, monkeypatch):
        """NEGATIVE CONTROL — red at 93330e6 and at 21bc4b6 (both route to vllm).

        Armed the way production armed it: the env var the container inherits,
        plus a raw attribute on the settings object, because the old code read
        both through `getattr`. `monkeypatch.setattr` is not usable here — a
        pydantic Settings refuses a field that no longer exists, which is
        itself half the proof."""
        monkeypatch.setenv("SOLO_JETSON_MODE", "true")
        monkeypatch.setenv("ROUTING_MODE", "force_jetson")
        settings.__dict__["SOLO_JETSON_MODE"] = True
        settings.__dict__["ROUTING_MODE"] = "force_jetson"
        try:
            monkeypatch.setattr(pac, "llm", _llm(
                dgx_spark=_box("dgx_spark"), jetson=_box("jetson", cap=8)))
            monkeypatch.setattr(pac, "get_live_model_from_vllm", _serving())
            model, provider = await pac.resolve_default_model_for_agent("v3_regime_engine")
            assert (model, provider) == ("GLM-5.3-Flash-EXL3", "vllm-2")

            from unittest.mock import AsyncMock, patch
            from app.services import vllm_hosts as vh
            with patch("app.services.prism_agent_caller.llm",
                       _llm(dgx_spark=_box("dgx_spark"), jetson=_box("jetson"))), \
                 patch("app.services.prism_agent_caller.get_live_model_from_vllm",
                       new=AsyncMock(return_value="GLM-5.3-Flash-EXL3")):
                targets = await vh.vllm_targets()
            assert [t[0] for t in targets] == ["vllm-2", "vllm"], \
                "vllm_targets read the same flag and dropped the DGX"
        finally:
            settings.__dict__.pop("SOLO_JETSON_MODE", None)
            settings.__dict__.pop("ROUTING_MODE", None)

    def test_the_settings_object_has_no_such_field(self):
        assert "SOLO_JETSON_MODE" not in type(settings).model_fields
        assert "ROUTING_MODE" not in type(settings).model_fields

    def test_no_module_under_app_reads_it(self):
        """A mention in prose is fine; a NAME the interpreter resolves is not.

        Parsed with ast so a docstring or comment recording the removal cannot
        satisfy the check and a real reader cannot hide behind one."""
        import ast
        import pathlib as _p

        root = _p.Path(pac.__file__).resolve().parents[1]
        hits = []
        for f in sorted(root.rglob("*.py")):
            src = f.read_text()
            if "SOLO_JETSON_MODE" not in src and "ROUTING_MODE" not in src:
                continue
            try:
                tree = ast.parse(src)
            except SyntaxError:  # pragma: no cover
                continue
            for node in ast.walk(tree):
                # settings.SOLO_JETSON_MODE / getattr(s, "SOLO_JETSON_MODE", ...)
                # / os.environ["SOLO_JETSON_MODE"] all reach one of these.
                names = set()
                if isinstance(node, ast.Attribute):
                    names.add(node.attr)
                elif isinstance(node, ast.Name):
                    names.add(node.id)
                elif isinstance(node, ast.Constant) and isinstance(node.value, str):
                    names.add(node.value)
                if names & {"SOLO_JETSON_MODE", "ROUTING_MODE"}:
                    hits.append(f"{f.relative_to(root)}:{node.lineno}")
        assert hits == [], f"a solo-box reader is back: {hits}"


class TestStartupReadiness:
    @pytest.mark.asyncio
    async def test_one_resolved_box_is_enough(self):
        from app.services.startup_tasks import _resolved_models

        class Shim:
            _endpoints = {"dgx_spark": _box("dgx_spark"), "jetson": _box("jetson")}

            async def _sync_endpoint_model(self, ep, force=False):
                return None if ep.name == "dgx_spark" else "nemotron35"

        assert await _resolved_models(Shim()) == {"jetson": "nemotron35"}

    @pytest.mark.asyncio
    async def test_no_resolved_box_is_empty(self):
        from app.services.startup_tasks import _resolved_models

        class Shim:
            _endpoints = {"dgx_spark": _box("dgx_spark"), "jetson": _box("jetson")}

            async def _sync_endpoint_model(self, ep, force=False):
                return None

        assert await _resolved_models(Shim()) == {}

    def test_readiness_does_not_require_every_box(self):
        import inspect
        from app.services import startup_tasks
        src = inspect.getsource(startup_tasks.startup_vllm_discovery)
        assert "_resolved_models(" in src
        assert "Model not yet resolved for active endpoint" not in src
