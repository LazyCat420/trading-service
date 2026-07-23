# HANDOFF — Embeddings → MongoDB cutover + dual-write correctness wave (2026-07-23)

## What shipped (commit `60f6333`, deployed 2026-07-23T05:03Z)

**Embeddings now live in MongoDB.** The pgvector island is retired from the live
path; MongoDB is now the sole store for all trading-service/client logic except
the remaining PG tables still awaiting their own migration phases.

- `app/db/vector_store.py` — Mongo backend behind the `embeddings` key of
  `MONGO_STORE_BACKEND` (pg|dual|mongo, **live = mongo**). Vectors stored as
  packed little-endian float32 BinData (`dim` field alongside); cosine is
  app-side numpy over the filtered candidate set (verified 43 ms for a
  ticker-filtered top-12 over 27.3k docs); BM25 → Mongo `$text` index on
  `content_preview` (hybrid retriever fuses by rank, so the score-scale change
  is irrelevant).
- `app/cognition/lesson_store.py` — evolution-lesson embed/search rerouted
  through `vector_store` (was raw pgvector SQL).
- `app/services/embedding_ingest.py` — boot backfill anti-join is
  backend-aware: candidates from PG source tables, already-embedded filter via
  one Mongo `$in` (`vector_store.existing_source_ids`).
- `scripts/pg_embeddings_to_mongo.py` — standalone backfill/verify/dedupe
  (psycopg2+pymongo+numpy only; runnable from dev box).

**Migration run:** 28,000 PG rows → **27,306 Mongo docs**; the 694 skipped are
all-zero (failed-embed) vectors that poison cosine recall — intentionally
dropped. Sample cosine parity 1.000000. Live verification: `backend: mongo`,
self-hit rank-1 score 1.0, `$text` 5 hits, boot clean.

**Rollback:** PG `embeddings` table is UNTOUCHED (frozen at cutover) + a
54 MB pg_dump at `nas:/volume1/docker/backups/pre-migration/embeddings_pre_mongo_2026-07-23.dump`.
Rollback = remove `embeddings:mongo` from the flag (falls back to `pg`).
Note new live writes go Mongo-only, so PG staleness grows from 2026-07-23.

## Dual-write mirror fixes (from the 07-23 parity audit)

- `mongo_store.ensure_indexes()` now builds natural-key **unique** indexes
  (partial, `$type`-guarded) on all 12 dual collections + read-path compound
  indexes. All verified built on live.
- Deduped: execution_errors (−1,598), cycle_audit_log (−1,944),
  agent_audit_log (−12 by request_id).
- context_blobs backfill re-run to completion: 55,943 == 55,943.
- Live writer fixes: pipeline_events mirror converts ISO-string timestamps →
  BSON dates; battle_royale summary mirror stores `result_summary` as object;
  tool/v3 telemetry + audit mirrors now carry `created_at` (and ids where PG's
  serial was invisible); agent_audit_log upserts by `request_id`.
- Every mirror's `except: pass` replaced with logged warnings (a 12-minute
  silent Mongo write outage on 07-23 01:25Z had left zero trace).

## ⚠ Flag management trap (bit us this deploy)

`deploy-kit/.env.deploy` (gitignored, local) exports `MONGO_STORE_BACKEND` and
**wins over deploy.sh's default** because PRE_BUILD sources it with `set -a`.
Both places now hold the full 13-entry list (12×dual + embeddings:mongo) and
must be kept in sync. deploy.sh's default alone is NOT authoritative.

## Not done / next

- Read-flips for the 12 dual tables (pipeline_events, trade_results,
  ticker_reports, analysis_results, llm_audit_logs are parity-clean now;
  agent_traces/telemetry tables need a soak with the fixed mirrors first).
- lazy-agent-service `python/` mirror synced at `fe7cf8b` (source-only).
- The 694 zero-vector source rows will be re-embedded by the boot backfill as
  they surface in the recency window; failures skip harmlessly.
- pgvector schema objects (embeddings table, 2 duplicate HNSW indexes,
  dead `ontology_nodes.embedding`/`user_data` vector columns) can be dropped
  after a few clean cycles — keep until soak passes.
