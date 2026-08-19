# Handoff — Postgres→Mongo, 2026-08-18

**Not complete.** One of two repos is converted. Read this before touching
either branch.

Everything below is a command's output, re-run at handoff time. Where a number
came from a document rather than a measurement, it says so.

---

## State

| | measured now (2026-08-19) |
|---|---|
| **trading-service** `quality-purge` @ `5396be7` | Gate 1: **0 couplings in 0 files** · suite **5049 pass / 0 fail / 0 error** |
| **trading-client** `client-mongo-conversion` @ `10de5952` | **810 `.execute(` sites in 126 files — untouched** |

(An earlier run showed `test_prism_prompt_injection` failing on a vLLM 502.
The endpoint recovered and it passes; it touches no Mongo, Decimal or psycopg
code. Worth knowing it is environment-sensitive.)

**Phases 1, 2, 3 (partial) and 5 (partial) are DONE and pushed.** See
`documentation/chapters/` ch.76. What remains is phase 4 (the client) plus
the parity sweep and the cutover itself. The client count is 810/126, not the
564/67 recorded below: that figure excluded `app/` subpackages.

Both branches pushed, both worktrees clean, **nothing deployed**.

```
scripts/gate_zero_pg.py --self-test
  master (control): 1860 couplings in 186 files
  HEAD    (live)  :    0 couplings in   0 files
  PASS: the gate reports nonzero on master, so a zero on HEAD is meaningful.
```

Zero, and the control still fails on master, so the zero means something. The
remaining Postgres code lives under `scripts/migration/`, which the gate does
not scan by design — it is migration tooling that must keep reading the frozen
backup, not unconverted application code.

### Run these before trusting anything

```bash
cd /home/lazycat/github/projects/sun/.worktrees/ts-quality-purge
VENV=/home/lazycat/github/projects/sun/trading-service/.venv/bin/python   # the worktree has none

PYTHONPATH=$PWD $VENV scripts/gate_zero_pg.py --self-test        # progress + its control
PYTHONPATH=$PWD $VENV scripts/check_lost_pg_defaults.py --strict # lost PG DEFAULTs
PYTHONPATH=$PWD $VENV -m pytest -q --timeout=180 --ignore=tests/debug

# the money artifact needs a real (isolated) Mongo — it SKIPS without this
PYTHONPATH=$PWD TRADING_BOT_MONGO_TEST=1 $VENV -m pytest -q \
    tests/unit/test_money_is_cent_exact.py \
    tests/unit/test_index_creation_inside_a_transaction.py \
    tests/unit/test_deploy_interlocks_fail_closed.py

# in trading-client, after every conversion batch:
PYTHONPATH=$PWD python scripts/check_converted_tables_have_data.py
```

**Read the errors line, not just the failure count.** pytest counts them
separately; a `patch()` on a removed symbol raises at fixture *setup*, so the
test never runs and never appears as a failure. That hid 21 tests today behind
a "0 failed" summary I had already relayed as green.

---

## What is left, in order

### 1. `connection.py` teardown (S8) — **DONE** @ `6bc835f`
Gate 1 went 6 -> 0 (control still 1,860 on master). `connection.py`,
`migrations.py`, `init_db.py`, `schema_pg.sql` and `db_migrations.py` moved to
`scripts/migration/` — moved, not deleted, because the parity checks still read
the source store and the DDL must stay findable. psycopg split into
`requirements-migration.in`; the image no longer installs a Postgres driver
(`tests/unit/test_app_image_has_no_pg_driver.py`, sabotage-verified).

`table_spec.py` did NOT move, correcting the plan below: it imports no psycopg
at all (it takes an open `db` handle), so it never put the driver in the image,
and `mongo_query._money_cols()` now reads its per-column policy — moving it
would make `app/` import from its own tooling.

### 2. Money as Decimal (S3) — **DONE** @ `daa3dba`
The reverted attempt's diagnosis was right and its granularity was wrong. The
policy was per TABLE, so ratios (`stop_loss_pct`) and share counts (`qty`) were
promoted alongside real amounts, and `entry_price * (1 + effective_tp)` broke on
the RATIO. `table_spec.column_is_money()` is per COLUMN and both halves resolve
through it. 17 of 26 numeric columns across the 7 money collections are amounts.

The real defect was in the ledger, not the storage: the FIFO sell loop did
`lot_entry = float(lot_entry)` and accumulated P&L in float. Also fixed:
`agg_row`/`group_rows`/`join_rows` leaked a raw `bson.Decimal128`, so the same
column had a different type depending on which helper fetched it.

Artifact: `tests/unit/test_money_is_cent_exact.py` — real isolated Mongo, real
Decimal128, 1,000-movement reconciliation to the cent, with a negative control
proving float does NOT reconcile. (The first movement set cancelled out and the
control caught it. Keep that control.)

### 3. Seed + parity (S6) — **half done**
DONE:
- `ensure_key_index` moved INTO `backfill()`. It lived in `migrate_all.py` and
  was called from ITS loop, so seeding one table with `pg_to_mongo_backfill.py`
  — the documented way — upserted against a collection scan and the only
  symptom was slowness. That is the 15-29 rows/s figure.
- `backfilled_at` has a writer (`stamp_backfilled`). A MISMATCH deliberately
  does NOT stamp it; it records `backfill_last_attempt` instead, so a failed
  load cannot read as a completed one.
- Parity now compares money EXACTLY. `_normalize` demoted every Decimal to
  float before `_values_equal` applied `abs_tol=1e-9`, so the check could not
  fail on the drift Decimal128 exists to remove.

LEFT: run the actual sweep. `verify_all` (exhaustive, batched `$in`,
collation-safe) already exists and is the right tool — `--verify-fields`
samples and cannot certify a promotion. Money + live operational state get the
exhaustive sweep; everything else count-level, per the user's scoping. Nothing
has been re-timed since the index fix, so treat any previous rows/s figure as
void.

### 4. trading-client — tooling ported, routes still untouched
**810 `.execute(` sites in 126 files** (not 564/67 — that count excluded `app/`
subpackages). Zero routes converted.

DONE (`3faf7aa3`, `92700835`, `e4b75f00`):
- The codemod and the SQL translator now exist here. They lived only in
  trading-service, so there was no mechanical path at all. `sqlglot` — the
  parser they run on — was in **no requirements file anywhere**; it had been
  installed by hand into `trading-service/.venv`, so the codemod did not run in
  a fresh checkout. Pinned in `requirements-migration.in`.
- The money contract (`money_policy.py`, byte-identical to the service's copy,
  enforced by `check_backend_map.py`). It had to land BEFORE the routes: the
  client's `_to_tuple` did no Decimal128 unwrapping at all, so every converted
  money read would have handed a raw BSON object to a template.
- `scripts/check_converted_tables_have_data.py`, below.

**The scope estimate below was wrong.** Measured with the codemod's own
refusals rather than by counting joins:

```
376 call sites rewritable across 52 files
 72 skip: sql not a literal      <- the real blocker
 12 skip: shape not safely rewritable
 10 skip: JOIN / LEFT JOIN       <- not the blocker
~29 skip: casts, computed columns, subqueries, DISTINCT, DDL
```

Joins account for **10** refusals, not 51. The dominant blocker is non-literal
SQL (f-strings and variables), which is a different and more tractable problem.
The mechanically addressable set is 376, not "single digits".

**THE REAL BLOCKER IS THE SEED.** Of the 84 tables the codemod can rewrite,
**17 are empty in Mongo** — 84 of those 376 sites — including `scraper_queue`,
`price_history`, `technicals`, `data_flags` and `sec_13f_holdings`. Converting
those reads ships code that returns `None`/`[]` forever, which is a legitimate
answer no test or log distinguishes from "nothing matched". So phase 3's seed
genuinely does gate phase 4, table by table.

`scripts/check_converted_tables_have_data.py` makes that checkable: it walks
every `mongo_query` read in `app/` and fails if the collection is empty. Run it
after every conversion batch. Resolve through `collections.collection_for()`,
never the raw name and never the `collection_map` entry — `apply_renames` is
false, so those two give different answers (the map says
`ts_price_history`, which does not exist).

Correcting the note below: the wildcard `backend_for()` branch is **not** a
split-brain. It is present and identical in BOTH repos (the two copies differ
only by a trailing space). `mongo_query.py`/`mongo_store.py` do differ
substantially, but deliberately — the client edition has its own client and
does not own `ensure_indexes`.

Suggested order: seed the 17 tables, then convert in batches by file, running
the data check and the suite after each.

### 5. Cutover — after everything above
The deploy interlocks are **DONE** @ `daa3dba`, and they were worse than
"will die silently": one of them already had.

`_check_cycle_running.py` imported `psycopg2`, which is not installed anywhere
in this repo, and swallowed the ImportError into `unknown|`. Its caller only
blocked on `running*`, so `unknown` fell through to exit 0 — that hook has been
permitting every deploy, including ones that would kill a live cycle, while
looking healthy. `guard_deploy.py` warned (exit 0) on every failure path and
would have joined it the moment `pipeline_state:mongo` lands.

Both now read Mongo and both fail **CLOSED**, with an explicit
`DEPLOY_SKIP_CYCLE_CHECK=1` override: a false block costs minutes, a false
allow kills a 30-minute cycle. Also fixed: the probe's bare `load_dotenv()`
found no `.env` when invoked from a worktree, and the missing-venv path exited
0. `tests/unit/test_deploy_interlocks_fail_closed.py` runs both hooks as
subprocesses against the isolated test DB and was verified by sabotage on each.

Note `guard_deploy.py` lives in `sun/.claude/hooks/`, which is **not a git
repo** — that edit is on disk only and is not carried by this branch. There is
a **second copy** at `sun/.gemini/hooks/guard_deploy.py` (the Gemini/Antigravity
payload variant, not byte-identical); it had the same psycopg probe and the same
fail-open, and was fixed the same way and behaviour-tested the same way. If you
touch one, check the other — two copies of a guard drift, and the whole failure
mode here was a guard that looked installed and did nothing.

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
