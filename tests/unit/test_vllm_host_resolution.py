"""
Tests for vLLM chat-host resolution (app/services/vllm_hosts.py).

Host discovery used to live in the scraper's vision engine, which put an
``app.services.prism_agent_caller`` import inside the subtree that
scraper-service ships standalone — so it raised ImportError in every deployed
scraper. The OCR engine was retired on 2026-08-09 and discovery moved here,
next to the config layer it actually reads.

The subtree-import guard that outage motivated now lives in
``tests/unit/test_scraper_subtree_import_closure.py`` — it outgrew this file
when it was rewritten as an allowlist.

The predecessor's own bug is still worth guarding: VISION_MODEL was unset, so
the engine defaulted to ``openai/gpt-4o``, every call reached prism as provider
"openai", and prism answered ``500 {"message":"OPENAI_API_KEY is not set"}``.
Resolution must target the local vLLM hosts and never invent a provider.
"""
from unittest.mock import AsyncMock, patch

import pytest

from app.services import vllm_hosts as vh


class _Endpoint:
    def __init__(self, url, enabled=True):
        self.url = url
        self.enabled = enabled


def _patched_llm(**endpoints):
    if not endpoints:
        endpoints = {
            "dgx_spark": _Endpoint("http://10.0.0.141:8000"),
            "jetson": _Endpoint("http://10.0.0.30:8000"),
        }
    return patch("app.services.prism_agent_caller.llm",
                 type("L", (), {"_endpoints": endpoints}))


@pytest.mark.asyncio
async def test_prefers_gold_spark_and_discovers_the_served_model():
    with _patched_llm(), \
         patch("app.services.prism_agent_caller.get_live_model_from_vllm",
               new=AsyncMock(return_value="google/gemma-4-26B-A4B-it")):
        targets = await vh.vllm_targets()

    # "vllm-2" is prism's label for the DGX Spark, not a model vendor.
    assert [t[0] for t in targets] == ["vllm-2", "vllm"], "Gold Spark must lead"
    assert targets[0][1] == "google/gemma-4-26B-A4B-it"
    assert all(t[0] != "openai" for t in targets), \
        "the OpenAI default is what made the old OCR path 500"


@pytest.mark.asyncio
async def test_targets_expose_the_endpoint_url_for_direct_calls():
    """Callers post to vLLM directly, so the base URL must ride with the target."""
    with _patched_llm(), \
         patch("app.services.prism_agent_caller.get_live_model_from_vllm",
               new=AsyncMock(return_value="google/gemma-4-26B-A4B-it")):
        targets = await vh.vllm_targets()

    assert targets[0][2] == "http://10.0.0.141:8000"
    assert targets[1][2] == "http://10.0.0.30:8000"


@pytest.mark.asyncio
async def test_only_is_a_hard_pin_not_a_preference():
    """The backfill worker depends on this: "the Jetson is down" must stop
    low-priority work, never redirect it onto the box the cycle is using."""
    with _patched_llm(), \
         patch("app.services.prism_agent_caller.get_live_model_from_vllm",
               new=AsyncMock(return_value="cyankiwi/Qwen3.6-35B-A3B-AWQ-4bit")):
        targets = await vh.vllm_targets(only=("jetson",))

    assert [t[0] for t in targets] == ["vllm"], "dgx_spark must be removed, not demoted"


@pytest.mark.asyncio
async def test_a_down_host_is_skipped_but_the_others_still_serve():
    async def _live(url, force_refresh=False):
        if "141" in url:
            raise RuntimeError("VLLM endpoint offline")
        return "cyankiwi/Qwen3.6-35B-A3B-AWQ-4bit"

    with _patched_llm(), \
         patch("app.services.prism_agent_caller.get_live_model_from_vllm", new=_live):
        targets = await vh.vllm_targets()

    assert [t[0] for t in targets] == ["vllm"]


@pytest.mark.asyncio
async def test_raises_when_no_endpoint_is_available():
    """Failing loudly beats silently falling back to a provider with no key."""
    with _patched_llm(dgx_spark=_Endpoint("http://x", enabled=False),
                      jetson=_Endpoint("")):
        with pytest.raises(RuntimeError, match="No vLLM endpoint available"):
            await vh.vllm_targets()


@pytest.mark.asyncio
async def test_every_host_down_raises_rather_than_returning_empty():
    async def _live(url, force_refresh=False):
        raise RuntimeError("VLLM endpoint offline")

    with _patched_llm(), \
         patch("app.services.prism_agent_caller.get_live_model_from_vllm", new=_live):
        with pytest.raises(RuntimeError, match="No vLLM endpoint available"):
            await vh.vllm_targets()

