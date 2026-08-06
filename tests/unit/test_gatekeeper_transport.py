"""The gatekeeper must reach the model over prism's TOOL-LESS /chat endpoint.

Why this is a test and not a comment: on 2026-08-06 the gatekeeper's empty
responses were "fixed" twice by changing which MODEL it asked for, while the
actual fault was the transport. `/agent` attaches prism's whole MCP catalog
server-side — 275 tools / 91,255 tokens before a single prompt token — and no
request field removes it, because tool attachment is server-side policy.
`enable_tools=False` only stops US sending schemas.

Both model swaps were measured against the live boxes:

  * `endpoint_override="jetson"`  → 0/3 passed, zero tokens in AND out. 91,255
    tool tokens is 1.4x Jetson's 65,536 window, so /agent can never reach that
    box at all — the pin chose the one endpoint guaranteed to fail.
  * default Gold Spark routing    → passed a 10-row watchlist, failed a 40-row
    one 3/3 with 229–1,493 output tokens arriving as EMPTY content.
  * raw tool-less call            → clean JSON on BOTH boxes, every time.

So the failure tracks the transport, not the model, and a future "just pin it
to a better model" edit is the regression this file exists to catch.
"""
import inspect
import re

import pytest

from app.services import pipeline_service


def _gatekeeper_source() -> str:
    """The gatekeeper block only — not the whole 2k-line module.

    Anchored on the agent's own prompt constant and closed at the ticker-
    validation step that follows it, so an unrelated run_agent call elsewhere
    in the pipeline cannot make this pass or fail by accident.
    """
    src = inspect.getsource(pipeline_service)
    start = src.index("Here are {stock_count} candidate stocks")
    end = src.index("Gatekeeper hallucinated tickers", start)
    block = src[start:end]
    # Comment lines are stripped: this block deliberately DOCUMENTS the failed
    # `endpoint_override="jetson"` attempt, and a guard that reads its own
    # prose would fire on the explanation instead of on the code.
    return "\n".join(
        line for line in block.splitlines()
        if not line.lstrip().startswith("#")
    )


def test_gatekeeper_uses_the_toolless_chat_transport():
    block = _gatekeeper_source()
    assert "chat_toolless" in block, (
        "The gatekeeper must call prism_agent_caller.chat_toolless (/chat). "
        "If this moved, update the anchors in _gatekeeper_source."
    )


def test_gatekeeper_does_not_go_through_the_agent_endpoint():
    block = _gatekeeper_source()
    assert not re.search(r"\brun_agent\s*\(", block), (
        "run_agent routes through prism /agent, which attaches 275 tools / "
        "91,255 tokens server-side and returns empty content for this agent. "
        "Use chat_toolless instead — see prism_agent_caller.chat_toolless."
    )


def test_gatekeeper_is_not_pinned_to_an_endpoint():
    """A model/endpoint pin is the wrong shape of fix for this bug.

    Pinning is how it was 'fixed' twice without ever being run once. /chat has
    no tool floor, so it works on either box and needs no pin at all.
    """
    block = _gatekeeper_source()
    assert "endpoint_override" not in block, (
        "The gatekeeper does not need an endpoint pin on the /chat path. "
        'Pinning it to "jetson" on /agent failed 3/3 at zero tokens.'
    )


@pytest.mark.asyncio
async def test_chat_toolless_posts_to_chat_and_never_to_agent(monkeypatch):
    """Pin the URL and the payload shape, so a refactor cannot silently
    re-route this onto /agent (where it would break again)."""
    from app.services import prism_agent_caller

    seen = {}

    class _Resp:
        def raise_for_status(self):
            return None

        async def aiter_lines(self):
            yield 'data: {"type":"chunk","content":"{\\"selected_tickers\\":"}'
            yield 'data: {"type":"chunk","content":"[\\"NVDA\\"]}"}'
            yield ('data: {"type":"done","model":"m","provider":"p",'
                   '"usage":{"inputTokens":10,"outputTokens":5}}')

    class _Stream:
        async def __aenter__(self):
            return _Resp()

        async def __aexit__(self, *a):
            return False

    class _Client:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        def stream(self, method, url, json=None):
            seen["method"], seen["url"], seen["json"] = method, url, json
            return _Stream()

    import httpx
    monkeypatch.setattr(httpx, "AsyncClient", _Client)

    out = await prism_agent_caller.chat_toolless(
        provider="vllm-2", model="deepseek-v4-flash-0731",
        system_prompt="sys", user_prompt="usr",
        max_tokens=4096, timeout_seconds=5.0,
    )

    assert seen["url"].endswith("/chat"), seen["url"]
    assert "/agent" not in seen["url"], (
        "chat_toolless must never hit /agent — that endpoint's 91k-token tool "
        "catalog is the bug it exists to avoid."
    )
    # thinking-off is carried explicitly: registration-level defaults are
    # ignored by prism, and this is the flag the 08-06 misdiagnosis blamed.
    assert seen["json"]["thinkingEnabled"] is False
    # No tool keys are sent at all — sending them is what pulls in the catalog.
    assert "tools" not in seen["json"] and "enabledTools" not in seen["json"]

    assert out["response"] == '{"selected_tickers":["NVDA"]}'
    assert out["tokens_used"] == 15
    assert out["model_used"] == "m" and out["provider"] == "p"
