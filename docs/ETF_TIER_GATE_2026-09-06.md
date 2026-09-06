# An ETF is not a micro-cap company, and the row always knew it — 2026-09-06

Branch `fix/etf-tier`, commit `cc52d04` on top of `138b713`. Found on the
verification cycle `cycle-v3-1788660665`, the first live run of `e592a26`
(`ensure_ticker_metadata`). Status at time of writing: tests green, NOT yet
merged or deployed — the verification cycle is live and a trading-service deploy
kills a live cycle.

## What the cycle showed

The gatekeeper selected SCHD, ABT, NBIS. Check #12 of the pre-written
expectations ("every selected ticker has a `market_cap_tier` after selection")
failed on SCHD. The container log:

    19:17:58 [TickerMeta] SCHD: no market cap on file — left untiered
    19:17:58 [PipelineService] Mega-cap cap ran blind on 1 selected name(s)
             with no market_cap_tier: ['SCHD']

Both sentences are false about the same document. SCHD's `ticker_metadata` row:

    {ticker: SCHD, asset_class: "etf", market_cap: 112337240064, market_cap_tier: null}

`ensure_ticker_metadata` projected `ticker` + `market_cap_tier` and nothing
else, so it could not see the cap or the asset class, called yfinance, got
nothing an ETF reports as `marketCap`, and fell open exactly as designed for
"a symbol with no market cap at all". The design premise was false for 38 of
the 39 rows it would ever meet.

## Census (1,049 `ticker_metadata` rows, measured before any change)

| population | count | finding |
|---|---|---|
| untiered rows | 39 | 38 are `asset_class: etf` with a cap on the row; 1 is BK |
| BK | 1 | dead symbol (BNY Mellon moved to `BNY`); yfinance 404 — the only true fail-open |
| `asset_class: etf` rows | 85 | 38 untiered + 47 tiered |
| tiered ETFs | 47 | **all 47 say `micro`** — QQQM $104B, JEPI $46B, XLV $43B |
| all tiered rows with a cap | 795 | 31 disagree with `tier_for_market_cap(own cap)`: 28 ETFs + 3 boundary stocks (MCD $195B/mega, MRVL $211B/large, DPZ $9.96B/large) |

`tier_for_market_cap` cannot return `micro` for a $104B cap, so the 47 labels
did not come from the cap on the row. The only writer of `"micro"` in the tree
is that function; the label disagrees with its own document.

## Decision (operator, asked 2026-09-06)

Three options were put to the operator: (a) a tier of the ETF's own, (b) derive
the company tier from the stored cap (makes SPY/VTI/VOO/QQQ/IVV/BND/VUG/VTV
"mega" and subject to the one-mega-cap-per-cycle cap), (c) fix only the false
log line. **Chosen: (a).** The mega-cap cap stays a statement about
single-company concentration, which is what it was built for.

## What shipped (`cc52d04`)

* `app/services/ticker_meta.py`: `ETF_TIER = "etf"`, `ETF_ASSET_CLASSES`.
  `ensure_ticker_metadata` now projects `asset_class` + `market_cap`; tags a
  fund from its own row with **no vendor call**; **corrects** a company tier
  sitting on a fund (a category fix from the row itself — the "existing tier
  always wins" guarantee is unchanged for companies); when the vendor has
  nothing, uses the cap already on the row before giving up, so "no market cap
  on file" is now only logged when true. `tier_for_market_cap` untouched.
* `scripts/backfill_market_cap_tier.py`: a fund pass, first, through
  `ensure_ticker_metadata` (one authority), funds excluded from the vendor pass.
  Dry-run against production: **85 funds → etf, 1 company target (BK, no cap,
  left untiered)**.
* `tests/unit/test_etf_tier_gate.py`: 21 tests, every fixture a verbatim
  production row (SCHD, QQQM, BK, ABT).

## Proof

* Red first: the symbol did not exist (ImportError on collection).
* Sabotage, one property at a time, after green: drop `asset_class` from the
  projection → 1 fails; `ETF_TIER = "micro"` → 4 fail; remove the stored-cap
  fallback → 1 fails; let an existing tier block the correction → 1 fails;
  make the tier falsy → 1 fails; `ETF_TIER = "mega"` → the cap-wiring test
  fails. Each mutation killed by a distinct test.
* The cap-wiring test DERIVES the mega-cap comparison literal from
  `pipeline_service` by AST rather than transcribing `"mega"`.
* Prior `test_ticker_metadata_gate.py` (16 tests) still green. Worktree unit
  suite: **6,392 passed / 0 failed / 85 skipped**.
* Consumers of `market_cap_tier` swept: the cap (`== "mega"`), ontology_builder
  (label pass-through), portfolio (pass-through). None enumerates the buckets;
  a sixth value breaks nothing. The sp500 loader iterates `SP500_TICKERS` only
  and cannot touch an ETF row.

## Still to do (in order)

1. Cycle `cycle-v3-1788660665` reaches terminal → remaining checks.
2. Backup taken: `~/db-backups/ticker_metadata-etf-tier-2026-09-06/`
   (full 1,049 rows + the 85 ETF rows before). Then run
   `scripts/backfill_market_cap_tier.py` (no flags) once — expect 85 tagged.
3. Merge `fix/etf-tier`, deploy trading-service, confirm the next
   GATEKEEPER_SELECTED event has `tier_unknown: []` when a fund is selected.

## Open item found in the same pass — check #6, the loop pin residual

`b873016` moved the sector compute into `asyncio.to_thread`. On this boot the
compute ran 160 s (19:26:12 → 19:28:51) and the loop stayed alive for most of
it, but the container logged **nothing for 53 s** (19:26:56 → 19:27:49) and
the 5 s poller missed 11 ticks. Zero bridge timeouts and zero failed tool rows
this cycle — nothing was in flight — but the mechanism is live.

Measured mechanism (`bench` on this box, `to_thread`, heartbeat on the loop):

| work shape in the worker thread | loop max gap |
|---|---|
| Python bytecode spin, 1.0 s | 0.02 s (interpreter switches every 5 ms) |
| `sum(range(60M))`, one C call | **0.71 s = the whole call** |
| `sorted(20M)` | 1.48 s |
| `pd.DataFrame(3M tuples)` | 0.66 s |
| groupby pct_change + rolling | 0.02 s |

A thread frees the loop only for work that re-enters the bytecode loop or
releases the GIL. One long C-level call holds it for its whole duration. The
existing guard `test_boot_compute_does_not_block_the_loop.py` uses
`time.sleep` as its stand-in, which releases the GIL — it cannot see this class.

The compute itself reads `price_history` with **no date bound**: 4,365,690 rows
for 509 tickers, joined in Python (`join_rows` is deliberately not `$lookup`),
to use only the latest date with 30-period lookbacks. A 90-day bound is 33,355
rows. Operator instruction: measure first, then fix.

### Bench result (real function, real store, the one write patched to a recorder)

| strategy | wall | loop max gap | gaps > 1 s | output |
|---|---|---|---|---|
| thread + full (today's code) | 179.7 s | 3.49 s | 5 | 11 sectors |
| thread + 90-day bound | **1.3 s** | **0.08 s** | 0 | **identical** to full, all 11 |
| process + full | 163.3 s | 0.79 s | 0 | 11 sectors |
| process + 90-day bound | 2.4 s | 0.47 s | 0 | identical to full |

Bench box: WSL, Mongo over LAN — the 53 s production gap did not reproduce here
(3.49 s max); the NAS container is slower and its read is the same 4.37M rows.
A process pool frees the loop and keeps the 163 s / 9 GB read. The bound removes
the read, and the result is identical sector by sector.

### Shipped (`2fd8895`): `SECTOR_PERF_WINDOW_DAYS = 90`

`compute_sector_performance` now reads `price_history` with
`{"date": {"$gte": now - 90d}}`. 30 rows ≈ 42 calendar days; 90 leaves room
for holiday clusters. `backfill_sector_performance` keeps its full read on
purpose (it writes every historical date, and skips itself when history exists).
Tests (`test_sector_compute_reads_a_bounded_window.py`): the query carries a
lower bound; the bound is 60–200 days; bounded output == unbounded output on a
400-day synthetic panel ending today. Red first on the empty filter. Sabotage:
30-day bound → margin test fails; bound removed → both bound tests fail; a
20-day bound that cuts INTO the lookback → the equality test fails, so it is not
a tautology. The fixture's first draft ended in February and the bound cut
everything — it now ends today.

### Still open

`test_boot_compute_does_not_block_the_loop.py` uses `time.sleep` as its stand-in.
That releases the GIL, so the guard cannot see the C-level class in the table
above and was green through the 53 s stall. With a 1.3 s compute the thread is
adequate; if the compute ever grows again, the guard will not say so. A faithful
stand-in is a single C-level call (`sum(range(N))`), which makes the thread
version fail — i.e. that guard can only be made honest together with a move to a
process, or by asserting the compute's wall time instead.


## Check #9 — a TIMED_OUT row said the run cost nothing (found 03:03 UTC, same cycle)

The ABT fundamental analyst ran the full agent timeout. Its telemetry row,
verbatim:

    outcome=TIMED_OUT  loops_used=0  prompt_tokens=0  token_usage=0
    cost_partial=False  elapsed_ms=1800068  failure_reason=TIMEOUT

`agent_tool_telemetry` holds **18 tool calls** for that agent on that ticker
in that cycle, the last at 02:45:12 — eighteen minutes before the timeout at
03:03:45 (the agent was waiting on the model, no stall classification fired
for it; the SCHD bull agent's stall in the same window WAS classified transient
and retried to SUCCESS — check #14 PASS).

`6586589` (B2) fixed "the crash row said it cost nothing" for the CRASH path by
attaching `partial_cost` to the escaping exception. The timeout path is a
sibling seam that fix could not reach: `asyncio.wait_for` cancels the run —
`run_agent` attaches the cost to the CancelledError — and raises its OWN
`TimeoutError` to the runner, which nothing decorated. The CANCELLED (stop
requested) path had the same hardcoded zeros. A patch fitted to one seam parks
the residual on the others.

**Fix:** `run_agent(cost_sink=...)` — the RUNNER owns a dict and passes it in;
`run_agent` uses THAT dict as its accumulator (not a copy), so timeout, cancel
and crash all read the same numbers from the same place. The artifact-repair
call passes the same sink so its spend joins the run's. Crash path keeps the
exception attribute as a fallback for a run_agent that predates the sink.

Tests (`test_timed_out_row_carries_its_cost.py`, 5): TIMED_OUT, CANCELLED and
AGENT_ERROR rows each carry `cost_partial=True` and the spent tokens/loops;
`run_agent` accepts `cost_sink`; the accumulator ALIASES the sink (AST). Tool
count and elapsed are the production row; the token figure is synthetic
because the row that motivated the test recorded none. Red first: 5 of 5.
Sabotage, each caught: TIMED_OUT handler back to zeros; base_agent copies the
sink; CANCELLED loses `cost_partial`; runner stops passing the sink; crash path
ignores the sink. Neighbouring runner tests (81) green.

Also exercised this cycle: **#11 PASS** — the phase-abort page landed as one
`fund_alerts` row (`v3_phase_abort`, warning, ABT), 0 "Failed to record fund
alert" rows.


## Check #4 — the first live run of recovery_stats took down the report (terminal, 03:54 UTC)

The autoresearch report for this cycle:

    status=error  phase=error
    error="Object of type datetime is not JSON serializable"

`core.py:272` writes `'recovery_stats': json.dumps(recovery)`; `56356ec` (B3)
had just made `_audit_recovery` return `recent_events` whose `"at"` is the
cycle_audit_log row's raw BSON datetime. Three minutes of audit work, then the
whole report — data quality, decision quality, LLM scores — went to `error`.
B3's tests built rows WITH datetimes and asserted counts, types and ordering,
but never handed the dict to the writer's serializer. A test that proves the
shape of a result must also prove it survives the one operation every consumer
performs on it.

**Fix (`ed62fa1`, branch `fix/autoresearch-json`):** convert `at` to ISO AFTER
the sort. Tests (`test_recovery_stats_are_json_serializable.py`, 3): the stats
survive `json.dumps`; events stay time-ordered after conversion; a row with no
timestamp still serializes and sorts last. Red first 2 of 3; conversion removed
→ red; the prior 18 recovery tests still pass. The report for
`cycle-v3-1788660665` itself is lost (it errored before scoring); the next cycle
is the first that can show `recovery_stats.total_failures` non-zero — this one
would have counted 2 (the SCHD stall, the ABT timeout).

## Terminal verdict table — cycle-v3-1788660665

27 agents, 26 SUCCESS, 1 TIMED_OUT (ABT fundamental). SCHD BUY 83.512 @ $34.81
(conf 72), NBIS HOLD (conf 63), ABT aborted. 83 tool calls. 40 error rows: 31
slow-tool, 3 agent-timeout, 3 prism-disconnect, 2 prism-500, 1 prism-stall.

| # | check | verdict | evidence |
|---|---|---|---|
| 1 | signal_weights_source | PASS | 2 rows, both `model`, 4 keys, sum 1 |
| 2 | ranked_pool + selected_ranks | PASS | 40 rows desc, ranks SCHD 2 / ABT 15 / NBIS 16, pool 126 |
| 3 | deep retrieval iff judge < 60 | PASS | SCHD judge 58 → start + done events, no others |
| 4 | recovery_stats on the report | **FAIL** | report `error`: datetime not serializable — fixed `ed62fa1` |
| 5 | cached_tokens on GLM | PASS by explanation | 22 GLM rows all 0; usage-keys line names `cacheReadInputTokens`, the reader reads it — the value from vllm-2 is 0 |
| 6 | boot refresh does not pin the loop | **FAIL** | 53 s silence in a 160 s compute; 0 bridge timeouts — fixed `2fd8895` |
| 7 | Last update cell | wired, not watched | SSE `updated_at` → `fmtAge` in source; lazy chunk not located on :3030 |
| 8 | stopped-path summary | PASS on the DONE path only | trade_executed 1 == fills 1; the STOPPED path is not exercised — needs a stopped cycle |
| 9 | dead row carries cost | **FAIL** | ABT TIMED_OUT: 18 tool calls, tokens 0, cost_partial false — fixed `6b08c11`. The checker's first version queried AGENT_ERROR only and printed NOT EXERCISED |
| 10 | noise floor | PASS | 0 violations |
| 11 | fund_alerts | PASS | 1 row `v3_phase_abort`/warning/ABT, 0 rejections. Checker's first version printed NOT EXERCISED |
| 12 | metadata gate | **FAIL** | SCHD untiered, cap ran blind — fixed `cc52d04` + 85-row migration. Checker's first version printed PASS on an empty set |
| 13 | language gate | NOT EXERCISED | 0 non-Latin artifacts, 0 firings |
| 14 | stall classification | PASS | 2 stall rows, classified transient, retried, agent SUCCESS. Checker's first version printed NOT EXERCISED |

Four checker verdicts were wrong before the by-hand pass (12 vacuous PASS; 9,
11, 14 false NOT EXERCISED). The instrument was repaired in place; every
verdict above was confirmed by a direct query.


## Live confirmation after deploy (boot 04:09:57 UTC, master `7bbed69b`)

| item | before (boot 02:08, `138b713`) | after (boot 04:09, `7bbed69b`) |
|---|---|---|
| sector compute wall | 160 s (19:26:12 → 19:28:51) | **1.7 s** (21:19:02.49 → 21:19:04.20) |
| longest container silence inside it | 53 s | bounded by the 1.7 s wall |
| rows read | 4,365,690 | ≤ 33,355 (90-day window) |
| `ticker_metadata` funds untiered | 38 | 0 (85 tagged `etf`; BK the only untiered row, a dead symbol) |
| ETFs carrying a company tier | 47 (`micro`) | 0 |

Both deploys verified inside the container: `ETF_TIER`, `SECTOR_PERF_WINDOW_DAYS`,
`cost_sink` in `run_agent`, and the `isoformat` conversion in `_audit_recovery`
all import from the running image. Primary suite at `7bbed69b`: 6,411 passed.

Note on the 5 s vllm-shim metrics poller used as the loop heartbeat in check #6:
it is CYCLE-scoped, not boot-scoped — 0 `/metrics` lines on the new boot until
a cycle started. On a quiet boot the compute's own wall time is the only bound
on loop starvation, which is why the wall time is the number recorded above.

## Follow-up cycle `cycle-v3-1788668370` (04:19 → 05:34 UTC, stopped on purpose)

Gatekeeper selected AVGO (mega) and LULU (large), `tier_unknown: []`. AVGO ran
the full desk (17 agents SUCCESS), the Board said BUY at 72, the fill landed
(2.2973 @ $357.92, $822.25) at 05:34:03, and the watcher sent STOP_CYCLE on
seeing it. LULU's bear agent was mid-turn and was CANCELLED.

| check | verdict | evidence |
|---|---|---|
| 8 (stopped path) | **PASS** | `status=stopped`, `counts_source=store`, `trade_executed=1`, `trade_fills=1`, B/S/H 1/0/0 |
| 12 (live) | PASS, companies only | both selected names tiered, `tier_unknown=[]`; no fund was selected, so the `etf` branch is not yet exercised live |
| 9 (live, CANCELLED path) | fix works, one residual | LULU bear: `cost_partial=True`, `loops_used=2`, 1 tool row, elapsed 549 s — and `prompt_tokens=0`, because the sink's token figure comes from `harness.total_tokens`, which only counts COMPLETED responses; a call cancelled in flight has none. The row is now labelled partial and carries its loops; its token cost for an in-flight prefill is still invisible. Open item: estimate from `sys_prompt_chars + user_prompt_chars` when tokens are 0 and the run died, labelled as an estimate. |
| 6 (poller) | explained | the 5 s `/metrics` poller is started by the first prism agent call: 0 lines on the quiet boot, 1,056 during this cycle |

Check #4 live (the stopped cycle's autoresearch report with `recovery_stats`
serialised) is pending the report's completion.
