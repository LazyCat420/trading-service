# PostgreSQL → MongoDB Migration Checklist

The standing tracker for the migration. Boxes are ticked only when the claim
behind them is verifiable — a command output, a report file, or a test.

**Scope, reconciled 2026-08-17 against the live database and
`app/db/migration_ledger.json`:** 161 `migrate` / 28 `archive-only` / 1 `absent`
(`rejected_symbols`). 4 tables are runtime-created and absent from `schema.sql`
(`agent_tasks`, `agent_registry`, `autofix_runs`, `box_benchmark_runs`) and are
carried by an explicit positive list. 26 tables have composite natural keys,
including `price_history`, `technicals` and `sec_13f_holdings`.

> **A table is converted only when its writers, readers, verification,
> promotion evidence and fallback behaviour have all passed.** "A Mongo
> collection exists" and "a backfill ran" are not conversion.

---

## 0. Rules and inventory

- [x] **Scope reconciled** across live DB inventory, ledger, backend map and
      collection map — `scripts/quality_census.py` does this reconciliation on
      every run and fails loudly when a gate names a table that is not there.
- [x] **Foreign-owned tables identified and protected.** `treesearch-service`
      stores 13 tables in this same `trading_bot` database (`observations`,
      `canonical_strains`, `genomic_samples`, the `glass_*` family, …). They are
      excluded from every purge, gate and migration step by an explicit
      allowlist in `scripts/quality_gates.py`, re-checked immediately before
      every statement, and their row counts are asserted unchanged after any
      destructive run.
- [x] **Runtime-created positive list preserved** — the 4 tables above keep
      their ledger rows even where the empty PG shell was dropped.
- [x] **`rejected_symbols` stays `absent`** and out of the tooling.
- [ ] Do not remove PostgreSQL or its data before every migrated table has a
      recorded promotion artifact and a shadow-validation period.
      *(Junk removal under §A below is not this — it is measured bad data,
      user-authorised, and preceded by a verified full dump.)*
- [ ] `mongo_backends.env` remains the authoritative logical-backend contract.
- [ ] `mongo_backends.env`, `collection_map.json` and the resolver stay
      byte-identical across both repos, checked in pre-deploy.
- [ ] `apply_renames` stays disabled until both services use the resolver and
      both containers can be stopped and redeployed together.
- [ ] Regenerate the ledger only from clean committed trees.

## A. Data quality purge — **DONE 2026-08-17**

Executed before any further conversion so that only good data is ever migrated.

- [x] Full `pg_dump -Fc` taken and **verified restorable**
      (`pg_restore --list` → 226 TABLE DATA entries, 1.16 GB, sha256 recorded).
- [x] `mongodump` of the existing `trading_bot` collections (372 MB, sha256 recorded).
- [x] Quality gates defined as data in `scripts/quality_gates.py`, with a
      `reason` and a measured `evidence` line each. Census and purge share them;
      neither restates a predicate.
- [x] Census run **before** the purge (`scripts/quality_census.py`).
- [x] Purge dry-run compared against the census, then executed.
- [x] Census run **after** the purge — every row gate reads **0**.
- [x] **214 → 157 tables**; **244,650 bad rows** deleted; DB **4.79 → 4.44 GB**.
- [x] All 57 dropped tables archived individually first, each with row count,
      sha256 and a restore command → `docs/PURGED_TABLES_MANIFEST_2026-08-17.json`.
- [x] treesearch-service row counts asserted **unchanged**.
- [x] Good scraped data asserted intact: `price_history` 15,755,628 (0 bad
      closes, 0 duplicates on its true key), `technicals` 1,369,231,
      `sec_13f_holdings` 174,119, `congress_trades` 30,483, `macro_indicators`
      46,164, `embeddings` 28,657.
- [x] Collection-defect checklist produced from live measurements →
      `docs/DATA_COLLECTION_IMPROVEMENT_CHECKLIST.md`.
- [ ] Work the collection-defect checklist (fixes the *collectors*, so we stop
      producing bad rows). Open — 15 defects listed, worst first.

## B. Migration platform

- [ ] Every migration target has exactly one `collection_map.json` entry.
- [ ] Collection mapping is injective.
- [ ] Resolve the `trade_results` classification conflict (ledger says
      money/Decimal128, collection map says decision-log float). Make the
      promotion tool consume the intended override instead of failing on two
      contradictory sources of truth.
- [ ] Mongo index declarations per access pattern (append-only log, mutable
      state, reference/config, queue, time series, ledger).
- [ ] **`price_history` gets a UNIQUE index on `(ticker, date, source)`** — its
      real natural key, verified to have zero duplicates. The 34,093
      "duplicate" `(ticker,date)` pairs are legitimate multi-source coverage,
      not corruption; readers need a documented source priority.
- [ ] Every Mongo access in both repos goes through the resolver, including
      `$lookup` names and raw `get_doc_db()[name]`.
- [ ] Standardise the shared write surface (`insert_docs`, `upsert_doc`,
      `bulk_upsert`, `update_docs`, `delete_docs`, `find_one_and_update`,
      transactions, Decimal128 helpers).
- [ ] Reconcile the `delete_docs` signature divergence between the repos.
- [ ] Bulk writes return the number actually persisted, not the number requested.
- [ ] Promotion/proof tools stay read-only without an explicit apply flag.
- [ ] Write guard stays armed at `mongo`; read guard soaked with
      `MONGO_GUARD_BLOCK_READS=1` before and after each promotion.
- [ ] Guard behaviour verified for all three of `pg` / `dual`+`mongo_read` / `mongo`.
- [ ] Mirror failures visible at WARNING/ERROR (a silent stream cannot support
      a "zero failures" conclusion).

## C. Known blockers to repair first

- [ ] Per-table identity contract replaces the "everything keys on `id`"
      assumption: natural key, composite columns, BSON representation,
      comparison rules, authoritative timestamp, orphan policy.
- [ ] Fix embeddings verification — join on true semantic identity, not the
      regenerated UUID; compare pgvector lists to BSON binary numerically with a
      documented tolerance; reclassify the historical "missing"/"drifted"
      counts as verifier artifacts.
- [ ] Composite-key pagination tested across multiple batches (26 tables).
- [ ] Repair tables with known missing Mongo rows: `execution_errors`,
      `cycle_audit_log`, `pipeline_events`, `agent_traces` — then find the
      shared writer path and prove it cannot under-write silently again.
- [ ] Repair `trade_results` field divergence (`internal_consensus_score`,
      `dynamic_trigger`, `policy_action`) and prove the live writer and the
      backfill mapper emit the same shape.
- [ ] Every mirrored timestamp comes from one authoritative value.
- [ ] No TTL index silently deletes data expected to have full-history parity.
- [ ] Per-table read/write site inventory for both repos, and a coupling
      classification for each table.

## D. Conversion tiers

Convert by data shape and failure semantics, never alphabetically.

- [x] **Tier A — archive-only, dead and empty tables.** 57 tables archived with
      manifest, checksum and restore procedure, then dropped. Zero-row tables
      were processed explicitly rather than left ambiguous. See §A.
- [ ] **Tier B — small service-only tables** (62 migrate-scope tables have no
      client coupling as of 2026-08-17). Prove the full ceremony on one first.
- [ ] **Tier C — mutable state and append-only logs.** Missing-Mongo must
      surface honestly, never as a fake default such as `status: idle`.
- [ ] **Tier D — client-coupled tables.** Test service-old/client-new,
      service-new/client-old, and both-new.
- [ ] **Tier E — queues.** Drain cutover, not dual-write.
- [ ] **Tier F — money, positions, orders, fills, P&L.** Decimal128, real
      replica-set transactions, reconciliation artifact, human sign-off.
- [ ] **Tier G — large time series.** `price_history` (15.75M rows) and
      `technicals` (1.37M) are **kept, not recomputed** (user decision: the data
      is expensive to re-collect). Rate-limit the backfill against the ~2 GB
      oplog and monitor lag.

## E. Evidence required per table

- [ ] Scope status correct; key contract documented; BSON mapping tested.
- [ ] Required indexes exist and are verified.
- [ ] Backfill completes with no silently skipped rows.
- [ ] Exhaustive verification: PG count, Mongo count, missing, orphan,
      field-level equivalence, representation-specific comparison.
- [ ] All live writers and readers Mongo-capable and idempotent.
- [ ] Cross-repo compatibility proven where applicable.
- [ ] Dual-write soak with no unresolved mirror failures, on a non-silent log.
- [ ] Read guard catches a deliberately injected stale-Postgres access.
- [ ] `prove_mongo` PASS artifact; `promote_table.py --dry-run` clean against
      both repos; real promotion recorded with operator, timestamp and soak.

## F. PostgreSQL retirement

- [ ] Audit all code, scripts, cron jobs, admin tools and deploy hooks for raw
      Postgres access. *(Baseline 2026-08-17: 1,278 `.execute()` call sites
      across 188 files; ~994 excluding `migrations.py`/`init_db.py`. Of those,
      101 `ON CONFLICT` upserts and ~800 single-table statements are mechanical;
      65 JOINs, 73 GROUP BYs and 23 CTEs need redesign.)*
- [ ] Remove dual-write logic only after that table's full-Mongo soak.
- [ ] Freeze Postgres writes for the final validation window.
- [ ] Run the full cycle, dashboards, agents, scheduled jobs and restart tests
      against Mongo-authoritative data.
- [ ] Keep a read-only Postgres snapshot and a documented restore procedure.
- [ ] Archive schema, ledger, parity reports, promotion artifacts, dispositions.
- [ ] Remove PostgreSQL from runtime only after explicit final sign-off.

## Immediate next

- [ ] Work the collection-defect checklist so the collectors stop producing bad rows.
- [ ] Fix `trade_results` classification + field divergence.
- [ ] Fix embeddings parity to use its true natural key.
- [ ] Run the complete ceremony on one small service-only table and use that
      artifact as the template for the Tier B wave.
