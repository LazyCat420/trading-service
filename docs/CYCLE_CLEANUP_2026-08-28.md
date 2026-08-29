# Cycle cleanup — trading-service, 2026-08-28

Scope: the v3 cycle's step sequence, duplicate logic and dead ends. Read-only
audit first (`sun/.agents/audit-rules.md`), then implementation under explicit
per-item approval. Net **−5,685 lines** across 10 commits.

Companion decisions and the client/half-built-feature findings live in the
session plan; this file records what changed in this repo and why.

## 1. Correctness bugs (three real, one refuted)

### 1.1 Boot FRED refresh could never skip — FIXED
`BootService._startup_fred_refresh` called
`_is_data_fresh("macro_indicators", "source = 'fred'", 2)`. That helper's
`query` argument is a **Mongo filter dict**; a SQL `WHERE` string raised inside
its bare `except`, the helper returned `False`, and the "already fresh,
skipping" branch was unreachable. Every restart re-collected FRED.

Root cause was duplication: BootService carried private copies of the FRED,
market-collect and SP500-seed tasks alongside `app/services/startup_tasks.py`.
The copies had drifted in **both** directions — startup_tasks had a commodity
check and an always-run correlation compute BootService lacked; BootService had
price-collection and sector-analytics work on the already-seeded branch that
startup_tasks lacked. So the fix **merges** rather than picks, and
`startup_tasks` becomes the one owner. `startup_all` (zero callers) is gone.

### 1.2 A dead backend flag silently blanked the recency baseline — FIXED
The funnel's last-analysis lookup sat behind
`mongo_store.reads_mongo("analysis_results")`. The per-table backend map
defaults to `"pg"` and **never reached the store's own helpers** — `aggregate()`
always hits Mongo, and Postgres is gone — so the gate could only subtract. With
`MONGO_STORE_BACKEND` unset (any local run, any container that missed
`deploy.sh`'s env resolution) every candidate ticker scored as never-analysed:
no recency penalty, no freshness baseline, nothing in any log.

The flag machinery itself is **left in place** — retiring it spans both repos
plus the migration ledger, and its `"pg"` default is pinned by
`tests/unit/test_mongo_store.py`.

### 1.3 One bad import unmounted the entire API — FIXED
All 15 routers were imported and mounted in a single `try:`. Any import error
aborted the block before the first `include_router`, so the process served
**zero** routers — while `/health` and `/status`, declared above that block,
kept answering. The container reported healthy with its whole API missing, and
the one log line named no router. Each router now mounts independently and
failures are logged by name.

### 1.4 "Policy gates decided twice" — REFUTED, no change made
The confidence-floor check in `pipeline_service` is an `elif` **after** the
`HOLD_POLICY_BLOCKED` branch, so it fires only when the orchestrator produced no
`policy_action` at all (GLANCE early-return, DELTA tier, a crash before Layer 6).
It is documented in-code as measured-and-kept.

Re-verified against live data: 349 executable decisions all-time, 284 with a
null `policy_action` — but only 2 in the last 30 days, and **both are test rows**
(`cycle_id='test-cycle'`/`'test-cycle-123'`, 2026-08-18). Last real one is
2026-07-23 (C BUY conf 60, GOOG BUY conf 64 — the exact rows the code comment
cites). The gates now populate `policy_action` reliably; the backstop is free
and still guards live bypass paths. **Kept.**

### 1.5 What the repaired check revealed (follow-up, not a regression)

Verified live after deploy: the delegation works (the log line now comes from
`app.services.startup_tasks`), and the boot refresh **ran** rather than skipped.
That is the correct answer, and the reason is worth recording.

The gate is an AND of two conditions. Measured at 2026-08-28:

- `source=fred` newest **2026-08-28**, age 0d, threshold 2d → fresh
- `source=fred AND indicator=CPI` newest **2026-07-01**, age 58d, threshold 45d → stale

So the AND is False and it refreshes — exactly what the two-part check exists
for ("a bare MAX(date) is dominated by the daily treasury series and says
nothing about the monthly CPI/UNRATE lagging").

> [!NOTE]
> Today's observable behaviour is therefore UNCHANGED — it refreshed before the
> fix too. What changed is that it now refreshes for the right reason instead of
> because an exception forced `False`, and the skip branch is reachable at all.
> The reachability is what `tests/unit/test_startup_task_ownership.py` proves,
> since it fails against the previous boot_service.

**Open follow-up.** FRED dates monthly series by PERIOD START, so a monthly
indicator is between ~30 and ~60 days old at any moment. The 45-day CPI
threshold splits that range, which means the boot refresh will skip or re-collect
depending on where in the publication cycle the restart lands. Per-indicator ages
on the same day:

| Cadence | Indicators | Age |
|---|---|---|
| daily | INFLATION_EXPECT, TREASURY_3MO/2Y/10Y, VIX, HY_SPREAD | 0–1d |
| weekly | INITIAL_CLAIMS, DOLLAR_INDEX | 6–7d |
| monthly | CPI, PCE_CORE, PAYROLLS, UNEMPLOYMENT, FED_FUNDS, CONSUMER_SENT | 58d |
| quarterly | GDP, REAL_GDP_GROWTH | 149d |

The threshold was chosen while the check could never execute, so it has never
actually been exercised. It should be re-derived from each series' publication
cadence (or the gate keyed on the daily series with a separate, cadence-aware
staleness alarm for the monthlies) rather than left at a single 45-day number.
GDP at 149 days would fail ANY monthly-scaled threshold.

## 2. Deletions

| What | Lines | Evidence |
|---|---|---|
| 16 zero-caller modules | ~1,510 | exact dotted-path + relative-import grep, then an import sweep of all 398 modules under `app/` |
| Tournament stack + peer-request channel | ~3,000 | see below |
| Config with no reader, SQL fossil | ~40 | zero readers anywhere, incl. scripts |

### 2.1 The tournament
Retired on measurement: 239,028 tokens and 191s per ticker, **31% of all
pipeline spend**, for a debate-winner signal indistinguishable from noise
(bull-won n=57 mean −0.18%, bear-won n=67 mean −0.03%, t = −0.17). Not a wiring
bug — the winner reached the Board and moved it; the signal had no predictive
content. Those numbers are preserved verbatim in the `# Debate` block of
`parameter_store.py`, because they are the reason not to rebuild it.

It had also been unreachable far longer than the retirement flag implied:
`TOURNAMENT_MODE` defaulted `True` and routed every debate to the tournament,
leaving the bull/bear branch dead since 2026-07-12. What runs now is
bull/bear/judge — a different, much smaller mechanism that the measurement never
condemned.

> [!IMPORTANT]
> `tournament_result` **outlived the tournament** and is deliberately kept. The
> two debate-skip markers (regime panic, no-trade-available) still write it, the
> whiteboard subscriber still chains the Board off it, and both set `risk_flags`
> so `HOLD_POLICY_BLOCKED_UNMITIGATED_RISK` still fires. Only the `vetoed` read
> is gone — both surviving writers hardcode `False`, so the jury-veto gate could
> never fire again.

Also kept: `equation_library` and `backtest_runner` (live agent tools via
`quant_tools`) and `panel_math` (serves `scripts/score_panel.py`).

### 2.2 The peer-request channel
`request_peer_analysis` let an analyst ask a sibling for a data point. Measured
before removal: **103 lifetime calls, zero in the last 30 days** (last use around
2026-08-01), because its targets all run before requests drain and the quant
prompt already steered agents away from it.

Producer **and** consumer went together. Deleting only the orchestrator side
would have left agents able to file requests nothing serves — worse than the
status quo. The dispatch-once latches stay (an agent can still run up to
`MAX_RUNS_PER_AGENT` times), with their comments corrected to stop citing a
mechanism that no longer exists.

## 3. Consolidations

- **`START_CYCLE` had four copy-pasted writers** → `app/services/cycle_queue.py`.
  The queue *name* is the contract with the worker, and a copy-pasted writer is
  how a producer ends up addressing a queue whose drainer does not handle it.

  That is not hypothetical — but the precise shape matters, because there are
  **two legitimate queues**:

  | Queue | Drained by | Handles |
  |---|---|---|
  | `v3_system_commands` | `cycle_main.poll_system_commands` | START_CYCLE, STOP_CYCLE, FORCE_RESET, PAUSE, RESUME, FLASH_BRIEFING, MORNING_BRIEFING, DISCARD/FORCE_CHECKPOINT |
  | `system_commands` | `autoresearch/eval_worker.poll_system_commands` | AUTORESEARCH, ACTIVATE_BRAIN_GRAPH, RUN_FRED_COLLECTION, RUN_MARKET_COLLECTION, EVALUATE_STRATEGY |

  trading-client writes to both, and mostly correctly: MORNING_BRIEFING and
  FLASH_BRIEFING go to `v3_system_commands`; AUTORESEARCH, ACTIVATE_BRAIN_GRAPH
  and EVALUATE_STRATEGY go to `system_commands`, whose worker handles them.

  What is broken is the remainder, which land in `system_commands` where **no
  drainer matches the command type**: `REFRESH_SCHEDULE`
  (`app/routers/scheduler.py`), `ANALYZE_TICKER` (`app/routers/analysis.py`,
  whose SSE endpoint then polls for a status that never changes) and
  `DEPLOY_FIX`/`ROLLBACK_FIX` (`app/routers/autoresearch.py`, which have no
  handler in either repo). The endpoints all return success. Client-side fix is
  Area 2.
- **`_finite` ×3 → `app/utils/numeric.finite`.** Only one copy rejected bools;
  the other two coerced them, so `float(True)` → 1.0 could land in a metric as a
  plausible ratio. The shared guard takes the strictest behaviour.
- **`_parse_result_json` ×3 → `app/utils/json_utils.parse_json_field`.** The
  copies disagreed on their `except` clause; the shared one also returns `{}`
  for JSON that decodes to a non-dict, since every caller immediately `.get()`s.

## 4. Explicit tickers are validated again
`tickers=[...]` correctly skips the funnel — an operator naming a ticker means
that ticker. But it also skipped the funnel's **sanity filters**, so a typo or a
model-invented symbol reached the analysts. `FALSE_TICKERS` and `is_us_tradeable`
now apply, and an all-rejected request **raises** instead of falling through into
discovery and quietly analysing a different set. The recency exclusion is
deliberately *not* applied: a manual run is an override.

## 5. Two findings worth carrying forward

### 5.1 Six test rows in production collections — cause found, hole already closed
`trade_results` ×2, `shared_desk` ×2, `analysis_results`, `cycle_run_summaries`,
all 2026-08-18, one a BUY at confidence 0.

The 2026-08-17 codemod (`421f931`) moved `save_trade_result` from Postgres to
Mongo, but its test still patched `app.db.connection.get_db` — **a mock aimed at
a seam the code had left**. The patch applied cleanly, guarded nothing, and the
write went to production.

Closed the next day by `5b85ab9` plus conftest's autouse, fail-closed
`block_production_mongo`. **Verified 2026-08-28 that the guard still fires.**
The 6 stale rows remain; deleting production data was out of audit scope.

> [!WARNING]
> This trap recurred during *this* cleanup. Moving the `START_CYCLE` insert into
> `cycle_queue.py` orphaned `test_clock_boundary`'s mock, which patched
> `cycle_scheduler.mongo_store`; the write went at the real client and
> `block_production_mongo` caught it loudly. After moving any write, re-point the
> tests at the module that **performs** it.

### 5.2 Deliberately not touched
`app/quant/returns.py`'s `dominant_source_sql` emitters look like the same PG
fossil but have live callers (two backtest scripts, two tests that monkeypatch
them). They belong with the wider decision about porting the ~87 scripts stranded
on the removed `psycopg`.

## 6. Verification
- Full unit suite: **5,211 passed / 75 skipped**.
- The only failures are pre-existing and environmental, both **confirmed failing
  on unmodified master**: `test_prism_prompt_injection` (vLLM Gold Spark 502) and
  `test_dynamic_trigger_normalisation` (the documented `-n 8` flake; passes
  alone). A master baseline run also showed `test_migration_ledger`.
- Every behavioural change carries a guard test **proven red against the
  pre-change file** — not merely green after it.
- Import sweep of all modules under `app/` after each deletion batch (0 broken).
- No live cycle at deploy time (`pipeline_state` = `done`).
