"""
Tests for grounded news-fact extraction (app/services/news_extraction.py).

The load-bearing piece is align_quote: a fact only survives if its quote can
be located in the source article. That alignment is the anti-hallucination
filter (langextract's char_interval idea), so it gets the deepest coverage.
"""
import json
from unittest.mock import AsyncMock, patch

import pytest

from app.services import news_extraction as ne


ARTICLE = (
    "Microsoft Corp (MSFT) reported fiscal Q2 revenue of $62.0 billion, up 18% "
    "year over year, beating analyst consensus of $61.1 billion. CEO Satya "
    "Nadella said the company's AI products — now embedded across the cloud "
    "portfolio — drove “strong momentum” in enterprise adoption.\n\n"
    "Shares rose 2.4% in extended trading. The company guided Q3 revenue to "
    "$60.5–$61.5 billion, slightly below some estimates. Analysts at two firms "
    "reiterated buy ratings following the report, citing cloud margin expansion."
)

# The extractor skips sub-400-char rows as title-only; the fixture must be a
# real article by that definition or every call-path test silently no-ops.
assert len(ARTICLE) >= 400


# ── align_quote ──────────────────────────────────────────────────────────────

def test_exact_quote_aligns_with_correct_offsets():
    q = "reported fiscal Q2 revenue of $62.0 billion"
    span = ne.align_quote(ARTICLE, q)
    assert span is not None
    start, end = span
    assert ARTICLE[start:end] == q


def test_whitespace_and_case_drift_still_aligns():
    # LLMs routinely collapse newlines and normalize case when quoting.
    q = "shares rose 2.4%  in extended trading"
    span = ne.align_quote(ARTICLE, q)
    assert span is not None
    start, end = span
    assert "Shares rose 2.4% in extended trading" in ARTICLE[start:end]


def test_curly_quote_and_dash_drift_still_aligns():
    # Article has “strong momentum” (curly) and $60.5–$61.5 (en-dash); models
    # typically emit straight quotes and hyphens.
    span = ne.align_quote(ARTICLE, 'drove "strong momentum" in enterprise adoption')
    assert span is not None
    span2 = ne.align_quote(ARTICLE, "guided Q3 revenue to $60.5-$61.5 billion")
    assert span2 is not None


def test_fabricated_quote_is_rejected():
    """The whole point: evidence the article doesn't contain must not ground."""
    assert ne.align_quote(ARTICLE, "revenue collapsed 40% amid mass layoffs") is None


def test_too_short_or_empty_quotes_are_rejected():
    assert ne.align_quote(ARTICLE, "revenue") is None  # < 12 chars: not evidence
    assert ne.align_quote(ARTICLE, "") is None
    assert ne.align_quote("", "reported fiscal Q2 revenue") is None


# ── extract_article_facts ────────────────────────────────────────────────────

def _chat_response(content: str) -> dict:
    return {"choices": [{"message": {"content": content}}]}


class _FakeClient:
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
            def raise_for_status(self_i):
                return None

            def json(self_i):
                return item

        return _R()


TARGETS = [("vllm-2", "google/gemma-4-26B-A4B-it", "http://10.0.0.141:8000")]


def _patch_targets():
    return patch.object(ne, "_chat_targets", new=AsyncMock(return_value=TARGETS))


@pytest.mark.asyncio
async def test_grounded_facts_survive_and_fabricated_facts_are_dropped():
    model_out = json.dumps({"facts": [
        {"class": "earnings",
         "statement": "Q2 revenue beat",
         "quote": "reported fiscal Q2 revenue of $62.0 billion, up 18% year over year",
         "direction": "bullish"},
        {"class": "guidance",
         "statement": "FABRICATED — no such text in the article",
         "quote": "announced a $10 billion share buyback program",
         "direction": "bullish"},
    ]})
    client = _FakeClient([_chat_response(model_out)])
    with _patch_targets(), patch("httpx.AsyncClient", return_value=client):
        facts = await ne.extract_article_facts(ARTICLE, "MSFT", "MSFT Q2")

    assert facts is not None
    assert len(facts) == 1, "the fabricated fact must be dropped by alignment"
    assert facts[0]["class"] == "earnings"
    assert facts[0]["char_start"] >= 0
    assert ARTICLE[facts[0]["char_start"]:facts[0]["char_end"]].startswith(
        "reported fiscal Q2"
    )


@pytest.mark.asyncio
async def test_short_article_is_skipped_without_llm_call():
    with _patch_targets() as tgt:
        out = await ne.extract_article_facts("Too short.", "MSFT")
    assert out is None
    tgt.assert_not_called()


@pytest.mark.asyncio
async def test_llm_failure_returns_none_for_raw_fallback():
    client = _FakeClient([RuntimeError("connection refused")])
    with _patch_targets(), patch("httpx.AsyncClient", return_value=client):
        out = await ne.extract_article_facts(ARTICLE, "MSFT")
    assert out is None, "transient failure must leave the raw path in charge"


@pytest.mark.asyncio
async def test_empty_facts_is_a_valid_result_not_a_failure():
    """[] means 'article has no substance' and must be cached as such —
    None means 'try again later'. Conflating them re-extracts junk forever."""
    client = _FakeClient([_chat_response('{"facts": []}')])
    with _patch_targets(), patch("httpx.AsyncClient", return_value=client):
        out = await ne.extract_article_facts(ARTICLE, "MSFT")
    assert out == []


# ── rendering ────────────────────────────────────────────────────────────────

def test_render_facts_line_is_compact_and_directional():
    line = ne.render_facts_line([
        {"class": "earnings", "statement": "Beat", "quote": "beat consensus",
         "direction": "bullish"},
        {"class": "guidance", "statement": "Soft Q3 guide", "quote": "guided lower",
         "direction": "bearish"},
    ])
    assert "[earnings↑]" in line
    assert "[guidance↓]" in line
    assert len(line) < 400
