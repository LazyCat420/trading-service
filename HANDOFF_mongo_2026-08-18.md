# Handoff — Postgres→Mongo, 2026-08-18

**Not complete.** One of two repos is converted. Read this before touching
either branch.

Everything below is a command's output, re-run at handoff time. Where a number
came from a document rather than a measurement, it says so.

---

## State

| | measured now |
|---|---|
| **trading-service** `quality-purge` @ `4207af8` | Gate 1: **6 couplings in 1 file** · suite **5000 pass / 0 fail / 0 error** |
| **trading-client** `client-mongo-conversion` @ `10de5952` | **564 `.execute(` sites in 67 files — untouched** |

Both branches pushed, both worktrees clean, **nothing deployed**.

```
scripts/gate_zero_pg.py --self-test
  master (control): 1805 couplings in 187 files
  HEAD    (live)  :    6 couplings in   1 file
  PASS: the gate reports nonzero on master, so a zero on HEAD is meaningful.
```

The 6 are `app/db/connection.py` itself. `connection_import` and `get_db_call`
are both zero — no application module imports or calls `get_db`.

### Run these before trusting anything

```bash
cd /home/lazycat/github/projects/sun/.worktrees/ts-quality-purge
VENV=/home/lazycat/github/projects/sun/trading-service/.venv/bin/python   # the worktree has none

PYTHONPATH=$PWD $VENV scripts/gate_zero_pg.py --self-test        # progress + its control
PYTHONPATH=$PWD $VENV scripts/check_lost_pg_defaults.py --strict # lost PG DEFAULTs
PYTHONPATH=$PWD $VENV -m pytest -q --timeout=180 --ignore=tests/debug
```

**Read the errors line, not just the failure count.** pytest counts them
separately; a `patch()` on a removed symbol raises at fixture *setup*, so the
test never runs and never appears as a failure. That hid 21 tests today behind
a "0 failed" summary I had already relayed as green.

---

## What is left, in order

### 1. `connection.py` teardown (S8) — small, do it first
Still on disk: `connection.py` 18KB, `migrations.py` 205KB, `schema_pg.sql`
116KB, `init_db.py` 13KB, `db_migrations.py` 7KB. `requirements.in:14` still
has `psycopg`.

Delete the DDL **in the same change as the drop**. A `DROP TABLE` here survives
only until the next boot: `run_migrations()` runs at startup and both
`migrations.py` and `schema_pg.sql` declare the tables with
`CREATE TABLE IF NOT EXISTS`. That is how 40 of 57 "purged" tables came back
empty and the live count read 197 rather than 157.

`app/db/table_spec.py` must NOT be converted — it reads `information_schema`
for the backfill's column mappers and its only callers are the migration
scripts. Move it to `scripts/migration/` with psycopg, so the app image drops
the driver while parity tooling keeps working against the frozen backup. Gate 1
already exempts it and prints the reason on every run.

### 2. Money as Decimal (S3) — designed, deliberately not flipped
The plumbing is in and tested: `_clean_val(as_decimal=...)` and `_money()` in
`app/db/mongo_query.py`, resolving through `table_spec.uses_decimal128`.
Flipping it works — `100000.07 + 0.03` gives `100000.10` instead of
`100000.09999999999`.

It is reverted anyway, and the docstring in `_clean_val` says why: the money
path mixes money with things that are not money — ratios (`take_profit_pct`),
vendor quotes still stored as float, share counts. `entry_price * (1 +
effective_tp)` raises `TypeError` the moment `entry_price` is a Decimal, and
that is **one of 39 such sites across 8 modules** (paper_trader 13,
bot_manager 11, portfolio 10).

Doing it means deciding per boundary whether to promote the float or demote the
Decimal, and proving it with the **cent-exact reconciliation artifact** Tier F
requires — not with a green suite.

Correct the premise while you are there: the plan says "psycopg-NUMERIC
parity". There are **zero** NUMERIC columns in `schema_pg.sql` and **328**
`DOUBLE PRECISION`. Postgres returned floats. Decimal128 is an upgrade being
chosen, which is what the 2026-07-21 decision actually said.

### 3. Seed + parity (S6) — not started
`migration_ledger.json`: **161 migrate-scope tables, 0 with `backfilled_at`.**
The writer was never built; the fields exist and are empty. Any "135/141
seeded" claim has no artifact behind it.

Prime suspect for the 15 rows/s seed rate, verified in code and unfixed:
`pg_to_mongo_backfill.py` never calls `ensure_key_index`, so a direct-entrypoint
seed upserts against a collection scan (~29 rows/s is the documented signature
of exactly that). Move the call *into* `backfill()` so the invariant cannot be
skipped by entrypoint choice, then re-time before believing the 12-day
price_history extrapolation.

Per the user's decision, exhaustive parity is scoped to money + live
operational state; everything else gets count-level checks. Data preservation
is explicitly not the goal — the frozen PG container is the archive.

### 4. trading-client — the whole thing, untouched
564 `.execute(` sites in 67 files, plus 220 more in `scripts/`. The branch adds
only a dead `mongo_query.py` and a client-only wildcard `backend_for()`; it
converted **zero** routes.

Hard part measured: ~67 joins — **38 LEFT JOIN, 13 LATERAL, 14 against CTE
aliases**. `join_rows()` does two-table INNER on one equality and refuses
LEFT/ANTI by name, so the mechanically addressable set is single digits.
`screener.py` (15 joins) and `data.py` (12) hold ~40% of it.

Before starting: delete the wildcard `backend_for()` branch (the client honours
a `*` entry the service ignores, in a byte-identical shared file — silent
split-brain), and unify `mongo_query.py`/`mongo_store.py` across the two repos
with the byte-identity check that already exists for `collection_map.json`.

### 5. Cutover — after everything above
Mongo-aware deploy interlocks **first**: `.claude/hooks/guard_deploy.py` and
`trading-service/.claude/hooks/_check_cycle_running.py` both read
`pipeline_state` from Postgres and fail open. They die silently the moment that
table leaves PG.

Then, as one event: stop both containers → final delta-seed + verify →
ff-merge both branches → deploy service first (it owns `ensure_indexes`) then
client → PG container left exactly as-is, for the user to retire.

`apply_renames` stays **false** (user decision). Remove the 30-table
`MONGO_STORE_BACKEND` stanza from `deploy-kit/.env.deploy` in the merge change
— after it, nothing defuses it.

**No partial deploy of either branch, ever.** Branch writes bypass the flag
map, so deploying one alone splits the two containers across two stores.

---

## Things that will bite you

**`git stash` is repo-wide across worktrees.** I ran it to compare against HEAD
while four agents were mid-conversion and swept up their in-flight edits; a
second agent independently hit a `git reset --hard` from elsewhere. Reconciled,
nothing lost, but the correct move is a separate checkout. A tree several
agents are writing to has no safe global git operation.

**`$ne` is asymmetric**, and was asserted wrongly in both directions in one day,
including by me:

```
{'f': {'$ne': None}}      -> EXCLUDES a document missing the field   (= IS NOT NULL)
{'f': {'$ne': "system"}}  -> INCLUDES one   (= COALESCE(f,…) <> 'system')
```

Both are pinned in `tests/unit/test_sql_null_semantics_in_mongo.py` against a
real isolated database. I "fixed" `research_governor` on the wrong rule, then
measured and reverted — the original was right.

**Mongo has no column DEFAULTs.** A converted INSERT that relied on one writes a
document missing the field, and any reader filtering on it silently sees
nothing. Three live bugs came from this. `check_lost_pg_defaults.py --strict`
must stay clean.

**A `writes_pg(...)` branch that calls `mongo_store` is a duplicate writer.**
Seven were found. `tests/unit/test_no_duplicate_mongo_writers.py` guards it.

**Test the guard, not just the code.** Four times today a *check* was the
problem: a source-text vendor guard that condemned correct code and would have
passed on a gutted helper; a defaults scanner that could not see the bug that
motivated it; 21 tests erroring behind "0 failed"; and the pure-Mongo E2E
quietly scraping the live web. Every guard here now carries a negative control,
and several were verified by sabotage. Do the same.

---

## Known-broken, filed not fixed

Recorded as trading-client **open item 57**; preserved verbatim so a migration
commit did not change trading behaviour:

- `sector_aggregator.get_sector_stocks` compares against the previous
  **calendar** day, so `return_1d` is 0 for every stock whenever the latest bar
  is a Monday or follows a holiday — roughly one session in five.
- `market_regime.yield_2y` is populated with the **10-year** yield, so
  `yield_2y10y_spread` is a hardcoded 0.0 and an inverted curve is undetectable.
- `_backfill_cycle_summaries` has **never executed** — its alias collides with
  the reserved word `do`, raising SyntaxError on every call since 2026-08-07.
  Left as an explicit no-op.
- The `price_history` one-vendor guard **decays toward vacuous**: it scans SQL
  literals, so a module moving to Mongo leaves its view and the debt retires by
  becoming invisible. A Mongo-side scan was added, but check it still
  discriminates before the last SQL reader leaves.

## Where the record lives

trading-client `documentation/chapters/` — **74** (the suite writing to
production; the count was never 141), **75** (the eight defects, the four
broken checks, the money premise), and **open item 57**. Served at
`http://10.0.0.16:8888/documentation`.
