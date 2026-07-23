"""
Vector Store — embedding storage and similarity search.

Two backends, selected per the Postgres→MongoDB consolidation flag
(MONGO_STORE_BACKEND, table name "embeddings" — see app/db/mongo_store.py):

  pg    → PostgreSQL/pgvector: cosine via <=> + HNSW index (legacy default)
  dual  → write both stores, read Postgres (soak phase)
  mongo → MongoDB only: vectors as packed float32 BinData, cosine computed
          app-side in numpy, keyword search via a Mongo $text index.

The corpus is small (~28k × 384-dim as of 2026-07-22) and every search is
per-ticker-per-cycle with top_k ≤ 50, so brute-force numpy cosine over the
filtered candidate set is single-digit milliseconds — no ANN index needed.

Usage:
    from app.db.vector_store import vector_store
    vector_store.store_embedding("news_articles", "abc123", "NVDA", "...", vec)
    results = vector_store.search_cosine(query_vec, ticker="NVDA", top_k=10)
"""

import logging
import struct
import uuid
from datetime import datetime, UTC

from app.db.connection import get_db

logger = logging.getLogger(__name__)

_TABLE = "embeddings"  # flag key in MONGO_STORE_BACKEND and Mongo collection name

# Hard cap on candidate docs fetched for one app-side cosine pass. The live
# corpus is ~28k total and every caller filters by ticker or source_table
# (worst measured candidate set ≈ 4.6k); this only guards a future unbounded
# call from slurping the whole collection forever. Most-recent docs win.
_MAX_CANDIDATES = 30_000


def _pack_vec(embedding: list[float]) -> bytes:
    """float list → packed little-endian float32 bytes (4 bytes/dim, ~2x
    smaller than a BSON double array at 384 dims)."""
    return struct.pack(f"<{len(embedding)}f", *embedding)


def _unpack_matrix(docs: list[dict]):
    """Stack docs' packed vectors into an (n, dim) float32 numpy matrix.
    Docs whose payload is missing/odd-length are dropped (returned mask)."""
    import numpy as np

    vecs, kept = [], []
    for d in docs:
        raw = d.get("embedding")
        if isinstance(raw, (bytes, bytearray)) and len(raw) % 4 == 0 and len(raw) > 0:
            vecs.append(np.frombuffer(bytes(raw), dtype="<f4"))
            kept.append(d)
    if not vecs:
        return None, []
    dim = len(vecs[0])
    same = [(v, d) for v, d in zip(vecs, kept) if len(v) == dim]
    if not same:
        return None, []
    return np.vstack([v for v, _ in same]), [d for _, d in same]


class VectorStore:
    """Flag-dispatched vector storage and similarity search (pgvector | Mongo)."""

    # ─── Backend plumbing ───────────────────────────────────────────────

    @staticmethod
    def _writes_mongo() -> bool:
        from app.db.mongo_store import writes_mongo

        return writes_mongo(_TABLE)

    @staticmethod
    def _reads_mongo() -> bool:
        from app.db.mongo_store import reads_mongo

        return reads_mongo(_TABLE)

    @staticmethod
    def _writes_pg() -> bool:
        from app.db.mongo_store import backend_for

        return backend_for(_TABLE) in ("pg", "dual")

    _mongo_indexes_ready = False

    @classmethod
    def _mongo_coll(cls):
        from app.db.mongo_store import get_doc_db

        coll = get_doc_db()[_TABLE]
        if not cls._mongo_indexes_ready:
            try:
                import pymongo

                coll.create_index("id", unique=True)
                coll.create_index([("source_table", pymongo.ASCENDING),
                                   ("source_id", pymongo.ASCENDING)])
                coll.create_index([("ticker", pymongo.ASCENDING),
                                   ("created_at", pymongo.DESCENDING)])
                # One $text index per collection — the BM25 replacement.
                coll.create_index([("content_preview", pymongo.TEXT)])
                cls._mongo_indexes_ready = True
            except Exception as e:
                logger.error("[vector_store] mongo index ensure failed (non-fatal): %s", e)
        return coll

    # ─── Storage ────────────────────────────────────────────────────────

    def store_embedding(
        self,
        source_table: str,
        source_id: str,
        ticker: str | None,
        content_preview: str,
        embedding: list[float],
        embedding_id: str | None = None,
    ) -> str:
        """Store a single embedding (one per source row — priors are cleared).

        Returns the embedding ID, or "" when the vector was rejected.
        """
        # Reject degenerate vectors: the embedding service returns an
        # all-zero vector when every backend fails, and a stored zero vector
        # silently poisons cosine search (rows look present, recall is noise).
        if not embedding or not any(embedding):
            logger.warning(
                "[vector_store] %s/%s: refusing to store zero/empty embedding",
                source_table, source_id,
            )
            return ""
        eid = embedding_id or str(uuid.uuid4())
        now = datetime.now(UTC)

        if self._writes_pg():
            self._pg_store(source_table, source_id, ticker, content_preview, embedding, eid, now)
        if self._writes_mongo():
            try:
                self._mongo_store(source_table, source_id, ticker, content_preview, embedding, eid, now)
            except Exception as e:
                if not self._writes_pg():
                    logger.error("[vector_store] mongo store failed for %s/%s: %s",
                                 source_table, source_id, e)
                    return ""
                # dual mode: Mongo is best-effort, never break the PG path.
                logger.warning("[vector_store] mongo mirror failed (non-fatal): %s", e)
        return eid

    def _pg_store(self, source_table, source_id, ticker, content_preview,
                  embedding, eid, now) -> None:
        with get_db() as db:
            # One embedding per source row: the conflict key below is a fresh
            # random UUID, so re-embedding an updated memory used to APPEND a
            # new row and leave the stale vector in search. Clear priors first.
            db.execute(
                "DELETE FROM embeddings WHERE source_table = %s AND source_id = %s",
                [source_table, source_id],
            )
            db.execute(
                """
                INSERT INTO embeddings
                (id, source_table, source_id, ticker,
                 content_preview, embedding, created_at)
                VALUES (%s, %s, %s, %s, %s, %s::vector, %s)
                ON CONFLICT (id) DO UPDATE SET
                    source_table = EXCLUDED.source_table,
                    source_id = EXCLUDED.source_id,
                    ticker = EXCLUDED.ticker,
                    content_preview = EXCLUDED.content_preview,
                    embedding = EXCLUDED.embedding,
                    created_at = EXCLUDED.created_at
            """,
                [eid, source_table, source_id, ticker,
                 content_preview[:500], embedding, now.isoformat()],
            )

    def _mongo_store(self, source_table, source_id, ticker, content_preview,
                     embedding, eid, now) -> None:
        from bson import Binary

        coll = self._mongo_coll()
        # Same one-per-source semantics as the PG path.
        coll.delete_many({"source_table": source_table, "source_id": source_id})
        coll.update_one(
            {"id": eid},
            {"$set": {
                "id": eid,
                "source_table": source_table,
                "source_id": source_id,
                "ticker": ticker,
                "content_preview": content_preview[:500],
                "embedding": Binary(_pack_vec(embedding)),
                "dim": len(embedding),
                "created_at": now,
            }},
            upsert=True,
        )

    def store_batch(
        self,
        records: list[dict],
    ) -> int:
        """Store a batch of embeddings (upsert by id).

        Each record should have: source_table, source_id, ticker,
        content_preview, embedding. Optional: id.

        Returns count of records stored.
        """
        if not records:
            return 0
        now = datetime.now(UTC)
        recs = [dict(r, id=r.get("id", str(uuid.uuid4()))) for r in records]

        count = 0
        if self._writes_pg():
            with get_db() as db:
                for rec in recs:
                    db.execute(
                        """
                        INSERT INTO embeddings
                        (id, source_table, source_id, ticker,
                         content_preview, embedding, created_at)
                        VALUES (%s, %s, %s, %s, %s, %s::vector, %s)
                        ON CONFLICT (id) DO UPDATE SET
                            source_table = EXCLUDED.source_table,
                            source_id = EXCLUDED.source_id,
                            ticker = EXCLUDED.ticker,
                            content_preview = EXCLUDED.content_preview,
                            embedding = EXCLUDED.embedding,
                            created_at = EXCLUDED.created_at
                    """,
                        [rec["id"], rec["source_table"], rec["source_id"],
                         rec.get("ticker"), rec.get("content_preview", "")[:500],
                         rec["embedding"], now.isoformat()],
                    )
                    count += 1
                logger.info(f"[DB] Stored {count} embeddings")
        if self._writes_mongo():
            try:
                count = self._mongo_store_batch(recs, now)
            except Exception as e:
                if not self._writes_pg():
                    logger.error("[vector_store] mongo store_batch failed: %s", e)
                    return 0
                logger.warning("[vector_store] mongo batch mirror failed (non-fatal): %s", e)
        return count

    def _mongo_store_batch(self, recs: list[dict], now) -> int:
        import pymongo
        from bson import Binary

        ops = [
            pymongo.UpdateOne(
                {"id": rec["id"]},
                {"$set": {
                    "id": rec["id"],
                    "source_table": rec["source_table"],
                    "source_id": rec["source_id"],
                    "ticker": rec.get("ticker"),
                    "content_preview": rec.get("content_preview", "")[:500],
                    "embedding": Binary(_pack_vec(rec["embedding"])),
                    "dim": len(rec["embedding"]),
                    "created_at": now,
                }},
                upsert=True,
            )
            for rec in recs
            if rec.get("embedding") and any(rec["embedding"])
        ]
        if ops:
            self._mongo_coll().bulk_write(ops, ordered=False)
        return len(ops)

    def exists(self, source_table: str, source_id: str) -> bool:
        """Check if an embedding already exists for this source."""
        if self._reads_mongo():
            return self._mongo_coll().count_documents(
                {"source_table": source_table, "source_id": source_id}, limit=1
            ) > 0
        with get_db() as db:
            result = db.execute(
                """
                SELECT 1 FROM embeddings
                WHERE source_table = %s AND source_id = %s
                LIMIT 1
            """,
                [source_table, source_id],
            ).fetchone()
            return result is not None

    def existing_source_ids(self, source_table: str, source_ids: list[str]) -> set[str]:
        """Subset of `source_ids` that already have an embedding. One round-trip;
        used by the ingest backfill's anti-join."""
        if not source_ids:
            return set()
        ids = [str(s) for s in source_ids]
        if self._reads_mongo():
            cur = self._mongo_coll().find(
                {"source_table": source_table, "source_id": {"$in": ids}},
                {"source_id": 1, "_id": 0},
            )
            return {d["source_id"] for d in cur}
        with get_db() as db:
            rows = db.execute(
                "SELECT source_id FROM embeddings"
                " WHERE source_table = %s AND source_id = ANY(%s)",
                [source_table, ids],
            ).fetchall()
            return {r[0] for r in rows}

    # ─── Search: Cosine Similarity ─────────────────────────────────────

    def search_cosine(
        self,
        query_embedding: list[float],
        ticker: str | None = None,
        top_k: int = 10,
        source_filter: str | None = None,
    ) -> list[dict]:
        """Search embeddings by cosine similarity.

        pg backend: pgvector's <=> operator with HNSW index.
        mongo backend: fetch the filtered candidate set, numpy cosine app-side.

        Args:
            query_embedding: 384-dim query vector.
            ticker: Optional ticker filter. If provided, returns chunks
                    where ticker matches OR ticker is NULL (macro context).
            top_k: Number of results to return.
            source_filter: Optional source_table filter (e.g., 'news_articles').

        Returns:
            List of dicts with: id, source_table, source_id, ticker,
            content_preview, score (cosine similarity 0-1).
        """
        if self._reads_mongo():
            return self._mongo_search_cosine(query_embedding, ticker, top_k, source_filter)

        with get_db() as db:
            # Build WHERE clause
            conditions = []
            params = []

            if ticker:
                conditions.append("(ticker = %s OR ticker IS NULL)")
                params.append(ticker)

            if source_filter:
                conditions.append("source_table = %s")
                params.append(source_filter)

            where_clause = "WHERE " + " AND ".join(conditions) if conditions else ""

            query = f"""
                SELECT id, source_table, source_id, ticker, content_preview,
                       1 - (embedding <=> %s::vector) as score
                FROM embeddings
                {where_clause}
                ORDER BY embedding <=> %s::vector
                LIMIT %s
            """

            # The order of params must match the query:
            # 1st %s is query_embedding in the SELECT clause
            # Next are the conditions for the WHERE clause
            # Next is query_embedding in the ORDER BY clause
            # Last is top_k in the LIMIT clause
            final_params = [query_embedding] + params + [query_embedding, top_k]

            rows = db.execute(query, final_params).fetchall()

            return [
                {
                    "id": r[0],
                    "source_table": r[1],
                    "source_id": r[2],
                    "ticker": r[3],
                    "content_preview": r[4],
                    "score": r[5],
                }
                for r in rows
            ]

    def _mongo_search_cosine(
        self,
        query_embedding: list[float],
        ticker: str | None,
        top_k: int,
        source_filter: str | None,
    ) -> list[dict]:
        import numpy as np
        import pymongo

        try:
            q: dict = {}
            if ticker:
                q["$or"] = [{"ticker": ticker}, {"ticker": None}]
            if source_filter:
                q["source_table"] = source_filter

            docs = list(
                self._mongo_coll()
                .find(q, {"_id": 0, "id": 1, "source_table": 1, "source_id": 1,
                          "ticker": 1, "content_preview": 1, "embedding": 1})
                .sort("created_at", pymongo.DESCENDING)
                .limit(_MAX_CANDIDATES)
            )
            matrix, kept = _unpack_matrix(docs)
            if matrix is None:
                return []

            qv = np.asarray(query_embedding, dtype="<f4")
            if qv.shape[0] != matrix.shape[1]:
                logger.warning(
                    "[vector_store] query dim %d != corpus dim %d — no results",
                    qv.shape[0], matrix.shape[1],
                )
                return []
            qn = np.linalg.norm(qv)
            norms = np.linalg.norm(matrix, axis=1)
            if qn == 0:
                return []
            norms[norms == 0] = 1e-12
            sims = (matrix @ qv) / (norms * qn)

            k = min(top_k, len(kept))
            idx = np.argpartition(-sims, k - 1)[:k]
            idx = idx[np.argsort(-sims[idx])]
            return [
                {
                    "id": kept[i]["id"],
                    "source_table": kept[i].get("source_table"),
                    "source_id": kept[i].get("source_id"),
                    "ticker": kept[i].get("ticker"),
                    "content_preview": kept[i].get("content_preview"),
                    "score": float(sims[i]),
                }
                for i in idx
            ]
        except Exception as e:
            logger.warning("[vector_store] mongo cosine search failed: %s", e)
            return []

    # ─── Search: HNSW ANN (pg alias — kept for API compat) ─────────────

    def search_hnsw(
        self,
        query_embedding: list[float],
        ticker: str | None = None,
        top_k: int = 10,
    ) -> list[dict]:
        """Alias of search_cosine (pgvector planner picks the HNSW index
        automatically; the mongo path is exact brute-force anyway)."""
        return self.search_cosine(query_embedding, ticker, top_k)

    # ─── Full-Text Search ──────────────────────────────────────────────

    def search_bm25(
        self,
        query_text: str,
        ticker: str | None = None,
        top_k: int = 30,
    ) -> list[dict]:
        """Ranked keyword search.

        pg backend: PostgreSQL to_tsvector/plainto_tsquery.
        mongo backend: $text index on content_preview (textScore ranking).
        Scores are NOT comparable across backends — the hybrid retriever
        fuses by rank (RRF), so only ordering matters.
        """
        if self._reads_mongo():
            return self._mongo_search_text(query_text, ticker, top_k)

        with get_db() as db:
            try:
                # Build WHERE clause for ticker filtering
                ticker_filter = ""
                from typing import Any

                params: list[Any] = [query_text, query_text]
                if ticker:
                    ticker_filter = "AND (ticker = %s OR ticker IS NULL)"
                    params = [query_text, query_text, ticker]

                query = f"""
                    SELECT id, source_table, source_id, ticker,
                           content_preview,
                           ts_rank(
                               to_tsvector('english', COALESCE(content_preview, '')),
                               plainto_tsquery('english', %s)
                           ) AS score
                    FROM embeddings
                    WHERE to_tsvector('english', COALESCE(content_preview, ''))
                          @@ plainto_tsquery('english', %s)
                    {ticker_filter}
                    ORDER BY score DESC
                    LIMIT %s
                """
                params.append(top_k)
                rows = db.execute(query, params).fetchall()

                return [
                    {
                        "id": r[0],
                        "source_table": r[1],
                        "source_id": r[2],
                        "ticker": r[3],
                        "content_preview": r[4],
                        "score": r[5],
                    }
                    for r in rows
                ]
            except Exception as e:
                logger.warning(f"[DB] Full-text search failed: {e}")
                return []

    def _mongo_search_text(
        self,
        query_text: str,
        ticker: str | None,
        top_k: int,
    ) -> list[dict]:
        try:
            q: dict = {"$text": {"$search": query_text}}
            if ticker:
                q["$or"] = [{"ticker": ticker}, {"ticker": None}]
            cur = (
                self._mongo_coll()
                .find(q, {"_id": 0, "id": 1, "source_table": 1, "source_id": 1,
                          "ticker": 1, "content_preview": 1,
                          "score": {"$meta": "textScore"}})
                .sort([("score", {"$meta": "textScore"})])
                .limit(top_k)
            )
            return [
                {
                    "id": d.get("id"),
                    "source_table": d.get("source_table"),
                    "source_id": d.get("source_id"),
                    "ticker": d.get("ticker"),
                    "content_preview": d.get("content_preview"),
                    "score": float(d.get("score", 0.0)),
                }
                for d in cur
            ]
        except Exception as e:
            logger.warning("[vector_store] mongo text search failed: %s", e)
            return []

    # ─── Stats ────────────────────────────────────────────────────────

    def get_stats(self) -> dict:
        """Return embedding statistics."""
        if self._reads_mongo():
            try:
                coll = self._mongo_coll()
                by_source = {
                    d["_id"]: d["cnt"]
                    for d in coll.aggregate([
                        {"$group": {"_id": "$source_table", "cnt": {"$sum": 1}}},
                        {"$sort": {"cnt": -1}},
                    ])
                }
                by_ticker = {
                    d["_id"]: d["cnt"]
                    for d in coll.aggregate([
                        {"$group": {"_id": "$ticker", "cnt": {"$sum": 1}}},
                        {"$sort": {"cnt": -1}},
                        {"$limit": 20},
                    ])
                }
                return {
                    "total_embeddings": coll.estimated_document_count(),
                    "by_source": by_source,
                    "by_ticker": by_ticker,
                    "hnsw_available": False,  # exact brute-force, no ANN
                    "fts_available": True,  # $text index
                }
            except Exception as e:
                logger.warning("[vector_store] mongo stats failed: %s", e)
                return {"total_embeddings": 0, "by_source": {}, "by_ticker": {},
                        "hnsw_available": False, "fts_available": False}
        with get_db() as db:
            total = db.execute("SELECT COUNT(*) FROM embeddings").fetchone()[0]

            by_source = db.execute("""
                SELECT source_table, COUNT(*) as cnt
                FROM embeddings
                GROUP BY source_table
                ORDER BY cnt DESC
            """).fetchall()

            by_ticker = db.execute("""
                SELECT ticker, COUNT(*) as cnt
                FROM embeddings
                GROUP BY ticker
                ORDER BY cnt DESC
                LIMIT 20
            """).fetchall()

            return {
                "total_embeddings": total,
                "by_source": {r[0]: r[1] for r in by_source},
                "by_ticker": {r[0]: r[1] for r in by_ticker},
                "hnsw_available": True,  # Always available with pgvector
                "fts_available": True,  # Always available with PostgreSQL
            }

    def clear(self):
        """Delete all embeddings. Use for testing."""
        if self._writes_pg():
            with get_db() as db:
                db.execute("DELETE FROM embeddings")
                logger.warning("[DB] All embeddings cleared")
        if self._writes_mongo():
            try:
                self._mongo_coll().delete_many({})
                logger.warning("[DB] All mongo embeddings cleared")
            except Exception as e:
                logger.warning("[vector_store] mongo clear failed: %s", e)


# Module-level singleton
vector_store = VectorStore()
"""
Global vector store instance. Import and use directly:

    from app.db.vector_store import vector_store
    results = vector_store.search_cosine(query_vec, ticker="AAPL", top_k=10)
"""
