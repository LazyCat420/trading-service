"""Embedding ingestion — feed narrative corpus text into the pgvector
`embeddings` table so the dense/hybrid retrievers actually have data to search.

Before this, only `canonical_memories` and `evolution_lessons` were ever
embedded, so semantic search over news / analysis / graph-claims returned
nothing and `retrieval_hybrid` was effectively dead. This module provides:

  - index_text(...)   — embed one string, upsert idempotently.
  - backfill_source() — index recent rows of a source table that lack an
                        embedding (idempotent, safe to re-run).

All operations are best-effort / non-fatal: embedding must never break the
trading pipeline.
"""

import hashlib
import logging
import os
from typing import Callable

logger = logging.getLogger(__name__)


def _emb_id(source_table: str, source_id: str) -> str:
    """Deterministic embedding id from (source_table, source_id) so re-indexing
    the same source row upserts (ON CONFLICT id) instead of duplicating."""
    key = f"{source_table}:{source_id}"
    return "emb_" + hashlib.sha1(key.encode()).hexdigest()[:24]


# Chunk size for prose ingest, in TOKENS. Kept under the embedder's 2048 so
# that even the densest article (measured 4.08 chars/token) fits.
PROSE_CHUNK_TOKENS = int(os.getenv("NEWS_EMBED_CHUNK_TOKENS", "1800"))


def index_text(source_table: str, source_id: str, ticker, text: str) -> bool:
    """Embed `text` and upsert it into `embeddings`. Non-fatal.

    Returns True if a row was written, False on empty text or any failure.
    """
    try:
        if not text or not text.strip():
            return False
        from app.services.embedding_service import embedder
        from app.db.vector_store import vector_store

        # Prose is NOT 1.8 chars/token. Measured 2026-08-10 against the live
        # embedder on real article bodies: 4.08-5.41 (mean 4.81), because the
        # 1.8 figure was fitted on the desk's dense JSON prompts. One global
        # constant cannot serve both corpora, so the news path carries its own.
        #
        # Before this, index_text embedded the FIRST 3,686 chars and dropped the
        # rest: 32.6% of articles collected after the 2026-08-09 body-upgrade
        # wave exceed that, and 31.6% of all body characters never reached a
        # vector. `chunk_text` existed the whole time and was never called here.
        chunks = embedder.chunk_text(text, max_tokens=PROSE_CHUNK_TOKENS) or [text]
        if len(chunks) > 1:
            logger.info(
                "[embed-ingest] %s/%s: %d chars -> %d chunks (was 1 vector over "
                "the first %d chars)",
                source_table, source_id, len(text), len(chunks),
                getattr(embedder, "EMBED_CHAR_BUDGET", 3686),
            )
        embs = embedder.embed_batch(chunks, show_progress=False)
        wrote = False
        for i, (chunk, emb) in enumerate(zip(chunks, embs)):
            vector_store.store_embedding(
                source_table=source_table,
                source_id=str(source_id),
                ticker=ticker,
                content_preview=chunk,
                embedding=emb,
                # Chunk 0 keeps the original id so existing rows update in place
                # rather than orphaning; later chunks get their own.
                embedding_id=_emb_id(source_table, str(source_id))
                if i == 0 else _emb_id(source_table, f"{source_id}#{i}"),
            )
            wrote = True
        return wrote
    except Exception as e:
        logger.debug(
            "[embed-ingest] %s/%s failed (non-fatal): %s", source_table, source_id, e
        )
        return False


# source_table -> (id_col, ticker_col, text SQL expr, recency_col).
# text exprs reference only fixed column names (no user input) — safe to inline.
_BACKFILL_SOURCES: dict[str, tuple[str, str, str, str]] = {
    "news_articles": (
        "id",
        "ticker",
        # llm_summary has had no writer since 8528bb0; see finance_tools.
        "COALESCE(NULLIF(summary, ''), title)",
        "collected_at",
    ),
    "analysis_results": (
        "id",
        "ticker",
        "NULLIF(thesis_summary, '')",
        "created_at",
    ),
}


def backfill_source(
    source_table: str,
    limit: int = 300,
    should_stop: Callable[[], bool] | None = None,
) -> int:
    """Index up to `limit` most-recent rows of `source_table` that don't yet
    have an embedding. Idempotent. Returns the number indexed."""
    cfg = _BACKFILL_SOURCES.get(source_table)
    if not cfg:
        logger.warning("[embed-ingest] no backfill config for %s", source_table)
        return 0
    id_col, ticker_col, text_expr, recency_col = cfg

    from app.db import mongo_store
    from app.db.vector_store import vector_store

    try:
        docs = mongo_store.find_docs(
            source_table,
            {},
            sort=[(recency_col, -1)],
            limit=limit * 4,
        )
        candidates = []
        for d in docs:
            cid = str(d.get(id_col) or "")
            ticker = d.get(ticker_col)
            if source_table == "news_articles":
                content = d.get("summary") or d.get("title") or ""
            elif source_table == "analysis_results":
                content = d.get("thesis_summary") or ""
            else:
                content = d.get("content") or d.get("summary") or ""
            if cid and content:
                candidates.append((cid, ticker, content))

        done = vector_store.existing_source_ids(
            source_table, [c[0] for c in candidates]
        )
        rows = [c for c in candidates if c[0] not in done][:limit]
    except Exception as e:
        logger.warning("[embed-ingest] backfill query failed for %s: %s", source_table, e)
        return 0

    indexed = 0
    for r in rows:
        if should_stop and should_stop():
            break
        if index_text(source_table, r[0], r[1], r[2]):
            indexed += 1
    if indexed:
        logger.info("[embed-ingest] backfilled %d rows from %s", indexed, source_table)
    return indexed


def backfill_all(
    limit_per_source: int = 300,
    should_stop: Callable[[], bool] | None = None,
) -> dict[str, int]:
    """Backfill every configured source table. Returns {source_table: count}."""
    return {
        t: backfill_source(t, limit_per_source, should_stop)
        for t in _BACKFILL_SOURCES
    }
