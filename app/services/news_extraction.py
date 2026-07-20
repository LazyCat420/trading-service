"""
Grounded news-fact extraction — langextract's method, in-house.

Design borrowed from google/langextract (Apache-2.0): a few-shot task prompt,
structured extractions, and — the part that matters — **source grounding**:
every extracted fact must carry an exact quote from the article, which is then
aligned back to character offsets in the source text. Facts whose quote cannot
be aligned are dropped, which is the anti-hallucination filter. We implement
the method directly against our own vLLM hosts instead of taking the library
dependency: our unit of work is one short article per call (no chunking or
multi-pass machinery needed), and the library's provider registry would sit
between us and endpoints we already resolve elsewhere.

Why this exists: agents received raw scraped article text. Measured on the
live DB: 0 of 4,923 articles collected in the last 7 days had an llm_summary,
so the news block fell back to `summary` — raw scrape averaging 2,324 chars
per article, often leading with site navigation. Fifteen of those per ticker
is ~9k tokens of low-signal input per agent call. Grounded facts compress that
~5-8x AND make every claim verifiable by offset lookup.

Cost model: extraction is cached per article (content never changes after
scrape), so only first-seen articles pay a call. The per-cycle budget bounds
worst-case latency; whatever doesn't finish inside it is served raw this cycle
and extracted next time.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from typing import Any

from app.db.connection import get_db
from app.utils.text_utils import parse_json_response

logger = logging.getLogger(__name__)

# Master switch — fail-open design means flipping this off simply restores the
# raw-summary rendering everywhere.
ENABLED = os.getenv("NEWS_GROUNDED_EXTRACTION", "true").lower() in ("1", "true", "yes")

# Article text below this carries nothing worth grounding (title-only rows).
_MIN_TEXT_CHARS = 400
# Cap what we send the model; the lede carries the facts in news copy.
_MAX_TEXT_CHARS = 6000
# Per-call timeout and per-cycle wall budget for a batch of extractions.
_CALL_TIMEOUT_S = float(os.getenv("NEWS_EXTRACT_CALL_TIMEOUT_S", "25"))
_BATCH_BUDGET_S = float(os.getenv("NEWS_EXTRACT_BATCH_BUDGET_S", "22"))
_CONCURRENCY = 4

_FACT_CLASSES = (
    "earnings", "guidance", "analyst_action", "product", "legal_regulatory",
    "macro", "ownership", "price_action", "other",
)

_PROMPT_TEMPLATE = """Extract the market-relevant FACTS from this news article about {ticker}.

Rules:
- Each fact needs an EXACT quote copied verbatim from the article text that supports it.
- Only substantive facts relevant to {ticker} or clearly market-moving context. Skip navigation text, ads, and boilerplate.
- class must be one of: earnings, guidance, analyst_action, product, legal_regulatory, macro, ownership, price_action, other
- direction is the fact's implication for {ticker}: bullish | bearish | neutral
- Return 0 to 6 facts. If the article has no substantive facts, return {{"facts": []}}.
- Return ONLY the JSON object, no commentary.

Example article: "Acme Corp (ACME) reported Q3 revenue of $2.1B, up 12% year over year, beating consensus of $1.9B. CFO Jane Doe said the company now expects full-year margins near 21%. Shares rose 4% in after-hours trading."
Example output: {{"facts": [
 {{"class": "earnings", "statement": "Q3 revenue beat consensus ($2.1B vs $1.9B, +12% YoY)", "quote": "reported Q3 revenue of $2.1B, up 12% year over year, beating consensus of $1.9B", "direction": "bullish"}},
 {{"class": "guidance", "statement": "Full-year margin guidance ~21%", "quote": "now expects full-year margins near 21%", "direction": "bullish"}},
 {{"class": "price_action", "statement": "After-hours move +4%", "quote": "Shares rose 4% in after-hours trading", "direction": "bullish"}}]}}

Article title: {title}
Article text:
{text}
"""

# ── Quote → source alignment (the grounding step) ───────────────────────────

_NORM_RE = re.compile(r"[\s ]+")


def _normalize(s: str) -> tuple[str, list[int]]:
    """Lowercase, unify curly quotes/dashes, collapse whitespace.

    Returns the normalized string plus a map from each normalized index back
    to the original index, so a match found in normalized space can be
    reported as offsets into the ORIGINAL text.
    """
    trans = {"‘": "'", "’": "'", "“": '"', "”": '"',
             "–": "-", "—": "-"}
    out_chars: list[str] = []
    index_map: list[int] = []
    prev_space = False
    for i, ch in enumerate(s):
        ch = trans.get(ch, ch)
        if _NORM_RE.match(ch):
            if prev_space:
                continue
            ch = " "
            prev_space = True
        else:
            prev_space = False
        out_chars.append(ch.lower())
        index_map.append(i)
    return "".join(out_chars), index_map


def align_quote(text: str, quote: str) -> tuple[int, int] | None:
    """Locate `quote` in `text`; return (start, end) offsets or None.

    Exact match first; then a normalized match that survives the usual LLM
    transcription drift (curly quotes, dash variants, whitespace runs, case).
    None means the model asserted evidence the article doesn't contain — the
    fact carrying it gets dropped.
    """
    if not quote or not text:
        return None
    quote = quote.strip()
    if len(quote) < 12:  # too short to be meaningful evidence
        return None

    pos = text.find(quote)
    if pos >= 0:
        return pos, pos + len(quote)

    norm_text, index_map = _normalize(text)
    norm_quote, _ = _normalize(quote)
    norm_quote = norm_quote.strip()
    if not norm_quote:
        return None
    pos = norm_text.find(norm_quote)
    if pos < 0:
        return None
    start = index_map[pos]
    end_norm = pos + len(norm_quote) - 1
    end = index_map[end_norm] + 1 if end_norm < len(index_map) else len(text)
    return start, end


# ── Extraction call ─────────────────────────────────────────────────────────


async def _chat_targets() -> list[tuple[str, str, str]]:
    # Same (provider, model, base_url) resolution the vision engine uses —
    # one source of truth for which local hosts serve chat completions.
    from app.scraper.engines.vision_engine import _vision_targets

    return await _vision_targets()


async def extract_article_facts(
    text: str, ticker: str, title: str = ""
) -> list[dict[str, Any]] | None:
    """Extract grounded facts from one article. None = extraction failed
    (caller keeps the raw path); [] = article genuinely has no facts."""
    import httpx

    if not text or len(text) < _MIN_TEXT_CHARS:
        return None

    body_text = text[:_MAX_TEXT_CHARS]
    prompt = _PROMPT_TEMPLATE.format(ticker=ticker, title=title or "(untitled)",
                                     text=body_text)

    try:
        targets = await _chat_targets()
    except Exception as e:  # noqa: BLE001 — no hosts up: raw path
        logger.warning("[news-extract] no chat targets: %s", e)
        return None

    for provider, model, base_url in targets:
        payload = {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.1,
            "max_tokens": 2048,
        }
        try:
            async with httpx.AsyncClient(timeout=_CALL_TIMEOUT_S) as client:
                r = await client.post(f"{base_url}/v1/chat/completions", json=payload)
                r.raise_for_status()
                message = (r.json().get("choices") or [{}])[0].get("message") or {}
                raw = str(message.get("content") or "")
        except Exception as e:  # noqa: BLE001 — try the next host
            logger.info("[news-extract] %s/%s failed: %s", provider, model, e)
            continue

        parsed = parse_json_response(raw)
        if not isinstance(parsed, dict) or not isinstance(parsed.get("facts"), list):
            logger.info("[news-extract] unparseable output (%d chars) from %s",
                        len(raw), model)
            continue

        grounded: list[dict[str, Any]] = []
        dropped = 0
        for fact in parsed["facts"][:6]:
            if not isinstance(fact, dict):
                continue
            span = align_quote(body_text, str(fact.get("quote") or ""))
            if span is None:
                dropped += 1  # ungrounded assertion — the filter working
                continue
            cls = str(fact.get("class") or "other")
            grounded.append({
                "class": cls if cls in _FACT_CLASSES else "other",
                "statement": str(fact.get("statement") or "")[:300],
                "quote": body_text[span[0]:span[1]][:300],
                "direction": str(fact.get("direction") or "neutral"),
                "char_start": span[0],
                "char_end": span[1],
            })
        if dropped:
            logger.info("[news-extract] dropped %d ungrounded fact(s) for %s",
                        dropped, ticker)
        return grounded

    return None


# ── Batch + cache layer ─────────────────────────────────────────────────────


def _store_facts(article_id: str, facts: list[dict[str, Any]], model_note: str) -> None:
    try:
        with get_db() as db:
            db.execute(
                "UPDATE news_articles SET grounded_facts = %s::jsonb, "
                "facts_extracted_at = NOW() WHERE id = %s",
                [json.dumps({"v": 1, "facts": facts, "model": model_note}), article_id],
            )
    except Exception as e:  # noqa: BLE001
        logger.warning("[news-extract] store failed for %s: %s", article_id, e)


async def ensure_facts(
    rows: list[tuple[str, str, str, Any]],
    budget_s: float = _BATCH_BUDGET_S,
) -> dict[str, list[dict[str, Any]]]:
    """Ensure grounded facts exist for (id, ticker, title, summary) rows.

    Returns {article_id: facts} for every article that has facts (cached or
    freshly extracted). Bounded by `budget_s`: extraction that doesn't finish
    in time is cancelled — those articles are served raw this cycle and picked
    up on a later one (results land in the DB whenever their task completes).
    """
    if not ENABLED:
        return {}

    have: dict[str, list[dict[str, Any]]] = {}
    todo: list[tuple[str, str, str, str]] = []

    ids = [r[0] for r in rows]
    if not ids:
        return {}
    try:
        with get_db() as db:
            cached = db.execute(
                "SELECT id, grounded_facts FROM news_articles "
                "WHERE id = ANY(%s) AND facts_extracted_at IS NOT NULL",
                [ids],
            ).fetchall()
        cached_map = {c[0]: c[1] for c in cached}
    except Exception as e:  # noqa: BLE001
        logger.warning("[news-extract] cache lookup failed: %s", e)
        cached_map = {}

    for article_id, ticker, title, summary in rows:
        if article_id in cached_map:
            doc = cached_map[article_id]
            if isinstance(doc, str):
                try:
                    doc = json.loads(doc)
                except Exception:  # noqa: BLE001
                    doc = None
            if isinstance(doc, dict) and doc.get("facts"):
                have[article_id] = doc["facts"]
            continue  # cached-empty counts as done — don't re-extract junk
        text = summary or ""
        if len(text) >= _MIN_TEXT_CHARS:
            todo.append((article_id, ticker, title or "", text))

    if not todo:
        return have

    sem = asyncio.Semaphore(_CONCURRENCY)

    async def _one(article_id: str, ticker: str, title: str, text: str) -> None:
        async with sem:
            facts = await extract_article_facts(text, ticker, title)
            if facts is None:
                return  # transient failure: no store, retry next cycle
            _store_facts(article_id, facts, "vllm")
            if facts:
                have[article_id] = facts

    t0 = time.monotonic()
    tasks = [asyncio.create_task(_one(*item)) for item in todo]
    done, pending = await asyncio.wait(tasks, timeout=budget_s)
    for task in pending:
        task.cancel()
    logger.info(
        "[news-extract] batch: %d cached, %d extracted, %d deferred (%.1fs)",
        len(cached_map), len(done), len(pending), time.monotonic() - t0,
    )
    return have


def render_facts_line(facts: list[dict[str, Any]]) -> str:
    """Compact one-article rendering for the agent-facing news table."""
    parts = []
    for f in facts:
        arrow = {"bullish": "↑", "bearish": "↓"}.get(f.get("direction", ""), "·")
        quote = (f.get("quote") or "")[:110]
        parts.append(f"[{f.get('class', 'other')}{arrow}] {f.get('statement', '')}"
                     f" — \"{quote}\"")
    return " | ".join(parts)
