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
      destructive run. **Re-verified 2026-08-17 after the purge: all present,
      1,659 `observations` / 471 `canonical_strains` / 648 `genomic_samples`
      and the `glass_*` family intact.** Note the allowlist declares **14**
      names but only **13** exist — `glass_votes` has no table, so the
      allowlist protects a phantom (harmless, but it is why the handoff says
      13 and the code says 14; do not "reconcile" them by adding a table).
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
- [ ] **No partial deploy of `quality-purge`, ever.** Branch writes bypass the
      flag map by design (`insert_docs` has no `writes_mongo` gate while
      `mongo_backends.env` still lists 13 tables). The branch is safe only
      deployed nowhere or merged whole: the flag map / `pg_write_guard` /
      dual-write are removed in the merge change itself, and the merged result
      deploys to **both repos as one coordinated event**. (Filed 2026-08-17;
      trading-client ch.73.)

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
- [ ] ⚠ **THE TABLE DROP DID NOT HOLD — re-measured 2026-08-17 20:0x: 197 live
      public tables, not 157.** **40 of the 57 dropped tables are back**, every
      one of them with **0 rows**. Nothing re-inserted data; the DDL simply
      recreated the shells. `run_migrations()` is called at boot
      (`app/db/connection.py:496-500`) and `schema_pg.sql` is applied statement
      by statement (`:400`, `:426`), and both still declare these tables with
      `CREATE TABLE IF NOT EXISTS` (e.g. `migrations.py:973` for
      `agent_experiences`). The 17 that stayed dropped are the ones no DDL
      declares. So:
      - a `DROP TABLE` is a **data** operation here, not a retirement — it
        survives exactly until the next service boot;
      - the count in the line above is a measurement of one moment, and any
        later claim of "157 tables" is stale;
      - scope regrows silently: 40 empty tables are in the live schema again,
        so anything deriving migrate scope from the live DB will re-adopt them,
        which is the same manifest→ledger→spec chain that hid the four
        runtime-created tables (ch.72).
      **Retiring a table means deleting its DDL in the same change as the
      drop.** Until that lands, do not treat the purge as having reduced the
      migration's denominator.
- [x] treesearch-service row counts asserted **unchanged**.
- [x] Good scraped data asserted intact: `price_history` 15,755,628 (0 bad
      closes, 0 duplicates on its true key), `technicals` 1,369,231,
      `sec_13f_holdings` 174,119, `congress_trades` 30,483, `macro_indicators`
      46,164, `embeddings` 28,657.
- [x] Collection-defect checklist produced from live measurements →
      `docs/DATA_COLLECTION_IMPROVEMENT_CHECKLIST.md`.
- [ ] Work the collection-defect checklist (fixes the *collectors*, so we stop
      producing bad rows). Open — 15 defects listed, worst first.

## A2. Code conversion — **459 sites done, branch `quality-purge`**

- [x] **SQL inventory** (`scripts/sql_inventory.py`). 1,274 call sites: 305 in
      schema-building files (retired, not converted), 659 mechanical, 170
      needing redesign, 94 unknown (86 dynamic f-strings, 8 unparsed). The 94
      are reported separately and never folded into a convertible percentage.
- [x] **Translator** (`scripts/sql_to_mongo.py`). Refuses rather than
      approximates: LIKE, INTERVAL in months, OFFSET, LIMIT 0, `DELETE` with no
      WHERE, unaliased computed columns. Covers 462/659 mechanical (70%).
- [x] **Row-shape compatibility layer** (`app/db/mongo_query.py`). Returns
      TUPLES in the SQL's column order, so positional access (`r[0]`) at 459
      call sites keeps working. This is what made the rewrite mechanical.
- [x] **Differential verification** (`scripts/verify_translations.py`). Runs
      each translation against BOTH stores and compares rows. Caught three real
      bugs, incl. `IS NOT NULL` inverted (sqlglot's `Is(negate=True)`) and
      `LIMIT 0` returning the whole collection. ~~**94.2% of comparable
      statements match** after fixes.~~
- [x] ⚠ **THE CHECKER ITSELF WAS BROKEN, and the 94.2% above is stale**
      (found and fixed 2026-08-17, `a3d8263`). Two faults, the second hidden
      behind the first:
      1. `mongo_query` was missing from the eval namespace, so **every**
         translated SELECT raised `NameError` and scored ERROR. It was
         comparing nothing while still printing a percentage.
      2. With that fixed, the real one surfaced: `compare()` only understood
         **dicts**, but `find_rows`/`find_row`/`agg_row`/`group_rows`/
         `join_rows` return **tuples** in the SQL's column order — the shape
         compatibility three lines above, and the reason the codemod could
         rewrite 578 positional sites. `if c not in d` therefore tested a
         tuple's VALUES for a column NAME, and reported **38 of 43** comparable
         statements as "mongo doc missing field '&lt;first column&gt;'". Single-row
         helpers were compared unwrapped, measuring the COLUMN count against
         the Postgres ROW count.
      **Re-measured with the oracle working: 166 MATCH / 21 DIFFER / 24
      UNTESTED / 51 NOT_SEEDED / 6 ERROR — 88.8% of comparable.** Do not quote
      94.2% again; this code could not have produced it.
      The lesson is the checker's own: a tool whose whole job is to be believed
      about parity has to be tested itself. `tests/unit/test_sql_to_mongo_joins.py`
      now pins `compare()` in both directions.
- [ ] **Triage the 21 DIFFER and 6 ERROR in a quiet window.** Not yet
      attributed, and deliberately not called translation bugs: a seed sweep
      was running during the measurement, and several DIFFERs are plain
      row-count drift (`pg=0/mongo=1`, `pg=197/mongo=230`, `pg=13/mongo=23`) of
      the same kind as the `news_articles` orphan gap (mongo 89,916 vs pg
      89,896 — `migrate_all`'s per-table VERIFY prints OK when Mongo has
      MORE rows). **5 of the 6 ERRORs are the harness, not the translator**:
      `placeholder_columns()` truncates a column name (`reso`←`resolution`,
      `tick`←`ticker`, `usag`←`usage_*`, `attempt_n`←`attempt_*`,
      `promoted_to_mem`←`promoted_to_memory`), so the sample query hits a
      column that does not exist.
- [x] **Codemod applied** (`scripts/codemod_pg_to_mongo.py`): 459 sites across
      122 files, −3,711 lines of SQL. All compile. Smoke-tested 9/9 against
      live Mongo including a real converted call site.
- [x] **Backfill made non-quadratic.** Collections had only `_id`, so every
      upsert was a collection scan (~29 rows/sec, degrading). Indexing the
      natural key first: ~65 tables in 45 seconds.
- [ ] **~370 refused statements** — 39 GROUP BY, 34 JOIN, 25 aggregates, 19
      RETURNING, 14 DISTINCT, 11 LIKE. Python-side rewrites, one at a time.
- [x] **JOIN: two-table INNER on one equality now translates** to
      `mongo_query.join_rows()` (`a3d8263`). **The addressable set is 10, not
      37** — the rest carry a CTE, a subquery, grouping or a third table, and
      of the 10 several are LEFT joins. `LEFT`/`RIGHT`/`FULL` are refused by
      name: an inner stitch DROPS the rows they keep, and
      `LEFT JOIN … WHERE right.col IS NULL` is an **ANTI-join** whose inner
      translation returns the exact COMPLEMENT of the intended rows — the same
      shape as the `IS NOT NULL` inversion. Also refused: `ORDER BY` on the
      joined table (join_rows sorts the LEFT collection, so the sort would be
      silently dropped), unqualified columns, `SELECT *`, and a WHERE spanning
      both tables. Parameter numbering is the subtle part — placeholders are
      assigned when `next_param()` is CALLED, so the WHERE leaves are walked in
      SQL order and bucketed by side afterwards; building one side then the
      other re-binds every parameter that crosses the split, and that failure
      parses perfectly. Note the dispatch had to precede
      `_reject_hard_features`, exactly as the GROUP BY branch did.
- [ ] **LEFT-join and ANTI-join support** would unlock most of the remaining
      join sites (`pending_review`, `smart_money_tools`, both
      `strategy_auditor` anti-joins). It needs a left-outer helper alongside
      `join_rows`, not a flag on it — the two have different row counts.
- [ ] **94 dynamic/unparsed sites** need a human read before classification.
- [ ] **Vestigial `with get_db() as db:` blocks.** The codemod replaced the
      statements inside them but left the block, so a Postgres connection is
      still opened and unused at those sites. Removing one means re-indenting
      its body — do it deliberately, not with a regex.

### Test suite — 134 failing, all triaged

Baseline before conversion was 4,071 pass / 2 fail. Now **4,016 pass / 134
fail**. Every failing file was classified; none is an unexplained failure:

- [x] **21 source-shape guards** — count SQL patterns in files, so the counts
      moved when the SQL did. `test_price_history_one_vendor_guard.py` done:
      9 ratchet budgets LOWERED to the measured values (never raised — raising
      a ratchet to make it pass defeats it), 5 entries deleted on reaching 0.
      **584 pass, was 10 failing.**
- [x] **Template for the mock-based ports**: `tests/unit/test_watchlist.py`,
      8 pass (was 5 failing). The old mock patched `get_db` and is INERT after
      the codemod — reads went to the LIVE database and the test measured
      production data. Port patches `mongo_query` AND `mongo_store` together;
      stubbing only the read leaves writes pointed at the real store.
- [ ] **25 remaining mock-based files** follow that template. Some (e.g.
      `test_scoring_formula.py`) use a factory that dispatches on SQL text and
      need per-file judgement, not a regex.
- [ ] **2 files needing individual review**: `test_mongo_store.py`,
      `test_prompt_split.py`. Both look pinned to old behaviour rather than
      code bugs; neither is confirmed.

> Not merged to master. A branch with 134 failing tests is not a branch to
> merge, and the flag map is still untouched — `master` reads Postgres exactly
> as before, so the cycle is NOT split across two stores today.

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
      ⚠ **The codemod already converted `app/trading/paper_trader.py` below
      this bar** (verified 2026-08-17): reads via `mongo_query`, writes via
      ungated `insert_docs` with `cash_balance`/`total_pnl` as **floats**, and
      **zero `with_txn`** — the buy/sell multi-table transaction is now
      non-atomic. `dec128()` exists but nothing outside `app/db/` calls it.
      Before merge: wrap buy/sell in `with_txn` and apply Decimal128 per the
      collection map's 7 `dec128` entries, or record an explicit signed-off
      policy downgrade. Do not let a codemod overrule a twice-recorded
      decision silently.
- [ ] **Tier G — large time series.** `price_history` (15.75M rows) and
      `technicals` (1.37M) are **kept, not recomputed** (user decision: the data
      is expensive to re-collect). Rate-limit the backfill against the ~2 GB
      oplog and monitor lag.
- [ ] ⚠ **MEASURE THE SEED RATE BEFORE PROMISING `price_history` IS
      "unattended".** Sampled 2026-08-17 21:2x on the running sweep:
      **15 rows/s** on `execution_errors` (160,542 rows, natural-key index
      already present, so this is not the non-indexed 29 rows/s case). At that
      rate `price_history` alone would need **~12 days** and `technicals`
      ~25 hours. That is a measurement of ONE table with large documents —
      price_history rows are small and numeric and may go far faster — so the
      number to act on is the one taken **when price_history actually starts**,
      not this one extrapolated. But "finish the data, unattended" is not a
      safe assumption until that rate is on the record.
- [x] **The oplog constraint is weaker than recorded, because there is no
      secondary.** `replSetGetStatus`: **rs0 has ONE member**, PRIMARY. The
      oplog is currently **full — 2,050 MB of a 2,048 MB cap — with a
      1 day 7 h window**, down from the 2.1 days ch.69 recorded, because the
      sweep is writing hard and nothing is throttled (no `throttled to` line
      appears in this run's log). With a single-member set **no secondary can
      fall off it**, so this does not threaten backfill correctness or
      replication; it bounds point-in-time restore and change streams only, and
      backups are `mongodump`, not oplog replay. Do not carry "the oplog
      constrains the backfill" forward without saying which of those it means.

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

- [ ] **trading-client conversion is a pre-cutover phase, not an afterthought**
      (filed 2026-08-17; trading-client ch.73 Phase 5). The client has no
      `mongo_query` and ~67 files with SQL against this same database (writes
      52 tables, 44 joins). "Branch green" covers only the service — the
      moment the service stops writing Postgres, every client dashboard reads
      frozen data. Approach: port `mongo_query` + inventory/translator/codemod
      to the client (it already has the strict `mongo_store` write surface);
      first task is the per-table client read/write matrix.
- [ ] Work the collection-defect checklist so the collectors stop producing bad rows.
- [ ] Fix `trade_results` classification + field divergence.
- [ ] Fix embeddings parity to use its true natural key.
- [ ] Run the complete ceremony on one small service-only table and use that
      artifact as the template for the Tier B wave.
