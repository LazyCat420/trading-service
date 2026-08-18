"""A search that matched nothing is not a search that is broken.

Both RSS providers return HTTP 200 with zero <item>s when a query is too
specific, so nothing raises and the error list stays empty. lazy_web_search
reported that as "Web search unavailable — every provider failed" 19 times
in the 14 days to 2026-07-31, on queries like "TSMC TSM dividend history
buyback share count trend 2022 2023 2024 2025 2026". The agent was told the
web was down and retried something equally long.

The distinction is load-bearing beyond the wording: `degraded` is what the
QUANT_ONLY triage gate reads to refuse to conclude "no catalyst" from a
broken tool, and `status == "error"` is what base_agent counts as a tool
failure. A genuinely empty result is evidence and must be neither.
"""

import json

from app.tools import web_tools


async def _no_results(client, query, limit):
    return []


async def _raises(client, query, limit):
    raise ConnectionError("egress blocked")


async def test_zero_matches_is_empty_not_an_outage(monkeypatch):
    monkeypatch.setattr(web_tools, "_search_bing_news", _no_results)
    monkeypatch.setattr(web_tools, "_search_gnews", _no_results)
    out = json.loads(await web_tools.lazy_web_search("a b c d e f g h"))
    assert out["status"] == "empty"
    assert out["degraded"] is False
    # The message must tell the agent what to do differently.
    assert "Retry with" in out["message"]
    assert "8 words" in out["message"]


async def test_a_real_provider_failure_is_still_an_outage(monkeypatch):
    monkeypatch.setattr(web_tools, "_search_bing_news", _raises)
    monkeypatch.setattr(web_tools, "_search_gnews", _raises)
    out = json.loads(await web_tools.lazy_web_search("AAPL earnings"))
    assert out["status"] == "error"
    assert out["degraded"] is True
    assert "ConnectionError" in out["message"]


async def test_an_outage_never_reports_the_empty_wording(monkeypatch):
    """The two branches must not converge: 'no provider returned results'
    was the placeholder that made an outage and an empty search identical."""
    monkeypatch.setattr(web_tools, "_search_bing_news", _raises)
    monkeypatch.setattr(web_tools, "_search_gnews", _raises)
    out = json.loads(await web_tools.lazy_web_search("AAPL"))
    assert "no provider returned results" not in out["message"]
