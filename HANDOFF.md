# HANDOFF — Data-collection wave 2 (2026-07-23, later session): bridge timeout + collectors + news cap

Commits: trading-service `5365b36`..`cb8ef86`+1, lazy-agent-service `00d5c88`+3 mirror syncs. All deployed and live-verified.

## 1. :3031 tool-bridge per-tool timeouts (lazy-agent-service)
`config.ts` + `src/services/LocalToolRouter.ts`: slow external-fetch tools
(SLOW_TOOLS env list; lazy_web_search, scrape_url, read_url, get_sec_filings,
run_tool_chain, get_market_map_data, get_ticker_summary, get_finnhub_news) now
get `SLOW_TOOL_TIMEOUT_MS` (60s) instead of the global 30s that raced
lazy_web_search's internal 20s+10s retries — the #1 tool-failure cause (~40
aborts/7d). Fast tools keep 30s fail-fast. Abort errors now read
"bridge timeout after Nms". Watch agent_tool_telemetry for the abort rate drop.

## 2. Collectors wired + fixed (trading-service)
Scheduler jobs (cycle process): PCR daily 1:15 PM PT · openinsider cluster
buys daily 4:30 AM PT · economic calendar 12h · social sweep 6h. VERIFIED
live: put_call_ratio 2 rows, economic_calendar 70 events (ForexFactory JSON
primary — TE HTML is a fallback; scraper-service /scrape returns text-extracted
content so table parsers must fetch RAW html via httpx), insider_trades 100
real cluster buys (canned /latest-cluster-buys page; the old screener URL
returns blank placeholder rows; cluster rows carry insider COUNT + industry),
social_posts 282. `get_upcoming_events` now appends `macro_events` from
economic_calendar (was called 55×/7d against an empty table). Watchlist gotcha:
the column is `status` ('active'), NOT `is_active`.

## 3. News URL fan-out cap
`url_fanout_exceeded()` guards all 4 insert sites: max NEWS_URL_FANOUT_CAP (5)
rows per URL (one story had been stored 110×; 58.6% of the table was dup-URL
fan-out). Fails open. `idx_news_articles_url` created (live + migration).
Historical dup rows NOT deleted (backup exists:
`nas:/volume1/docker/backups/pre-migration/news_articles_pre_dedupe_2026-07-23.dump`).

---

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
