"""A model swap must not be answered with the model that was swapped OUT.

2026-09-12: the operator loaded GLM on Gold Spark. The box served
`GLM-5.3-Flash-EXL3` — verified directly against
`/vllm-shim/gold-spark/v1/models` — while trading-service went on asking for
`deepseek-v4-flash-0731`, and every call came back
`502 vllm-shim upstream failure`.

The cache is why, and the reasoning in its own comment is what made it
invisible:

    "A box's model id changes only when someone reloads it, so a stale answer
     is nearly always the right answer"

**A reload is exactly what makes the probe fail.** The box is down while it
swaps, the probe times out, and the grace window opens — precisely at the
moment the cached name has become wrong. The premise was inverted, and at
`_STALE_MODEL_GRACE_S = 3600` it stayed wrong for an hour.

These tests pin the three properties that make a reload survivable:
the grace is shorter than a model load, a known-bad id is dropped rather than
re-served, and the pre-flight asks the box rather than the cache.
"""

from __future__ import annotations

import inspect

import pytest

from app.services import prism_agent_caller as pac


@pytest.mark.asyncio
async def test_a_failed_fresh_probe_never_serves_cached_identity(monkeypatch):
    import time
    from unittest.mock import AsyncMock, MagicMock
    url = "http://box.invalid"
    pac._dynamic_model_cache[url] = ("removed-model", time.time())
    client = MagicMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock()
    client.get = AsyncMock(side_effect=TimeoutError("reloading"))
    monkeypatch.setattr(pac.httpx, "AsyncClient", lambda **kwargs: client)
    with pytest.raises(pac.ModelUnavailableError):
        await pac.get_live_model_from_vllm(url, force_refresh=True)
    assert url not in pac._dynamic_model_cache


def test_a_known_bad_model_id_is_dropped_not_re_served():
    """`invalidate_model_cache` must actually forget, or the next call repeats."""
    url = "http://box.invalid:5591/vllm-shim/gold-spark"
    pac._dynamic_model_cache[url] = ("deepseek-v4-flash-0731", 1.0)
    pac.invalidate_model_cache(url)
    assert url not in pac._dynamic_model_cache, (
        "the swapped-out id is still cached; the next resolve will serve it again"
    )


def test_invalidate_clears_everything_when_given_no_url():
    pac._dynamic_model_cache["a"] = ("m1", 1.0)
    pac._dynamic_model_cache["b"] = ("m2", 1.0)
    pac.invalidate_model_cache()
    assert not pac._dynamic_model_cache


@pytest.mark.parametrize("fn_name", ["llm_can_answer", "tool_calls_are_parsed"])
def test_preflight_asks_the_box_not_the_cache(fn_name):
    """The gate that decides whether a cycle may START must force a refresh.

    Resolving from cache lets a cycle begin against a model the box unloaded an
    hour ago — the gate then passes on evidence about the past.
    """
    from app.services import llm_preflight

    # No skip-if-absent: both names exist today, and a skip would turn a
    # renamed-away probe into a silent pass.
    fn = getattr(llm_preflight, fn_name)
    src = inspect.getsource(fn)
    assert "resolve_default_model_for_agent" in src, (
        f"{fn_name} no longer resolves a model — this test is watching the "
        f"wrong function and must be updated, not deleted"
    )
    assert "force_refresh=True" in src, (
        f"{fn_name} resolves the model WITHOUT force_refresh, so the pre-flight "
        f"can pass on a cached id for a model the box no longer serves"
    )


def test_the_fixture_can_tell_a_forced_resolve_from_a_cached_one():
    """Non-vacuity control: the signature must actually carry the parameter."""
    sig = inspect.signature(pac.resolve_default_model_for_agent)
    assert "force_refresh" in sig.parameters
    assert sig.parameters["force_refresh"].default is False, (
        "if the default were True these assertions would pass trivially"
    )
