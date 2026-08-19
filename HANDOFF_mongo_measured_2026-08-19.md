# HANDOFF — Postgres→Mongo, the measured store state (2026-08-19)

Session scope: audit how much of the trading cycle is actually converted, and
verify data is being stored in and retrieved from MongoDB. **Read-only
session — nothing was deployed, migrated, or committed.**

Full plan (five tracks, cutover runbook, verification protocol):
`~/.claude/plans/look-at-my-commits-stateless-hopcroft.md`. Read that for the
work breakdown; this file records what was *measured live* and the three
things it corrects.

---

## 1. The store census (measured against live PG + live Mongo)

```
PG (trading_bot, :5433)   198 tables   18,563,236 rows
Mongo (trading_bot)       141 colls     1,273,549 docs
migration_ledger.json     190 rows
```

Of the 140 tables present in **both** stores:

| bucket | count | note |
|---|---:|---|
| exact count parity | 60 | none of them empty-vs-empty |
| Mongo behind PG | 57 | total deficit only **9,771 docs** |
| Mongo ahead of PG | 23 | seed predates a PG bad-row purge, or Mongo-only writers |
| Mongo-only collections holding docs | 0 | no orphan/misnamed collection has been written |

**16 tables have rows in PG and no Mongo collection at all — but 13 of those
are treesearch/glass** (`canonical_strains`, `genomic_samples`,
`observations`, `glass_*`, …), which share the `trading_bot` public schema and
are **out of migration scope**.

### The genuinely unseeded trading tables are exactly THREE

```
price_history       15,761,344 rows
technicals           1,370,287
sec_13f_holdings       174,118
                    -----------
                    17,305,749  = 93% of all remaining PG row volume
```

Everything else in scope is seeded.

## 2. Correction — "0 of 190 tables seeded" was reading the wrong signal

`migration_ledger.json` carries `backfilled_at` on 0 of 190 rows, and that is
true. But it does **not** mean the seed never ran: 140 collections hold
1.27M docs at or near parity. **The seed ran broadly; the stamping writer
(`stamp_backfilled`, `pg_to_mongo_backfill.py:552`) has simply never been
invoked.** The missing artifact is an *evidence* gap, not a *data* gap.

Consequence for the plan: Track S is far cheaper than it reads. It is three
real backfills (two of them large, one of them `technicals` which is
recompute-not-migrate) plus a delta-seed and a stamping pass over tables that
already hold their rows — not 190 table migrations.

## 3. The 9,771-doc deficit is seed drift, not silent row loss

Production runs `master`, which writes PG and mirrors only 13 tables. So every
non-mirrored collection is a **frozen point-in-time snapshot** that drifts
further every cycle. Confirmed by max-timestamp probe, with the mirrored
tables as a positive control:

```
NOT mirrored -- frozen at seed time, PG has moved on:
  smart_money_trade_scores [computed_at]  pg=2026-08-18 09:00  mongo=2026-08-17 09:00
  news_articles            [published_at] pg=2026-08-19 02:12  mongo=2026-08-17 21:00
  company_registry         [updated_at]   pg=2026-08-19 02:20  mongo=2026-08-18 00:08
  eval_scores              [created_at]   pg=2026-08-19 04:20  mongo=2026-08-17 20:12

MIRRORED (live dual-write) -- CONTROL, matches to the second:
  pipeline_events          [timestamp]    pg=2026-08-19 04:31  mongo=2026-08-19 04:31
  execution_errors         [created_at]   pg=2026-08-19 04:31  mongo=2026-08-19 04:31
```

Split of the deficit: **3 mirrored tables account for 389 docs; 54
non-mirrored account for 9,382.** The mirrored 389 is the one number worth
attributing (it is the historical mirror-drop class); the 9,382 is expected
drift that the T0 delta-seed erases. **Do not re-derive parity from counts
taken while master is still writing PG** — every non-mirrored count is stale
by construction.

## 4. The cutover provenance oracle is valid — measured, with full separation

The plan's "reads come from Mongo" test relies on BSON truncating datetimes to
milliseconds while PG keeps microseconds. Measured over the 200 most recent
rows per table:

```
                    PG ms-aligned   Mongo ms-aligned
pipeline_events        0/200            200/200
execution_errors       0/200            200/200
trade_results          0/200            200/200
```

Zero false positives, zero false negatives, on all three. The discriminator
has real power on these columns and can be trusted at cutover. (Re-measure
per column before trusting it on a column whose source is already
ms-precision — there it has no oracle power and you fall back to the
`import psycopg` → ImportError proof.)

## 5. What is verified about storage/retrieval today

- **Stored:** yes, for the mirrored set — the live cycle's writes land in
  Mongo at the same second as PG (pipeline_events / execution_errors above,
  during cycle `cycle-v3-1787113320`).
- **Retrieved:** proven at store level; **not** proven end-to-end through an
  HTTP endpoint this session. `:8000` (service) was down locally and `:8888`
  is the documentation server, not the client API. The endpoint-level
  retrieval proof stays a cutover-time check (protocol stage (b) in the plan).
- Note for whoever picks this up: `local/trading-service` is `exited(137)` and
  `local/music-player` `exited(127)` — standing state, not caused here.

---

## Next actions, in order

1. **Commit the two dirty files in this worktree** — `scripts/sql_to_mongo.py`
   (`_refuse_system_catalogs`, `:795-838`) and the untracked
   `tests/unit/test_sql_to_mongo_refuses_catalogs.py`. They are the guard
   against the codemod rewriting `SELECT … FROM pg_tables` into Mongo finds
   at 10 client sites (valid code, empty list forever). Uncommitted, this
   reasoning is one `git checkout` from gone, and the ledger scanner only
   reads committed HEAD.
2. **Track S**, re-scoped by §1/§2: backfill `sec_13f_holdings` (cheap, and it
   unblocks client conversion), then `price_history` (measure the real rows/s
   in the first 5 minutes — every prior figure is void now that
   `ensure_key_index` runs before upserting), build the `technicals` recompute
   job, then a stamping pass so the ledger stops under-reporting reality.
3. **Track D**: `sector_aggregator.py:14` `datetime.date` BSON defect, and the
   parity harness's `placeholder_columns()` truncation (5 of the 6 ERRORs).
4. Tracks C / O / L as written in the plan.

## Traps carried into the next session

- **No partial deploy of `quality-purge` or `client-mongo-conversion`, ever.**
  The flag map is decorative on both branches (writes are ungated). Merge
  whole, both repos as one event.
- A cycle was **live** during this audit (`cycle-v3-1787113320`, phase
  analyzing). Nothing here touched it; keep it that way until the cutover
  window.
- `check_backend_map.py` **silently SKIPs** when run from a worktree — its
  default sibling path does not exist there. Always pass
  `--sibling /home/lazycat/github/projects/sun/.worktrees/tc-mongo-conversion`.
  A run without it is not evidence.
- `MIGRATION_CHECKLIST.md` is ~50 commits stale and actively wrong on
  paper_trader, refusals, vestigial `get_db`, and test counts. Its §0 deploy
  rule and §A2 anti-join analysis are the parts still worth reading.
- Postgres cannot be deleted at the end — treesearch's tables live in this
  same schema. "Done" = the trading cycle stops touching PG.
