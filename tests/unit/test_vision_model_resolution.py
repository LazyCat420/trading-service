"""
Tests for vision OCR model resolution (app/scraper/engines/vision_engine.py).

Vision scraping had never worked in trading-service: VISION_MODEL was unset so
the engine defaulted to ``openai/gpt-4o``, every OCR call reached prism as
provider "openai", and prism answered
``500 {"message":"OPENAI_API_KEY is not set"}``. Resolution now targets the
local vision-capable vLLM hosts (Gold Spark / Jetson).
"""
from unittest.mock import AsyncMock, patch

import pytest

from app.scraper.engines import vision_engine as ve


class _Endpoint:
    def __init__(self, url, enabled=True):
        self.url = url
        self.enabled = enabled


def _endpoints(**kw):
    return kw


@pytest.mark.asyncio
async def test_prefers_gold_spark_and_discovers_the_served_model(monkeypatch):
    monkeypatch.delenv("VISION_MODEL", raising=False)
    fake_llm = type("L", (), {"_endpoints": _endpoints(
        dgx_spark=_Endpoint("http://10.0.0.141:8000"),
        jetson=_Endpoint("http://10.0.0.30:8000"),
    )})

    with patch("app.services.prism_agent_caller.llm", fake_llm), \
         patch("app.services.prism_agent_caller.get_live_model_from_vllm",
               new=AsyncMock(return_value="google/gemma-4-26B-A4B-it")):
        provider, model = await ve._resolve_vision_model()

    # "vllm-2" is prism's label for the DGX Spark, not a model vendor.
    assert provider == "vllm-2"
    assert model == "google/gemma-4-26B-A4B-it"
    assert provider != "openai", "the OpenAI default is what made OCR 500"


@pytest.mark.asyncio
async def test_falls_back_to_jetson_when_gold_spark_is_down(monkeypatch):
    monkeypatch.delenv("VISION_MODEL", raising=False)
    fake_llm = type("L", (), {"_endpoints": _endpoints(
        dgx_spark=_Endpoint("http://10.0.0.141:8000"),
        jetson=_Endpoint("http://10.0.0.30:8000"),
    )})

    async def _live(url, force_refresh=False):
        if "141" in url:
            raise RuntimeError("VLLM endpoint offline")
        return "cyankiwi/Qwen3.6-35B-A3B-AWQ-4bit"

    with patch("app.services.prism_agent_caller.llm", fake_llm), \
         patch("app.services.prism_agent_caller.get_live_model_from_vllm", new=_live):
        provider, model = await ve._resolve_vision_model()

    assert provider == "vllm"
    assert model == "cyankiwi/Qwen3.6-35B-A3B-AWQ-4bit"


@pytest.mark.asyncio
async def test_raises_when_no_endpoint_is_available(monkeypatch):
    """Failing loudly beats silently falling back to a provider with no key."""
    monkeypatch.delenv("VISION_MODEL", raising=False)
    fake_llm = type("L", (), {"_endpoints": _endpoints(
        dgx_spark=_Endpoint("http://x", enabled=False),
        jetson=_Endpoint(""),
    )})

    with patch("app.services.prism_agent_caller.llm", fake_llm):
        with pytest.raises(RuntimeError, match="No vision-capable vLLM endpoint"):
            await ve._resolve_vision_model()


def _patched_llm():
    return patch("app.services.prism_agent_caller.llm", type("L", (), {"_endpoints": _endpoints(
        dgx_spark=_Endpoint("http://10.0.0.141:8000"),
        jetson=_Endpoint("http://10.0.0.30:8000"),
    )}))


@pytest.mark.asyncio
async def test_override_with_provider_prefix_is_split_once(monkeypatch):
    """Model ids contain slashes, so only a known provider prefix may be split."""
    monkeypatch.setenv("VISION_MODEL", "vllm-2/google/gemma-4-26B-A4B-it")
    with _patched_llm():
        provider, model = await ve._resolve_vision_model()
    assert provider == "vllm-2"
    assert model == "google/gemma-4-26B-A4B-it"


@pytest.mark.asyncio
async def test_override_without_known_provider_is_treated_as_a_model_id(monkeypatch):
    """A bare split would read "google" as the provider and send a bogus model."""
    monkeypatch.setenv("VISION_MODEL", "google/gemma-4-26B-A4B-it")
    with _patched_llm():
        provider, model = await ve._resolve_vision_model()
    assert provider == "vllm-2"
    assert model == "google/gemma-4-26B-A4B-it"


@pytest.mark.asyncio
async def test_targets_expose_the_endpoint_url_for_direct_calls(monkeypatch):
    """OCR posts to vLLM directly, so the base URL must ride with the target."""
    monkeypatch.delenv("VISION_MODEL", raising=False)
    with _patched_llm(), \
         patch("app.services.prism_agent_caller.get_live_model_from_vllm",
               new=AsyncMock(return_value="google/gemma-4-26B-A4B-it")):
        targets = await ve._vision_targets()

    assert [t[0] for t in targets] == ["vllm-2", "vllm"], "Gold Spark must lead"
    assert targets[0][2] == "http://10.0.0.141:8000"
    assert targets[1][2] == "http://10.0.0.30:8000"


def test_truncation_notice_is_recognised():
    """Prism's cut-short notice is ~160 chars and would pass the >100 check."""
    notice = (
        "⚠️ The model's response was cut short because the **max_tokens** limit "
        "was reached before it could finish generating. Try increasing the "
        "**Max Tokens** setting."
    )
    assert len(notice) > 100, "guard is only needed because it clears the length check"
    assert ve._PRISM_TRUNCATION_MARKER in notice


# ── OCR response handling ────────────────────────────────────────────────────

def _chat_response(content: str) -> dict:
    return {"choices": [{"message": {"content": content}}]}


class _FakeClient:
    """Minimal async httpx.AsyncClient stand-in returning queued responses."""

    def __init__(self, queue):
        self._queue = queue
        self.posted = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, json=None, **kw):
        self.posted.append((url, json))
        item = self._queue.pop(0)
        if isinstance(item, Exception):
            raise item

        class _R:
            def raise_for_status(self_inner):
                return None

            def json(self_inner):
                return item

        return _R()


def _patch_targets(targets):
    return patch.object(ve, "_vision_targets", new=AsyncMock(return_value=targets))


TWO_HOSTS = [
    ("vllm-2", "google/gemma-4-26B-A4B-it", "http://10.0.0.141:8000"),
    ("vllm", "cyankiwi/Qwen3.6-35B-A3B-AWQ-4bit", "http://10.0.0.30:8000"),
]


@pytest.mark.asyncio
async def test_block_sentinel_returns_none_without_failing_over():
    """A bot-wall is not a host problem — re-reading it on the Jetson is waste."""
    client = _FakeClient([_chat_response("BLOCKED_OR_EMPTY")])
    with _patch_targets(TWO_HOSTS), patch("httpx.AsyncClient", return_value=client):
        out = await ve._ocr_with_openai([b"img"], "")

    assert out is None
    assert len(client.posted) == 1, "must not retry the other host on a blocked page"


@pytest.mark.asyncio
async def test_block_page_text_is_never_returned_as_content():
    """This is the data-poisoning case: the notice is >100 chars and looked valid."""
    block_page = (
        "Access is temporarily restricted\n\nWe detected unusual activity from your "
        "device or network.\n\nReasons may include:\n- Rapid taps or clicks\n"
        "- JavaScript disabled or not working\n- Automated (bot) activity"
    )
    assert len(block_page) > 100
    client = _FakeClient([_chat_response("BLOCKED_OR_EMPTY\n" + block_page)])
    with _patch_targets(TWO_HOSTS), patch("httpx.AsyncClient", return_value=client):
        out = await ve._ocr_with_openai([b"img"], "")

    assert out is None, "the sentinel must win even when prose follows it"


@pytest.mark.asyncio
async def test_empty_content_fails_over_to_the_second_host():
    """Budget spent entirely on reasoning is a host-level failure, so retry."""
    client = _FakeClient([_chat_response(""), _chat_response("A" * 500)])
    with _patch_targets(TWO_HOSTS), patch("httpx.AsyncClient", return_value=client):
        out = await ve._ocr_with_openai([b"img"], "")

    assert out == "A" * 500
    assert len(client.posted) == 2
    assert client.posted[1][0].startswith("http://10.0.0.30:8000")


@pytest.mark.asyncio
async def test_default_prompt_requests_primary_content_and_the_sentinel():
    client = _FakeClient([_chat_response("B" * 400)])
    with _patch_targets(TWO_HOSTS), patch("httpx.AsyncClient", return_value=client):
        await ve._ocr_with_openai([b"img"], "")

    sent = client.posted[0][1]["messages"][0]["content"][0]["text"]
    assert "PRIMARY content" in sent
    assert ve._NO_CONTENT_SENTINEL in sent
    assert "EXCLUDE" in sent, "nav/ads exclusion is the whole quality win"


@pytest.mark.asyncio
async def test_explicit_prompt_overrides_the_default():
    client = _FakeClient([_chat_response("C" * 400)])
    with _patch_targets(TWO_HOSTS), patch("httpx.AsyncClient", return_value=client):
        await ve._ocr_with_openai([b"img"], "just read the table")

    sent = client.posted[0][1]["messages"][0]["content"][0]["text"]
    assert sent == "just read the table"
