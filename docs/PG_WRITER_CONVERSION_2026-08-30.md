# Converting the last Postgres writers, and auditing what the data is for — 2026-08-30

Service-side record. Client chapter:
`trading-client/documentation/chapters/104-the-writers-that-never-left-postgres-2026-08-30.md`.
Branch `pg-writer-conversion`. Follows the 08-30 audit in
`PREFLIGHT_FAILED_OPEN_2026-08-30.md`.

## What was wrong

The cutover was 2026-08-19 and the **image** has been clean since 08-18
(`tests/unit/test_app_image_has_no_pg_driver.py`). `scripts/` was never swept:
**18 scripts still issued INSERT / UPDATE / DELETE against the archive.**

A Postgres write here is silent. It succeeds, the script exits 0, and the row is
simply not where the cycle reads.

| script | table | consequence |
|---|---|---|
| `jetson_benchmark.py` | `box_benchmark_runs` | **actually ran** — rows 114..136 (08-20..08-27) existed only in PG |
| `trigger_canary.py`, `canary_loop.py` | `v3_system_commands` | enqueued onto a queue `cycle_main.poll_system_commands` does not drain — the canary could never fire |
| `clear_db.py`, `fix_db.py`, `reset_pipeline_for_user.py` | `pipeline_state`, both command queues | printed success while the live control plane stayed stuck |
| `congress_backfill.py`, `backfill_bioguide_ids.py`, `populate_members.py`, `populate_historical_members.py` | congress feed | a re-run would have filled a store nothing reads |
| `populate_sp500.py` | `price_history`, `ticker_metadata` | as above, on the core price table |
| `mine_shkreli_doctrine.py` | `shkreli_opinions` | feeds the live desk via `app/v3/opinion_block.py` |
| `scrub_poisoned_memories.py` | `evolution_lessons`, `cycle_context`, `embeddings` | scrubbed the archive, left live docs poisoned |
| `db/wipe_13f.py` | `sec_13f_*` | destructive, against the wrong store |
| `autofix/run_autofix.py` + `autofix/worth_report.py` | `autofix_runs` | writer and reader agreed with each other and with nothing else |
| the four backfills | `decision_outcomes`, `shared_desk`, `price_backfill_progress` | — |

## Translations that were not mechanical

- **`ON CONFLICT DO NOTHING` had no bulk form.** `upsert_doc(insert_only=True)`
  covered one document; the only bulk path was `$set`, i.e. DO UPDATE.
  Converting a DO NOTHING writer through it silently changes behaviour —
  `congress_trades` ids are content hashes and the row deliberately keeps the
  FIRST disclosure seen. Added `bulk_upsert(insert_only=...)`.
- **`jsonb_set` on `desk_data` — `desk_data` is JSON TEXT.** A `$set` on
  `"desk_data.final_decision"` creates a dotted field beside the string and
  leaves the artifact untouched. Read, patch, re-serialise, and write back in
  the shape it was read in.
- **`SET overridden_from = action`** copies a column into a column; `$set`
  cannot express it. Written per id from values already in hand.
- **`length(user_prompt) > 200`** → `$strLenCP` inside `$expr`, so the filter
  stays in the database.
- **`UNION ALL` of two GROUP BYs** → `$unionWith`, one round-trip.
- **`count(*) FILTER (WHERE c)`** → `$sum: {$cond: [c, 1, 0]}`.
- **`coalesce(col,'') <> ''`** → an `$ifNull` comparison, which also fails for a
  MISSING field — `$ne: None` would have matched neither.
- **`latest_quarter IS NOT NULL`** → `$nin: [None]`, for the same reason.
- **Per-row writes → bulk.** `populate_sp500` issued one statement per bar
  (~125k for 500 tickers); it now does two `bulk_upsert`s.

## Two dead limbs

`resolve_bioguide_id(db, name)` still took a DB handle the cutover had stopped
reading — the live caller in `congress_collector` was already passing `None`,
which is what proved it dead. Removed.

`DbLoggingHandler`'s docstring claimed it wrote "into the PostgreSQL
`execution_errors` and `cycle_audit_log` tables" for six weeks after the writer
became `mongo_store.insert_docs`. That sentence was the only reason `app/`
appeared in the sweep.

## Verification

Every conversion was exercised against live Mongo, not just compiled:

- `box_benchmark_runs` recovery: 23 rows inserted, collection 113 → 136 matching
  PG, re-run reports 0 missing (idempotent).
- `jetson_benchmark._db_attribution` → 183 SUCCESS / 6 AGENT_ERROR / 3
  EMPTY_RESPONSE / 1 HARNESS_ERROR; `load_corpus(3)` → prompts of 1853-1854
  chars; `cycle_is_running` → `cycle-v3-1788074145 status=done`.
- `wipe_13f --dry-run` → 174,119 holdings, 543 of 556 filers — matching the
  day's backup exactly.
- `backfill_price_history._load_universe` → 11,603 tickers ordered AAPL, MSFT,
  AMZN, NVDA…, **0** failing the `^[A-Z]{1,5}$` translation; `_already_done` →
  3,722, matching the backup.
- `backfill_desk_decisions` → 1,062 desks joined, 2 repairable (LMT, MTD), left
  as a dry run.
- `worth_report` → repair queue 4 rows / 0 attemptable (the known no-fuel
  finding, reproduced through the new path) and 348,875 execution_errors with
  10,628 carrying a stack trace.
- `mine_shkreli_doctrine._known_tickers` → 3,120 tickers, contains AAPL/MSFT,
  excludes VKTX — exactly what its docstring claims.

Three scripts report "nothing to do", so each was checked **non-vacuously**:
the blocked-label join has 46 firing keys, 33 with an outcome row, and all 33
already labelled (the other 13 are `cycle-test`/`TEST` rows); the scrub scans
571 real lessons with the detector proven to fire on a `POISON_SUBSTRING` and
stay quiet on clean text.

`tests/unit/test_no_pg_writers_for_trading_data.py` is **red on the
pre-conversion tree, naming all 18**, and green here.

## Data-usage audit

Full results in the client chapter. Headlines:

- 145 collections / 19,128,193 docs / 2,453 MB. **118 LIVE**, 10 write-only,
  6 read-only, 11 unreferenced.
- The first pass scanned only `app/` and called 17 collections dead. The
  dashboard reads every one of them — a single-repo answer to "is this used" is
  wrong by construction.
- Real dead weight: **19 MB of 2,453 MB (0.8%)**, concentrated in
  `agent_audit_log` — 91,198 docs, +1,270/day, **zero readers in either repo**.
- Field level is where the waste is: `news_articles` carries five enrichment
  fields empty across 116,345 rows (`is_cluster_winner` **never written once**;
  `llm_summary` writer dead since 8528bb0, newest enriched article 2026-06-20).
  Also `sec_13f_holdings.pct_change`, `llm_audit_logs.agent_task_id`,
  `cycle_run_summaries.{jetson_healthy_start,schedule_id}` empty, and
  `{report_published,review_count,lesson_stored}` populated but named nowhere.

⚠ **A sampling trap that produced a false finding**, recorded so it is not
repeated: the first field pass used `find({}, limit=4000)`. Natural order
returns the OLDEST documents, so every recently-added field read as "always
empty" — it claimed `v3_agent_telemetry.error_message` was never populated when
the collection holds 280 such values and 3,587 `model_used`. With `$sample`
that collection is clean. **On a growing collection a limit-sample is a sample
of the past.**

## Open

- `agent_audit_log`: give it a reader or stop writing it. Operator call.
- The `news_articles` enrichment writer has been dead ~10 weeks; restore it or
  drop the five fields.
- `decision_scores` is write-only *because* its reader
  (`decision_score_report.py`) is one of the dead PG instruments — the same
  finding as the previous doc, from the other end.
- The 91 read-only PG scripts are untouched. Writers were the data-loss risk;
  readers are the wrong-answer risk, and 19 still answer from the 08-19 archive.
- ⚠ `.claude/hooks/guard_db.py` scans `.scratch/db-backups/` for **files** at
  the top level only. Its own instructions say to use `mongodump`, which always
  produces a **directory** — so a correct backup is invisible to it and it
  refuses the command anyway. Today's backup (221,448 documents across 18
  collections, verified by reading it back) sits in a dated subdirectory.
