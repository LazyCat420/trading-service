# Boot-time resilience and the sector-compute fix — read-only audit

Scope: the 90-day bound on `compute_sector_performance` (sector_aggregator.py),
its regression test, the backfill path, the vllm-shim metrics poller, and
process readiness. All evidence below is file:line against
`/home/lazycat/github/projects/sun/trading-service` (primary) plus a local,
Mongo-untouched pandas experiment and read-only Mongo aggregations.

---

## 1. The loop-responsiveness test

**File:** `tests/unit/test_boot_compute_does_not_block_the_loop.py`

The workload is `time.sleep(BLOCK_S)` inside `async def _blocking_compute()`
(lines 48-51), `BLOCK_S = 0.40`. It is used two ways:

- Directly, in `TestTheLoopKeepsBreathing` (lines 72-104): a "direct await"
  control and an "`_off_the_loop` keeps it responsive" check.
- As a **substitute for the real analytics**, via `monkeypatch.setattr`, in the
  one test that actually calls the real boot code path —
  `TestTheDailyRefreshDoesNotPinTheLoop::test_the_sp500_refresh_keeps_the_loop_breathing`
  (lines 191-217). Lines 202-207:

  ```python
  monkeypatch.setattr(sector_aggregator, "backfill_sector_performance", _blocking_compute)
  monkeypatch.setattr(sector_aggregator, "compute_sector_performance", _blocking_compute)
  ticks, _ = await _ticks_during(lambda: BootService._sp500_full_refresh(period="5d"))
  ```

**Does the workload hold the GIL?** No. `time.sleep()` is a blocking OS call
that CPython implements with the GIL released for its duration — that is
precisely why it is safe to await directly on an event loop's thread pool
executor without contention. It is not a stand-in for a C-level call that
holds the GIL (pandas `groupby`/`pct_change`/`rolling`, `sorted()`, a large
`sum()`, BSON decode).

**Would this test fail if the bounded query were reverted?** No, and not
narrowly — structurally no test in this file can observe the real query at
all:

- `TestTheLoopKeepsBreathing` and `TestTheDailyRefreshDoesNotPinTheLoop` never
  call `compute_sector_performance()`; the latter explicitly replaces it with
  `_blocking_compute` before invoking `BootService._sp500_full_refresh`.
- `TestNoBareAwaitsRemain` is a static AST check for bare `await
  compute_sector_performance(...)` calls in two named files — it has no idea
  how many rows the query reads or how long the real computation takes.

So reverting `SECTOR_PERF_WINDOW_DAYS` (sector_aggregator.py:120) back to an
unbounded read (the pre-fix state: 4,365,690 rows, ~160s, per the comment at
sector_aggregator.py:113-119) would leave every test in this file green,
unchanged.

**A faithful workload** would be something that spends its time inside a
single C-level call with no natural bytecode-boundary yield points — e.g. the
real pandas pipeline run over data shaped like the real bounded read (tens of
thousands of rows, groupby + pct_change + rolling), or a comparably-sized
pure-Python CPU-bound computation (a large in-place sum/sort/hash over an
in-memory structure) — not an I/O sleep.

### Empirical check (local only, no Mongo writes)

To measure rather than assert, I copied the transform block from
`compute_sector_performance` verbatim (sector_aggregator.py:148-235) into a
synthetic-DataFrame harness (509 tickers × 62 rows ≈ 31,558 rows, matching the
real bounded read's shape) and ran it through a byte-for-byte copy of
`_off_the_loop` (`startup_tasks.py:164-191`), racing it against the same
10ms-tick heartbeat coroutine the test file uses. Script:
`scratchpad/gil_experiment.py` (no DB access; not committed to the repo).

| workload | how run | elapsed | ticks | ticks if fully free |
|---|---|---|---|---|
| real pandas pipeline | direct `await` | 0.825s | **1** | ~82 |
| real pandas pipeline | via `_off_the_loop` (to_thread) | 1.411s | **55** | ~141 (39%) |
| `time.sleep(0.4)` | via `_off_the_loop` | 0.412s | **38** | ~41 (93%) |
| pure-Python busy loop (no C ext calls) | via `_off_the_loop` | 0.405s | **18** | ~41 (44%) |
| real pandas pipeline (5 repeats) | via `_off_the_loop` | 0.82–2.80s | 41–67 | 39–48% each |

This directly confirms the mechanism the audit was asked to check:
`asyncio.to_thread` (what `_off_the_loop` uses) does **not** fully exempt
CPU-bound work from GIL contention with the event-loop thread — the real
pandas transform, run through the exact fix's own wrapper, sustains only
~39% of the heartbeat cadence a fully-free loop would get, versus ~93% for
`time.sleep`. It is dramatically better than a bare `await` (1 tick — fully
frozen), which is what the pre-fix incidents actually measured (210s/207s/129s
stalls in boot_service.py:404-412 and startup_tasks.py:173-180 were **bare
awaits**, not `to_thread` failures) — but it is not the "fully insulated"
picture the test's `time.sleep` stand-in paints. The test's own threshold
(`ticks >= 5`) is loose enough that even the throttled real-pipeline numbers
above would still pass it — so simply swapping the stand-in for something
GIL-holding, without also tightening the assertion, would still not
distinguish "fully free" from "halved cadence."

**Conclusion:** the loop-responsiveness suite tests that `_off_the_loop`'s
plumbing exists and is wired to every call site (a real and valuable
regression guard against the *bare-await* defect it was written for). It does
**not** test, and cannot currently detect, a regression in the *row-count
bound* that this task is about — that protection currently exists nowhere in
the test suite.

---

## 2. The 90-day window's sufficiency

**Computation:** `compute_sector_performance` (sector_aggregator.py:123-253).
Needs 30 rows per ticker for `pct_change(periods=30)` (line 153) and
`rolling(30).mean()` (line 156-158); the code comment (lines 109-111) states
this directly: "It needs 30 ROWS per ticker... ~42 calendar days is the
floor; 90 leaves room for holiday clusters and gaps." A ticker short of 30
rows does not crash the function — `return_30d`/`vol_sma_30` come back `NaN`
for that ticker's latest row, `pd.notna()` guards turn the sector-level
aggregate to `0.0` for that contribution, and `.sum()`/`.mean()` skip NaNs —
so insufficiency degrades one ticker's contribution silently rather than
raising.

**Real data** (queried read-only against `trading_bot.price_history` /
`ticker_metadata`, same universe filter the join uses —
`{"sp500": True, "sector": {"$ne": None}}`, 509 tickers, cutoff = now − 90
days):

- Distinct trading dates per ticker in the window: min **1**, median **62**,
  mean 61.7, max **62**. 507 of 509 tickers have the full ~62 trading days a
  90-calendar-day window should contain (no unusual market closure in this
  window — the max/median match the ~63-trading-day expectation for a
  calendar quarter).
- **2 of 509** tickers (`BK`, `SATS`) fall below the 30-row requirement — both
  have **exactly 1 row in price_history's entire history** (a single row
  dated 2026-07-17, confirmed by reading their full history with no window
  filter). This is a pre-existing data-collection gap, not a windowing
  artifact: an unbounded query returns the same single row for these two
  names. Reverting the bound would not fix them.
- Separately: **45 of 509** tickers carry duplicate `(ticker, date)` rows
  inside the 90-day window because two vendors (`yfinance` and `polygon`)
  both have rows for the same date (2,148 duplicate ticker-date pairs
  total, e.g. `LULU`/`AVGO`/`C`/`GOOG`/`NVDA` each show 124 rows in the
  window — exactly double the 62 distinct trading dates). Unlike
  `get_sector_stocks`'s `_prices_on()` (sector_aggregator.py:70-90), which
  explicitly calls `keep_dominant_source()` to pin one vendor per ticker,
  `compute_sector_performance`'s join (lines 129-138) does not deduplicate by
  vendor. `groupby("ticker")["close"].pct_change(periods=30)` on such a
  ticker computes a 30-**row** delta that spans roughly 15 real trading days,
  not 30 — the field is silently mislabeled for ~9% of the universe. This is
  a correctness defect independent of the window-size fix (it existed
  unbounded too) but is directly relevant to "does the 30-period calculation
  get what it needs" since it means the *effective* period count, not just
  the *available* row count, is wrong for those tickers.

**Verdict:** the 90-day bound is sufficient by a comfortable margin (median
62 vs. 30 required, ~2x) for the universe as it is actually collected. The
one failure mode found is a genuine data gap unrelated to the bound
(BK/SATS), and a separate multi-vendor deduplication bug that predates and is
orthogonal to this fix.

---

## 3. Backfill vs. live path

`SECTOR_PERF_WINDOW_DAYS` (sector_aggregator.py:120) is referenced **only**
inside `compute_sector_performance` (line 127) — confirmed by repo-wide grep;
no other module imports or reads it.

`backfill_sector_performance` (sector_aggregator.py:256-329) issues a
structurally different query: `{"source": "yfinance"}` (line 271) with **no
date key at all** — full history, gated only by an early-return guard ("if
sector_performance already has >1 distinct date, skip", lines 264-267) that
runs once per fresh deployment.

`mongo_query.join_rows()` (`app/db/mongo_query.py:352-391`), the shared
helper both functions call, takes no default date-bound parameter, no env
flag, and no window argument of any kind — the date filter lives entirely in
each caller's own `left_query` dict literal. There is no shared constant, no
default argument, and no environment flag through which the 90-day bound
could leak into the backfill path by accident.

**`backfill_isolated: true`.**

---

## 4. Quiet-boot health / the vllm-shim metrics poller

`PrismLLMShim.start_metrics_polling()` (`app/services/prism_agent_caller.py:1226-1232`)
is called from exactly four places:

- `chat()` — line 1044
- `chat_with_tools()` — line 1098
- `stream_prism_agent()` — line 1203
- `AdaptiveConcurrencyController._read_endpoints()` (`adaptive_concurrency.py:144-145`),
  itself reachable only through `concurrency_controller.track()`, which is
  only called from `chat()`/`chat_with_tools()` (prism_agent_caller.py:1057,
  1110) and `app/v3/orchestrator.py:3104` (inside a running cycle's agent
  turn).

None of `BootService.startup()`, `_start_background_tasks()`, or any of the
`startup_*` functions in `app/services/startup_tasks.py` call `llm.chat`,
`llm.chat_with_tools`, `llm.stream_prism_agent`, or
`start_metrics_polling()` directly. So the poller genuinely does not start
until a real prism/LLM call happens — confirmed independently of, and
consistent with, `docs/ETF_TIER_GATE_2026-09-06.md:265-273`: "0 `/metrics`
lines on the new boot until a cycle started... On a quiet boot the compute's
own wall time is the only bound on loop starvation."

**What does health/readiness report in that window?** `/health`
(`cycle_main.py:328-331`) is a hardcoded literal:

```python
@app.get("/health")
def health():
    return {"status": "ok", "service": "trading-service", "version": "v3"}
```

It reads nothing — not DB connectivity, not `sector_performance` freshness,
not vLLM endpoint state, not whether the poller is running. A quiet boot with
no cycle ever run reports **exactly** the same `/health` payload as a fully
warm, fully-populated system. It is not "degraded" — it is indistinguishable
from healthy, by construction, because the endpoint does not consult
anything that could be degraded. `/node-health` (`node_health_router.py`) is
the one endpoint that does something during a quiet boot: it force-syncs
each endpoint's model via `llm._sync_endpoint_model(ep, force=False)`
(line ~35) directly, independent of the poller — so `model`/`is_online` self-
heal on request, but `requests_running`/`requests_waiting` (populated only by
`_poll_all_metrics`, `prism_agent_caller.py:1236-1259`) stay at their `0`
default until the first real call, indistinguishable from "genuinely idle."

---

## 5. Readiness

Startup does **not** withhold readiness. `cycle_main.py:453-455`:

```python
worker_task = asyncio.create_task(run_worker(tickers=tickers, shutdown_event=shutdown))
health_task = asyncio.create_task(start_health_server(shutdown))
await asyncio.gather(worker_task, health_task)
```

`run_worker()` (`cycle_main.py:260-310`) calls `await BootService.startup()`
(the full sequence: DB connection & schema, vector indexes, app-state reset,
crash recovery scan, scheduler start, embedding warmup, V3 agent
registration, then the fire-and-forget background refreshes including
`compute_sector_performance`) and `start_health_server()` (`cycle_main.py:311-388`,
which binds uvicorn on `:8080` and mounts every router) run as two
independent, concurrently-scheduled tasks with **no ordering barrier between
them**. `/health` can and does answer "ok" while boot stages are still
running, or have permanently failed — every stage past the three `required=True`
ones (`_run_stage`, `boot_service.py:196-213`) swallows its exception into a
`logger.warning` and continues in "degraded mode," invisibly to `/health`.

**What can a caller get that is wrong, not merely absent?** `PipelineStateDB.get_state()`
(`app/services/pipeline_state.py:231-283`) reads the `pipeline_state`
singleton straight from Mongo on every `/status` call, with no cache. The
stage that corrects a crashed container's stale `running`/`blocked`/`starting`
status to `error` — `BootService._reset_app_state()` (`boot_service.py:216-234`)
— runs inside the same concurrently-scheduled `BootService.startup()`. In the
**current** code this resolves before the health server can accept
connections in practice, because none of the boot stages ahead of
`_reset_app_state` (`_check_sdk_capabilities`, `_init_database`,
`_init_vector_indices`) contain an `await`, so Python's cooperative scheduler
cannot interleave the health-server task in ahead of them — `run_worker()`'s
task runs to its first genuine suspension point (`await
session_manager.startup()`, later in the sequence) before yielding. That
ordering is accidental, not enforced by any barrier or dependency check:
nothing stops a future edit from adding an `await` earlier in the sequence
(e.g. an async DB ping) and reopening the window in which a caller polling
`/status` immediately after a crash-restart is told a cycle is **running**
when the container that owned it is dead — an actively wrong claim, not an
absent one.

More generally: sector/market endpoints served the moment `/health` starts
answering "ok" read whatever is already in `sector_performance` — the
previous day's (or previous boot's) numbers — with no freshness flag beyond
the row's own `date` field, until the background `compute_sector_performance`
task (fire-and-forget, no readiness coupling) finishes.

---

## Summary

| # | Question | Finding |
|---|---|---|
| 1 | Loop test faithful? | No — `time.sleep` stand-in releases the GIL; the one test on the real call path monkeypatches the real analytics away. Cannot catch a revert of the 90-day bound. Measured separately: the real pandas pipeline via `to_thread` sustains only ~39% of full heartbeat cadence vs ~93% for `time.sleep`. |
| 2 | 90-day window sufficient? | Yes, with wide margin (median 62 rows vs 30 required). 2/509 tickers (BK, SATS) fail, but from a total-history data gap unrelated to the bound. 45/509 tickers have unrelated multi-vendor duplicate rows that distort the 30-period return. |
| 3 | Backfill isolated? | Yes — structurally separate query, no shared bound, no default arg, no env flag. |
| 4 | Quiet-boot poller | Confirmed: starts only on the first real LLM/agent call. `/health` checks nothing, so a quiet boot is indistinguishable from a broken box by that endpoint either way. |
| 5 | Readiness withheld? | No — health server and boot sequence run concurrently with no barrier; `/health` is a hardcoded "ok". One concrete "wrong not absent" exposure: `/status` could show a stale running/blocked/starting cycle state from a crashed prior instance if `_reset_app_state`'s current accidental head-start ever disappears. |
