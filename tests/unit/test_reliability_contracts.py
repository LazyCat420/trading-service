import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
import pytest
from app.v3.usage_accounting import summarize_usage


def test_output_accounting_sums_repairs_and_distinguishes_missing():
    rows = [{"completion_tokens": 10, "usage_requests": 1}, {"completion_tokens": 20, "usage_requests": 1}]
    assert summarize_usage(rows)["completion_tokens"] == 30
    assert summarize_usage(rows)["usage_coverage"] == "complete"
    assert summarize_usage(rows + [{}])["usage_coverage"] == "partial"
    assert summarize_usage([{}])["completion_tokens"] is None
    assert summarize_usage([{"completion_tokens": 0, "usage_requests": 1}])["completion_tokens"] == 0


@pytest.mark.asyncio
async def test_runner_persists_repair_usage():
    from app.v3.agent_runner import run_v3_agent
    from app.v3.shared_desk import SharedDesk
    class Agent:
        AGENT_NAME = "v3_junior_analyst"
        ARTIFACT_TYPE = "desk_note"
        TOOL_WHITELIST = ["get_sec_filings"]
        SYSTEM_PROMPT = "Return JSON"
    desk = SharedDesk(ticker="WFC", cycle_id="cycle-test")
    rows = [{"response": "I need to read the filing.", "tokens_used": 100, "completion_tokens": 10, "usage_requests": 1},
            {"response": json.dumps({"summary": "Margins stable.", "key_findings": ["FCF positive"], "data_gaps": [], "confidence": 65}),
             "tokens_used": 200, "completion_tokens": 20, "usage_requests": 1}]
    with patch("app.agents.base_agent.run_agent", AsyncMock(side_effect=rows)):
        await run_v3_agent(desk=desk, agent_module=Agent, cycle_id="cycle-test", bot_id="test")
    assert desk.agent_telemetry[-1]["token_usage"] == 300
    assert desk.agent_telemetry[-1]["completion_tokens"] == 30


@pytest.mark.parametrize("value", ['literal ,} and ,]', 'an escaped "quote" and \\ path', 'price {USD,}'])
def test_delimiter_repair_preserves_authored_strings(value):
    from app.utils.text_utils import repair_delimiters_and_parse
    original = {"summary": value, "confidence": 60}
    broken = json.dumps(original)[:-1] + ',}'
    assert repair_delimiters_and_parse(broken) == original


def test_domain_failures_are_isolated_and_recover(monkeypatch):
    from app.collectors import news_collector as nc
    clock = [100.0]
    monkeypatch.setattr(nc.time, "monotonic", lambda: clock[0])
    b = nc._ScrapeBreaker(cooldown_s=10)
    for _ in range(3): b.record(False, domain="blocked.example")
    assert not b.allow("blocked.example")
    assert b.allow("healthy.example")
    clock[0] += 11
    assert b.allow("blocked.example")
    assert not b.allow("blocked.example")
    b.record(True, domain="blocked.example")
    assert b.allow("blocked.example")
    for _ in range(3): b.record(False, domain="a", service_failure=True)
    assert not b.allow("healthy.example")
    b.record(True, domain="b")
    assert b.allow("healthy.example")


@pytest.mark.asyncio
async def test_sweep_breakers_are_task_local():
    from app.collectors import news_collector as nc
    async def worker():
        own = nc._reset_sweep_breaker()
        await asyncio.sleep(0)
        return own is nc._SWEEP_BREAKER.get()
    assert all(await asyncio.gather(worker(), worker()))


def test_external_model_name_does_not_invent_hardware():
    from app.routers.cycle_replay_router import _derive_cycle_box
    assert _derive_cycle_box({"openrouter"}, {"qwen/model"})[0] == "unknown"
    assert _derive_cycle_box(set(), {"nemotron35"})[0] == "unknown"


@pytest.mark.asyncio
async def test_fresh_discovery_handles_swaps_and_context(monkeypatch):
    from app.services import prism_agent_caller as pac
    pac._dynamic_model_cache.clear()
    ep = pac.VLLMEndpoint("jetson", "http://box", 2)
    monkeypatch.setattr(pac, "llm", SimpleNamespace(_endpoints={"jetson": ep}))
    catalog = [{"id": "arbitrary-first", "max_model_len": 128000}]
    class Client:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): pass
        async def get(self, url):
            return SimpleNamespace(status_code=200, json=lambda: {"data": list(catalog)})
    monkeypatch.setattr(pac.httpx, "AsyncClient", lambda **kw: Client())
    assert await pac.get_live_model_from_vllm(ep.url, True) == "arbitrary-first"
    catalog[:] = [{"id": "replacement-unknown-family", "max_model_len": 64000}]
    assert await pac.get_live_model_from_vllm(ep.url, True) == "replacement-unknown-family"
    assert ep.max_model_len == 64000
    catalog[:] = []
    with pytest.raises(pac.ModelUnavailableError):
        await pac.get_live_model_from_vllm(ep.url, True)
    assert ep.url not in pac._dynamic_model_cache


@pytest.mark.asyncio
async def test_override_cannot_repair_a_mismatched_provider(monkeypatch):
    from app.services import prism_agent_caller as pac
    monkeypatch.setattr(pac, "resolve_default_model_for_agent", AsyncMock(return_value=("fresh", "vllm-2")))
    with pytest.raises(pac.ModelContractError):
        await pac.resolve_requested_model("agent", "obsolete")
    assert await pac.resolve_requested_model("agent", "fresh") == ("fresh", "vllm-2")


def test_unknown_context_does_not_borrow_a_different_models_budget():
    from app.config import context_budget as cb
    cb.register_model_context("large-test-model", 850000)
    assert cb.get_context_budget("never-discovered").model_id == "default"


def test_evidence_does_not_count_deterministic_helpers_or_invent_coverage():
    from app.services.cycle_evidence import build_evidence
    evidence = build_evidence({"cycle_id": "test", "analysis_results_count": 1},
        [{"agent_name": "helper", "token_usage": 0, "elapsed_ms": 0},
         {"model_used": "any", "completion_tokens": 0, "usage_requests": 1, "token_usage": 50}],
        [{"ticker": "TEST", "result_json": {"action": "HOLD"}}],
        [{"detail": {"repaired": True}}])
    assert evidence["usage"]["model_runs"] == 1
    assert evidence["usage"]["completion_tokens_measured"] == 0
    assert evidence["usage"]["coverage"] == "partial"  # historical total is not certified complete
    assert evidence["summary_count_matches"]
    assert evidence["artifact_recovery"]["recovered_events"] == 1


@pytest.mark.asyncio
async def test_concurrent_forced_discovery_coalesces_and_rejects_ambiguity(monkeypatch):
    from app.services import prism_agent_caller as pac
    pac._dynamic_model_cache.clear()
    requests = []
    catalog = [{"id": "new-unfamiliar-model", "max_model_len": 64000}]
    class Client:
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def get(self, url):
            requests.append(url)
            await asyncio.sleep(0)
            return SimpleNamespace(status_code=200, json=lambda: {"data": list(catalog)})
    monkeypatch.setattr(pac.httpx, "AsyncClient", lambda **kwargs: Client())
    results = await asyncio.gather(*(pac.get_live_model_from_vllm("http://coalesce", True) for _ in range(12)))
    assert results == ["new-unfamiliar-model"] * 12
    assert len(requests) == 1
    catalog.append({"id": "another-model"})
    with pytest.raises(pac.ModelUnavailableError):
        await pac.get_live_model_from_vllm("http://coalesce", True)


def test_evidence_attributes_attempt_tokens_to_the_served_box():
    from app.services.cycle_evidence import build_evidence
    row = {"model_used": "final", "token_usage": 300, "usage_attempts": [
        {"model_used": "first", "provider": "vllm", "tokens_used": 100},
        {"model_used": "final", "provider": "vllm-2", "tokens_used": 200}]}
    evidence = build_evidence({}, [row], [], [])
    assert {(r["provider"], r["model"], r["tokens"]) for r in evidence["models"]} == {
        ("vllm", "first", 100), ("vllm-2", "final", 200)}
    assert all(r["latency_coverage"] == "partial" for r in evidence["models"])


def test_production_harness_and_workflows_have_no_literal_model_selection():
    import ast
    from pathlib import Path
    root = Path(__file__).resolve().parents[2] / "app"
    files = list((root / "agents").rglob("*.py")) + list((root / "v3").rglob("*.py"))
    assert len(files) > 30
    for path in files:
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Call):
                for kw in node.keywords:
                    if kw.arg in {"model", "model_override", "model_name"} and isinstance(kw.value, ast.Constant):
                        assert not isinstance(kw.value.value, str) or not kw.value.value, f"{path}:{node.lineno}: literal model selector"
