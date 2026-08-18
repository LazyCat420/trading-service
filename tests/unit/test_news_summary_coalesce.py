"""`COALESCE(llm_summary, summary)` now picks a stale, shorter body.

`llm_summary` has had no writer since 8528bb0 ("rip out V2 python processors",
2026-06-19). Two audits recorded that as harmless because the column was NULL
for every row, which makes `COALESCE(llm_summary, summary)` identical to
`summary` — see the comment block in
`app/cognition/evidence/packet_builder.py`, which fixed its own copy on those
grounds, and `tests/unit/test_evidence_packet_columns.py`.

**That premise is false.** Measured on the live DB 2026-08-10:

    news_articles rows                              72,358
    llm_summary IS NOT NULL AND <> ''                  640   <- not zero
    published_at range of those 640         2026-06-09..06-20 (the writer's life)
    rows where llm_summary = summary                     0
    rows where llm_summary is SHORTER than summary     486
    avg length llm_summary / summary             844 / 2,762 chars

So on 640 rows the COALESCE does not resolve to `summary`; it resolves to a
different, 3.3x shorter, two-month-stale string. Those rows are reachable
because `get_finnhub_news` widens its window from 14 days to 90 when a ticker
has fewer than three recent articles — and 36 tickers with `llm_summary` rows
trip that widening right now, serving 56 rows through the stale column.

The damage is not in the rendered table (the raw body is capped at 400 chars
there anyway). It is upstream of it: the COALESCE'd string is what gets handed
to `ensure_facts`, the grounded-fact extractor. Feed it 703 chars of stale
summary instead of 2,783 chars of article and it grounds fewer facts — and
below `news_extraction._MIN_TEXT_CHARS` (400) it grounds none at all and the
article is skipped. Measured: 19 of those 56 rows have `llm_summary` under 400
chars while `summary` is over it, i.e. the COALESCE alone drops them out of
extraction.

These tests are offline; the numbers above are the live measurement that
motivates them.
"""
from __future__ import annotations

import ast
import pathlib
from unittest.mock import AsyncMock, patch

import pytest

REPO = pathlib.Path(__file__).resolve().parents[2]

# A row as it exists on the live DB: llm_summary written in June and never
# updated, `summary` carrying the article the scraper won in August.
STALE_LLM_SUMMARY = "Stale June one-liner about the company. " * 18   # 702 chars
FULL_ARTICLE = "The full scraped article body, sentence by sentence. " * 54  # 2,808


def _doc(*, llm_summary, summary):
    """One `news_articles` document as the live collection holds it.

    Both columns are present. Which one the tool reads is precisely what is
    under test, so the fake must NOT collapse them — the old version applied
    the COALESCE itself inside a fake `execute`, which measured the stub.
    """
    from datetime import UTC, datetime

    return {
        "id": "art-1",
        "ticker": "UFO",
        "title": "A headline",
        "publisher": "Reuters",
        "published_at": datetime(2026, 8, 10, tzinfo=UTC),
        "summary": summary,
        "llm_summary": llm_summary,
    }


# ── The defect, behaviourally ────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_the_grounded_extractor_is_fed_the_full_article_not_the_stale_summary():
    """RED against current code.

    `get_finnhub_news` selects `COALESCE(llm_summary, summary)` and passes that
    single string to `ensure_facts`. On a row whose `llm_summary` is the June
    leftover and whose `summary` is the real article, the extractor must
    receive the article.
    """
    from app.tools import finance_tools

    # What the DB actually holds for one of the 640 rows: both columns
    # populated, the June stub shorter than the August article.
    docs = [_doc(llm_summary=STALE_LLM_SUMMARY, summary=FULL_ARTICLE)]

    seen: dict = {}

    async def _fake_ensure_facts(rows, *a, **kw):
        seen["text"] = rows[0][3]
        return {}

    # `finance_tools` imports mongo_store INSIDE get_finnhub_news, so the
    # module attribute is not the one that resolves — patch the source.
    with patch("app.db.mongo_store.find_docs", return_value=docs), \
         patch("app.collectors.news_collector.collect_finnhub_news", AsyncMock()), \
         patch("app.services.news_extraction.ensure_facts", _fake_ensure_facts):
        await finance_tools.get_finnhub_news("UFO")

    assert seen.get("text") == FULL_ARTICLE, (
        "the grounded-fact extractor was handed "
        f"{len(seen.get('text') or '')} chars, not the {len(FULL_ARTICLE)}-char "
        "article — COALESCE(llm_summary, summary) picked the stale June column"
    )


@pytest.mark.asyncio
async def test_a_sub_400_char_stale_summary_does_not_hide_a_real_article():
    """The 19 measured rows: `llm_summary` under `_MIN_TEXT_CHARS`, `summary`
    well over it. Grounding is skipped entirely for text that short, so the
    COALESCE does not merely shorten the input — it removes the article from
    extraction."""
    from app.services import news_extraction
    from app.tools import finance_tools

    tiny = "June stub." * 12  # 120 chars, below _MIN_TEXT_CHARS
    assert len(tiny) < news_extraction._MIN_TEXT_CHARS < len(FULL_ARTICLE)

    docs = [_doc(llm_summary=tiny, summary=FULL_ARTICLE)]

    seen: dict = {}

    async def _fake_ensure_facts(rows, *a, **kw):
        seen["text"] = rows[0][3]
        return {}

    with patch("app.db.mongo_store.find_docs", return_value=docs), \
         patch("app.collectors.news_collector.collect_finnhub_news", AsyncMock()), \
         patch("app.services.news_extraction.ensure_facts", _fake_ensure_facts):
        await finance_tools.get_finnhub_news("UFO")

    assert len(seen.get("text") or "") >= news_extraction._MIN_TEXT_CHARS, (
        "the extractor was handed text below its own grounding floor while a "
        f"{len(FULL_ARTICLE)}-char article sat unread in `summary`"
    )


# ── The defect, as code shape ────────────────────────────────────────────────

def _sql_literals(path: pathlib.Path) -> list[str]:
    tree = ast.parse(path.read_text())
    return [
        n.value
        for n in ast.walk(tree)
        if isinstance(n, ast.Constant) and isinstance(n.value, str)
    ]


def test_the_agent_facing_news_query_does_not_read_llm_summary():
    """`llm_summary` has had no writer since 8528bb0. Reading it can only
    return something older than what `summary` holds."""
    src = REPO / "app" / "tools" / "finance_tools.py"
    offenders = [s for s in _sql_literals(src) if "llm_summary" in s]
    assert not offenders, (
        "app/tools/finance_tools.py still selects llm_summary — a column with "
        "no writer since 2026-06-19 and 640 stale rows that are NOT equal to "
        f"summary: {offenders}"
    )


def test_the_embedding_ingest_text_expression_does_not_read_llm_summary():
    """`_BACKFILL_SOURCES['news_articles']` embeds
    `COALESCE(NULLIF(llm_summary,''), NULLIF(summary,''), title)`. Same column,
    same staleness — and here it decides what the vector *is*."""
    from app.services.embedding_ingest import _BACKFILL_SOURCES

    text_expr = _BACKFILL_SOURCES["news_articles"][2]
    assert "llm_summary" not in text_expr, (
        "embedding_ingest embeds llm_summary in preference to summary "
        f"({text_expr!r}) — on the 640 rows that have it, the stored vector "
        "describes an 844-char June stub instead of the 2,762-char article"
    )
