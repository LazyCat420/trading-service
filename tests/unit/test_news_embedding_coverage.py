"""A long article is embedded from its first 3,686 chars and the rest is dropped.

`embedding_ingest.index_text` calls `embedder.embed_text(text)` — one vector per
article, no chunking — and `EmbeddingService.embed_batch` silently slices
anything over `EMBED_CHAR_BUDGET` (3,686). `EmbeddingService.chunk_text` exists
and is never called on this path.

Measured on the live DB, 2026-08-10 (articles collected in the last 3 days):

    articles with a body                       8,952
    body longer than the 3,686-char budget     2,915  (32.6%)
    body longer than 4,900                     2,078
    median / p90 / max body           2,599 / 7,501 / 20,300 chars
    share of ALL body characters discarded     31.6%

Pre-wave baseline (articles collected 10-20 days ago): 1,920 of 17,431 (11.0%)
over budget, 25.8% of characters discarded, mean body 1,509 chars. The
2026-08-09 body-upgrade wave roughly tripled the fraction of articles that
overflow.

The clamp is also far tighter than this corpus needs. Measured the same day by
asking the live embedder (`embeddinggemma`, `max_model_len` 2048, at
http://10.0.0.30:8001) for `usage.prompt_tokens` on eight real article bodies:

    1,800-char slice   ->   333-432 tokens    4.17 - 5.41 chars/token (mean 4.81)
    binary search, densest article   8,362 chars accepted  (4.08 chars/token)
    binary search, next densest      9,661 chars accepted  (4.72 chars/token)
    8,000-char slice   -> 1,596 tokens accepted; 11,000 chars REJECTED

So 3,686 chars of news prose is ~733 tokens — **36% of a 2,048-token window**.
The 1.8 chars/token in `embedding_service.py` was measured on the desk's dense
JSON prompts, which is a different corpus; news bodies run 2.3x looser. Both
numbers are right for their own corpus, which is exactly why one global
constant cannot serve both.

What this test pins is not the size of the budget — a per-corpus budget is a
design choice — but that the ingest path must not silently discard the tail of
an article it was asked to index.
"""
from __future__ import annotations

from unittest.mock import patch

# Measured live 2026-08-10 against embeddinggemma on real news bodies.
NEWS_CHARS_PER_TOKEN_MIN = 4.08     # densest of eight sampled articles
NEWS_CHARS_PER_TOKEN_MEAN = 4.81
EMBEDDER_WINDOW_TOKENS = 2048

# A real p90-ish article after the 2026-08-09 body-upgrade wave.
ARTICLE = "Sentence number %04d of the scraped article body. "
LONG_ARTICLE = "".join(ARTICLE % i for i in range(250))  # ~12,250 chars


def _capture_embedded_text():
    """Record every string that actually reaches the embedding HTTP layer."""
    from app.services import embedding_service

    sent: list[str] = []
    real_batch = embedding_service.EmbeddingService.embed_batch

    def _spy(self, texts, prefix="", batch_size=32, show_progress=True):
        # Re-run the real clamp so we observe what would go on the wire, then
        # short-circuit the network.
        budget = embedding_service.EMBED_CHAR_BUDGET
        for t in texts:
            sent.append(t[:budget] if len(t) > budget else t)
        return [[0.1] * embedding_service.EMBEDDING_DIM for _ in texts]

    return sent, patch.object(
        embedding_service.EmbeddingService, "embed_batch", _spy
    )


def test_indexing_a_long_article_does_not_drop_its_tail():
    """RED against current code: 12,250 chars in, 3,686 embedded, 70% gone."""
    from app.services import embedding_ingest

    sent, spy = _capture_embedded_text()
    stored: list = []

    class _FakeStore:
        def store_embedding(self, **kw):
            stored.append(kw)
            return "emb_x"

    with spy, patch("app.db.vector_store.vector_store", _FakeStore()):
        ok = embedding_ingest.index_text(
            "news_articles", "art-1", "NVDA", LONG_ARTICLE
        )

    assert ok
    covered = sum(len(t) for t in sent)
    assert covered >= len(LONG_ARTICLE), (
        f"{len(LONG_ARTICLE)} chars of article were handed to index_text but "
        f"only {covered} reached the embedder across {len(sent)} vector(s) — "
        f"{100 * (1 - covered / len(LONG_ARTICLE)):.0f}% of the article is not "
        "represented by any vector. `EmbeddingService.chunk_text` exists and is "
        "never called on this path."
    )


def test_the_budget_is_not_the_measured_ceiling_for_news_prose():
    """A control on the measurement above, so the next session does not
    re-derive it. This asserts the GAP is real; it deliberately does not demand
    that the global constant change, because 1.8 chars/token is the correct
    measurement for the desk's dense-JSON prompts and the same constant serves
    both corpora today."""
    from app.services.embedding_service import EMBED_CHAR_BUDGET

    news_ceiling = int(EMBEDDER_WINDOW_TOKENS * NEWS_CHARS_PER_TOKEN_MIN)
    assert news_ceiling == 8355, news_ceiling
    assert EMBED_CHAR_BUDGET < news_ceiling, (
        "re-measure: news prose no longer has more headroom than the budget"
    )
    # The budget uses barely a third of the window on this corpus.
    assert EMBED_CHAR_BUDGET / news_ceiling < 0.5, EMBED_CHAR_BUDGET
