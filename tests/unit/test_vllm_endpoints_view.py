"""GET /api/v1/vllm/endpoints reports what the box says, never a default.

At 21bc4b6 the route read role/purpose/auto_disabled/loading/active_count/
max_model_len through `getattr(ep, name, default)` on a VLLMEndpoint that has
none of those fields, so the DGX Spark — whose /v1/models says
max_model_len=1000000 — was reported as a 128,000-token box. A number nobody
measured, served as if it had been.
"""
import json
import types

import pytest

import app.services.prism_agent_caller as pac
from app.routers import vllm_router


REAL_KEYS = {
    "name", "url", "max_concurrent", "enabled", "model", "max_model_len",
    "cache_usage", "requests_running", "requests_waiting",
}


def test_the_view_carries_only_real_fields(monkeypatch):
    ep = pac.VLLMEndpoint(name="dgx_spark", url="http://dgx:8000", max_concurrent=6)
    monkeypatch.setattr(vllm_router, "llm", types.SimpleNamespace(_endpoints={"dgx_spark": ep}))
    view = vllm_router.vllm_endpoints()["dgx_spark"]
    assert set(view) == REAL_KEYS, sorted(set(view) ^ REAL_KEYS)
    assert view["max_model_len"] is None, "unsynced must read as unknown, not 128000"


@pytest.mark.asyncio
async def test_sync_fills_max_model_len_from_v1_models(monkeypatch):
    ep = pac.VLLMEndpoint(name="dgx_spark", url="http://dgx:8000", max_concurrent=6)

    class _Resp:
        status_code = 200

        def json(self):
            return {"data": [{"id": "GLM-5.3-Flash-EXL3", "max_model_len": 1000000}]}

    class _Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url):
            assert url.endswith("/v1/models")
            return _Resp()

    import httpx
    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    shim = pac.PrismLLMShim.__new__(pac.PrismLLMShim)
    model = await shim._sync_endpoint_model(ep, force=True)
    assert model == "GLM-5.3-Flash-EXL3"
    assert ep.max_model_len == 1_000_000

    monkeypatch.setattr(vllm_router, "llm", types.SimpleNamespace(_endpoints={"dgx_spark": ep}))
    assert json.loads(json.dumps(vllm_router.vllm_endpoints()))["dgx_spark"]["max_model_len"] == 1_000_000


def test_the_poller_keeps_the_model_current():
    """A model swap on a box must reach the view without a container restart."""
    import inspect
    src = inspect.getsource(pac.PrismLLMShim._poll_all_metrics)
    assert "_sync_endpoint_model(ep)" in src
