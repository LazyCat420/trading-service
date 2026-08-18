"""
Vector Store — embedding storage and similarity search.

MongoDB only: vectors are packed float32 BinData, cosine is computed app-side
in numpy, and keyword search uses a Mongo $text index. The pgvector backend
and the dual-write soak path were removed once `embeddings` was the one table
running on Mongo alone — their branches were already unreachable, because the
backend predicates had been hardcoded to mongo-only.

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

from app.db import mongo_store
from app.db import mongo_query

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
    """Vector storage and similarity search, on MongoDB."""

    # ─── Backend plumbing ───────────────────────────────────────────────

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

        try:
            self._mongo_store(source_table, source_id, ticker, content_preview, embedding, eid, now)
        except Exception as e:
            logger.error("[vector_store] mongo store failed for %s/%s: %s",
                         source_table, source_id, e)
            return ""
        return eid


    def _mongo_store(self, source_table, source_id, ticker, content_preview,
                     embedding, eid, now) -> None:
        from app.db import mongo_store

        mongo_store.delete_docs("embeddings", {"source_table": source_table, "source_id": source_id})
        mongo_store.update_docs(
            "embeddings",
            {"id": eid},
            {"$set": {
                "id": eid,
                "source_table": source_table,
                "source_id": source_id,
                "ticker": ticker,
                "content_preview": content_preview[:500],
                "embedding": _pack_vec(embedding),
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
        try:
            count = self._mongo_store_batch(recs, now)
        except Exception as e:
            logger.error("[vector_store] mongo store_batch failed: %s", e)
            return 0
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
        return self._mongo_coll().count_documents(
            {"source_table": source_table, "source_id": source_id}, limit=1
        ) > 0

    def existing_source_ids(self, source_table: str, source_ids: list[str]) -> set[str]:
        """Subset of `source_ids` that already have an embedding. One round-trip;
        used by the ingest backfill's anti-join."""
        if not source_ids:
            return set()
        ids = [str(s) for s in source_ids]
        cur = self._mongo_coll().find(
            {"source_table": source_table, "source_id": {"$in": ids}},
            {"source_id": 1, "_id": 0},
        )
        return {d["source_id"] for d in cur}

    # ─── Search: Cosine Similarity ─────────────────────────────────────

    def search_cosine(
        self,
        query_embedding: list[float],
        ticker: str | None = None,
        top_k: int = 10,
        source_filter: str | None = None,
    ) -> list[dict]:
        """Search embeddings by cosine similarity.

        Fetches the filtered candidate set and computes cosine app-side in
        numpy — exact, not approximate. The corpus is small enough that a
        brute-force pass beats maintaining an ANN index.

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
        return self._mongo_search_cosine(query_embedding, ticker, top_k, source_filter)


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
            from app.db import mongo_store

            q: dict = {}
            if ticker:
                q["$or"] = [{"ticker": ticker}, {"ticker": None}]
            if source_filter:
                q["source_table"] = source_filter

            docs = mongo_store.find_docs(
                "embeddings",
                q,
                projection={"_id": 0, "id": 1, "source_table": 1, "source_id": 1,
                            "ticker": 1, "content_preview": 1, "embedding": 1},
                sort=[("created_at", -1)],
                limit=_MAX_CANDIDATES,
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
        """Alias of search_cosine — the search is exact brute-force, so there
        is no separate indexed path to choose."""
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
        return self._mongo_search_text(query_text, ticker, top_k)


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
        total = mongo_query.agg_row('embeddings', {}, [('count', None)])[0]

        by_source = mongo_query.group_rows('embeddings', {}, ['source_table'], [('count', None)], [('key', 'source_table'), ('agg', 0)], sort=[('a0', -1)])

        by_ticker = mongo_query.group_rows('embeddings', {}, ['ticker'], [('count', None)], [('key', 'ticker'), ('agg', 0)], sort=[('a0', -1)], limit=20)

        return {
            "total_embeddings": total,
            "by_source": {r[0]: r[1] for r in by_source},
            "by_ticker": {r[0]: r[1] for r in by_ticker},
            # Reported as False rather than removed: consumers read these
            # keys. They were hardcoded True citing pgvector's HNSW index and
            # Postgres full-text search, neither of which exists now — cosine
            # is an exact numpy pass and keyword search is a Mongo $text
            # index, so a caller choosing a strategy on "hnsw_available" was
            # being told about a capability that had been deleted.
            "hnsw_available": False,
            "fts_available": True,  # Mongo $text index on content_preview
        }

    def clear(self):
        """Delete all embeddings. Use for testing."""
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
