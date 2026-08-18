# Handoff — Postgres → MongoDB conversion, 2026-08-17

Branch **`quality-purge`**, 13 commits, **not merged**. `master` is untouched:
the trading cycle still reads Postgres end to end, so nothing is split across
two stores today. That property is worth preserving until the branch is green.

Everything below was measured, not estimated. Re-run the commands to refresh.

---

## 1. Where it stands

| | start | now |
|---|---|---|
| Postgres tables | 214 | **157** (57 archived + dropped) |
| Bad rows purged (PG) | — | **244,650** |
| Bad docs purged (Mongo) | — | **183,712** |
| Tables seeded into Mongo | 0 | **135 / 141** |
| SQL call sites in `app/` | 1,274 | **783** |
| — of those, application code | 969 | **478** |
| Sites converted by codemod | 0 | **578** |
| Statements still refused | — | **117** |
| Tests | 4,071 pass / 2 fail | **4,010 pass / 140 fail** |

**Still to seed:** `technicals` (1.37M) and `price_history` (15.76M). The sweep
was on `news_articles` when this session ended. Re-run:

```bash
PYTHONPATH=$PWD python scripts/migrate_all.py            # idempotent, resumable
```

---

## 2. The tools — read these before writing new ones

| file | what it does |
|---|---|
| `scripts/quality_gates.py` | THE definition of bad data. SQL predicate + Mongo query per gate. Census and both purges import it; none restates a predicate. |
| `scripts/quality_census.py` | Read-only. Measures every gate, writes `docs/DATA_COLLECTION_IMPROVEMENT_CHECKLIST.md`. Exits non-zero on a broken gate. |
| `scripts/purge_bad_data.py` | Postgres purge. Archives each dropped table with row count + sha256 + restore command first. |
| `scripts/purge_mongo_bad_data.py` | Same gates against Mongo. |
| `scripts/check_natural_keys.py` | **Run this before any backfill.** Asserts `count(*) == count(DISTINCT key)` per table. |
| `scripts/sql_inventory.py` | Parses every call site with sqlglot, classifies it. The scoreboard. |
| `scripts/sql_to_mongo.py` | The translator. **Refuses rather than approximates.** |
| `scripts/codemod_pg_to_mongo.py` | Applies translations to files. Re-parses before writing. |
| `scripts/verify_translations.py` | Runs each translation against BOTH stores and compares rows. |
| `scripts/migrate_all.py` | Bulk data sweep. Creates the natural-key index before each table. |
| `app/db/mongo_query.py` | `find_rows` / `find_row` / `agg_row` / `group_rows` / `join_rows` / `exists` / `count`. |

### Why `mongo_query` exists

Application code reads results **positionally** (`r[0]`). `mongo_store.find_docs()`
returns dicts, so a direct swap turns every one of 578 call sites into a
KeyError. `mongo_query` returns **tuples in the SQL's column order**. That
shape-compatibility is the only reason this conversion could be mechanical.

Keep that invariant. Any new helper must return the same shape as the cursor
method it replaces, or the codemod must refuse the site.

---

## 3. What is left, in the order I would do it

### 3.1 Finish the data (unattended)
`technicals`, `price_history`. Both key-verified sound. Rate-limited in
`migrate_all.py` because the oplog is capped at 2.15 GB.

### 3.2 The 117 refused statements — 89 are in the trading cycle

```bash
PYTHONPATH=$PWD python scripts/sql_inventory.py --json /tmp/inv.json --show redesign
```

| feature | count | approach |
|---|---|---|
| JOIN | 37 | `mongo_query.join_rows()` is **written but unused** — wire it into the translator. Deliberately not `$lookup`: that scans per input doc on an unindexed field, and its left-outer semantics silently turn an INNER JOIN into a pass-through. |
| subquery | 18 | case by case |
| CTE | 17 | usually a subquery + join; do those two first |
| aggregate (leftover) | ~25 | shapes `agg_row` refuses — conditional SUMs other than the IS-NULL idiom |
| window | 2 | `LEAD()` gap detection in `data_audit.py`; compute in Python |

Largest clusters: `fund_scanner.py` (13), `data_sanity.py` (9),
`data_audit.py` (7), `pipeline_service.py` (7).

### 3.3 The 221 "mechanical but not rewritten"
These translate fine; the codemod refuses the **call-site shape**:
- 90 — SQL is not a literal (built at runtime)
- 61 — `cur = db.execute(...)` used later, so the rewrite must follow a variable
- rest — parameters passed as a variable rather than a list literal

Extending the codemod to handle `cur = db.execute(...)` assignment is the
single highest-yield change left on the code side.

### 3.4 Tests — 140 failing, all triaged, none unexplained
- **21 source-shape guards** — count SQL patterns in files. `test_price_history_one_vendor_guard.py` is done (584 pass); the rest follow the same shape. Only ever LOWER a ratchet.
- **~25 mock-based files** — their `db.execute` mock is now **inert**, so the test reads the LIVE database. Template: `tests/unit/test_watchlist.py`. Patch `mongo_query` **and** `mongo_store` together; stubbing only the read leaves writes pointed at production.
- **2 needing review** — `test_mongo_store.py`, `test_prompt_split.py`.

### 3.5 Only after the above
Vestigial `with get_db() as db:` blocks (the body was converted, the block still
opens an unused Postgres connection), then remove `mongo_backends.env` /
`pg_write_guard` / dual-write, then run a real cycle.

---

## 4. Things that will bite you

1. **treesearch-service owns 13 tables in this same database** (`observations`,
   `canonical_strains`, `genomic_samples`, the `glass_*` family). Excluded by an
   explicit allowlist in `quality_gates.py`, re-checked before every statement,
   row counts asserted unchanged after each destructive run. Do not drop them.

2. **A generated catalog is a cache with no invalidation.** `schema_manifest.json`
   claimed `PRIMARY KEY (id)` for `cycle_summaries`, which has no `id` column.
   That propagated manifest → ledger → spec → backfill and upserted 2,499 rows
   onto 262 documents. `check_natural_keys.py` catches this class by asking the
   DATA, not the declaration.

3. **Purging one side of a mirrored pair is not purging.** Cleaning Postgres
   left 183,712 bad docs in Mongo. The purge completed successfully and said
   nothing.

4. **A mock that no longer intercepts anything reads the live database.** Every
   conversion makes another test's mock inert. Convert a module and port its
   tests in the same commit, or the suite drifts.

5. **Index the natural key before backfilling.** Without it every upsert is a
   collection scan: measured 29 rows/sec and degrading. With it, ~65 tables in
   45 seconds.

6. **Shape mismatches do not crash the codemod.** `mongo_query.count()`
   returned a bare int where `fetchone()` returned a tuple; `row[0]` would have
   raised TypeError at every site. Removed in favour of `agg_row`.

---

## 5. The rule that earned its keep

**Refuse, never approximate.** Every construct not implemented exactly raises
`Unsupported` with a reason. The differential checker then runs translations
against both stores and compares rows — it found, among others:

- `IS NOT NULL` translating to `IS NULL` (sqlglot encodes it as
  `Is(negate=True)`; the flag went unread) — returned the **complement** of the
  intended rows.
- `LIMIT 0` returning the whole collection (pymongo reads 0 as unlimited).
- `ON CONFLICT DO NOTHING` becoming an overwriting `$set` (the action is a
  `Var` node, not the string `"NOTHING"`).
- `ORDER BY n` on an aliased aggregate sorting on a field that does not exist.

Every one of these parsed cleanly and would have shipped. None was findable
without running both sides against real data.

```bash
PYTHONPATH=$PWD python scripts/verify_translations.py --limit 250 --show-differ
```
Last run: **94.2% of comparable statements match**; the rest are `NOT_SEEDED` or
`UNTESTED`, reported separately and never counted as passes.

---

## 6. Backups

```
~/db-backups/trading_bot_pre_purge_20260817.dump        1.16 GB  + .sha256
~/db-backups/mongo_trading_bot_pre_purge_20260817.gz     372 MB  + .sha256
~/db-backups/purged-tables/*.dump                        57 files, manifest in
                                                         docs/PURGED_TABLES_MANIFEST_2026-08-17.json
```
The PG dump was verified restorable (`pg_restore --list`, 226 TABLE DATA entries).
