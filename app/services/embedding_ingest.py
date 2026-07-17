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
from typing import Callable

logger = logging.getLogger(__name__)


def _emb_id(source_table: str, source_id: str) -> str:
    """Deterministic embedding id from (source_table, source_id) so re-indexing
    the same source row upserts (ON CONFLICT id) instead of duplicating."""
    key = f"{source_table}:{source_id}"
    return "emb_" + hashlib.sha1(key.encode()).hexdigest()[:24]


def index_text(source_table: str, source_id: str, ticker, text: str) -> bool:
    """Embed `text` and upsert it into `embeddings`. Non-fatal.

    Returns True if a row was written, False on empty text or any failure.
    """
    try:
        if not text or not text.strip():
            return False
        from app.services.embedding_service import embedder
        from app.db.vector_store import vector_store

        emb = embedder.embed_text(text)
        vector_store.store_embedding(
            source_table=source_table,
            source_id=str(source_id),
            ticker=ticker,
            content_preview=text,
            embedding=emb,
            embedding_id=_emb_id(source_table, str(source_id)),
        )
        return True
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
        "COALESCE(NULLIF(llm_summary, ''), NULLIF(summary, ''), title)",
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

    from app.db.connection import get_db

    try:
        with get_db() as db:
            rows = db.execute(
                f"""
                SELECT src.{id_col}, src.{ticker_col}, {text_expr} AS content
                FROM {source_table} src
                WHERE {text_expr} IS NOT NULL
                  AND NOT EXISTS (
                        SELECT 1 FROM embeddings e
                        WHERE e.source_table = %s AND e.source_id = src.{id_col}
                  )
                ORDER BY src.{recency_col} DESC NULLS LAST
                LIMIT %s
                """,
                [source_table, limit],
            ).fetchall()
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
