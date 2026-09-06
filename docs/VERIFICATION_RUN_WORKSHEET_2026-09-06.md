# Verification-run worksheet — one fully observed cycle

## Baseline: three cycles graded on the CURRENT deployment

`scripts/collect_cycle_bundle.py <cycle_id>` collects the bundle and grades it.
Run against every cycle that completed while this pass was being written, all
of them on the code that is deployed today (none of this branch):

| cycle | finished (UTC) | verdicts |
|---|---|---|
| cycle-v3-1788674782 | 06:46 | 9 PASS, 1 FAIL, 2 NOT EXERCISED |
| cycle-v3-1788682529 | 09:04 | 9 PASS, 1 FAIL, 2 NOT EXERCISED |
| cycle-v3-1788699598 | 13:40 | 9 PASS, 1 FAIL, 2 NOT EXERCISED |

The same three verdicts every time, which is the useful part:

- **PASS** — reached `done`; summary, `trade_results`, `trade_fills` and the
  `pipeline_events` timeline agree; exactly one AUTORESEARCH command and one
  `done` report per cycle; `recovery_stats` on the right cycle and honestly
  zero; no zero-cost non-success agent row; no 30-minute agent.
- **FAIL** — denied `think`: 5, 4 and 3 calls. Fixed on this branch (75e4e27).
- **NOT EXERCISED** — `recent_events` empty (no failure occurred, so the ISO
  repair is unproven) and no fund among the selected tickers.

Two of the three were Watch Desk trips on the explicit-ticker path, so the
metadata gate never ran: **EXLS completed a full cycle on 09-06 and still has
no `ticker_metadata` row**, which is the hole e9711979 closes.

## Decisions taken (operator, 2026-09-06)

1. **`think` — restored.** Double-checked before shipping: a denied call costs
   0.98 turns (mean 1.14 think calls per run), and an allowed one cost 1.28
   against a mean of 1.26. The turn is spent either way, so the deny bought
   nothing. 75e4e27.
2. **ETF — gate the explicit-ticker path.** e9711979. Makes criterion 8
   deterministic: an operator-forced fund is now classified before analysis.
3. **UI — fix the server heartbeat.** a7b85bc. Measured, not assumed: all four
   >300 s gaps in the sampled cycle contained tool calls, so the tool-result
   path covers them; worst gap 522 s → 323 s.

## Running the observed cycle

Explicit tickers, because that path now runs the metadata gate and makes the
ETF branch deterministic:

    {"tickers": ["JEPQ", "AMD"], "collect": true, "analyze": true,
     "trade": true, "dynamic_selection_mode": false}

`JEPQ` has **no `ticker_metadata` row**, so it exercises the persist branch
(vendor lookup → `asset_class: etf` → `market_cap_tier: etf`) rather than the
already-correct branch every other fund would take. `AMD` is a tiered company
in the same run, which proves the gate did not simply disable selection.

Budget roughly 45–60 minutes per ticker: cycle-v3-1788682529 took 49 minutes
for one.


Written 2026-09-06, BEFORE the run. Every criterion below states where its
evidence is read from, what PASS looks like, and what FAIL looks like. A
criterion with no stated failure signature is not a criterion.

Rule carried over from the last pass: an opportunistic criterion whose
condition never occurs is recorded NOT EXERCISED, never PASS.

## 0. What the audit changed about these criteria (read first)

Four of the requested acceptance criteria are **not measurable as written**.
Each is backed by a query run on 2026-09-06 against `trading_bot`.

| # | As requested | What the store/code says | Consequence |
|---|---|---|---|
| A | "exactly one corresponding report ... containing serializable `recovery_stats`" | Of **244** stored reports carrying `recovery_stats`, **0** have a non-empty `recent_events`. The only cycle that ever produced events (`cycle-v3-1788660665`) is the one whose report is `status=error`, `recovery_stats=None` — the incident itself. | A clean cycle CANNOT prove the ISO repair: it writes `recent_events: []`. Proof must come from the writer boundary (`app/autoresearch/core.py:272`) driven with datetime-bearing rows, or from a cycle with an injected recoverable failure. |
| B | "exactly one post-cycle AUTORESEARCH command ... exactly one report" | The enqueue is **not idempotent per cycle**. `cycle-v3-1788486930` has **4** commands and **4** `done` reports (2026-09-04 03:47, 05:00, 05:07, 05:10). `cycle-v3-1784554200` has 2 and 2. | "Exactly one" is a property the code does not have today. The cycle can only prove "exactly one enqueued BY THE DONE TAIL"; a second one from the Run Audit button is still accepted and still produces a second `done` report with no version marker. |
| C | "the ETF is denied the company-tier path deterministically" | The tier gate has exactly **one** caller: `app/services/pipeline_service.py:2200`, inside the gatekeeper branch, guarded by `len(selected) > 1`. An explicit-ticker cycle prints "discovery & gatekeeper bypassed" (`pipeline_service.py:1399`) and never reaches it. **Measured over 21 days: 143 of 194 cycles (74%) took the bypass; only 17 ran the gatekeeper at all.** All **85** funds in `ticker_metadata` already carry `market_cap_tier: "etf"`, so the persist branch (`ticker_meta.py:149`) cannot fire again for them. | Forcing an ETF via an explicit ticker list exercises NOTHING. Selection by the gatekeeper is an LLM choice, so it is not deterministic either. And the gate protects a path that ~9% of cycles take: the last completed cycle (`cycle-v3-1788674782`, a Watch Desk trip on ET) never called it. See §3. |
| D | "the ETF is explicitly excluded" | There is no exclusion. `ETF_TIER = "etf"` (`ticker_meta.py:19`) only keeps a fund out of the **mega-cap cap** (`pipeline_service.py:2205` tests `tier == "mega"`) and out of `tier_unknown`. A selected ETF is analysed and traded like any company. | The criterion must be restated as "classified, and visibly not mistaken for a company tier", or an exclusion has to be written first. It is a product decision, not a bug. |

## 1. Cycle-level criteria

**Where the timeline lives.** `cycle_audit_log` carries ONLY warnings and
errors — for `cycle-v3-1788674782` all 46 rows were severity `warning` or
`critical`, and not one was a start / decision / terminal event. The lifecycle
timeline the bundle needs is `pipeline_events`
(`app/services/pipeline_state.py:115`), 65 rows for the same cycle, carrying
`cycle_trigger`, `explicit_tickers` / `GATEKEEPER_SELECTED`, per-ticker
`v3_start_*` … `v3_done_*`, `v3_policy_*` and `trade_executed_*`. A bundle
built from the audit log alone has no timeline in it. Live intra-cycle
progress (`pipeline_state.progress`) is a SINGLETON and is overwritten on every
update — if it is not sampled while the cycle runs, it is gone.

| # | criterion | evidence source | PASS | FAIL |
|---|---|---|---|---|
| 0 | the timeline exists at all | `pipeline_events` for the cycle_id (NOT `cycle_audit_log`) | per-ticker `v3_start_*` / `v3_done_*` / `trade_executed_*` steps, plus `cycle_trigger` and either `explicit_tickers` or `GATEKEEPER_SELECTED` | no rows — the run is then unreconstructable after the fact |
| 1 | cycle reaches `done` normally | `cycle_run_summaries.status` for the cycle_id; `pipeline_state.status` | `status == "done"`, `finished_at` set, `partial` absent/false | `stopped`/`error`, or no summary row |
| 2 | one book, agreeing everywhere | `cycle_run_summaries` (buy/sell/hold_count, trade_executed, tickers_final) vs `trade_results` (per-ticker action) vs `trade_fills` (fill rows) vs `cycle_audit_log` timeline | counts equal across all four; `counts_source == "store"` | any disagreement; `trade_executed` 0 with a fill present |
| 3 | exactly one AUTORESEARCH enqueue from the done tail | `system_commands` where `command_type=AUTORESEARCH` and `payload.cycle_id == <id>` | exactly 1 row, `status` completed, `created_at` within seconds of `finished_at` | 0 rows (tail skipped), or >1 (see §0 B) |
| 4 | exactly one report, terminal, non-error | `autoresearch_reports` where `cycle_id == <id>` | 1 row, `status == "done"`, `overall_score` present | `status == "error"`, or >1 row |
| 5 | `recovery_stats` is serializable and truthful | `autoresearch_reports.recovery_stats` (stored as a JSON **string**) | parses; `cycle_id` equals this cycle; `total_failures` equals the classified rows in `cycle_audit_log` | `cycle_id ""`; `total_failures` 0 while the log holds a CRASHED/stall row |
| 6 | `recent_events[].at` is an ISO-8601 string | same field | every `at` is a `str` that `datetime.fromisoformat` parses | any `at` is an object, or the block is absent — **and** an empty list is NOT a pass, it is NOT EXERCISED (§0 A) |
| 7 | UI last-update advances, and amber means quiet, not dead | client control bar during the run + `cycle_audit_log` event gaps | the age ticks each second, names the running agent, goes amber only after a real 5-min event gap, and recovers when the next event lands | frozen text; amber while events are arriving; a healthy 200-900 s GLM turn shown as a dead pipeline |
| 8 | ETF classified, not mistaken for a company | `ticker_metadata` row for the fund (`asset_class`, `market_cap_tier`); `GATEKEEPER_SELECTED.data.tier_unknown`; log `[TickerMeta] tagged N fund(s)` | `market_cap_tier == "etf"`, fund absent from `tier_unknown`, mega-cap cap did not drop it | a company tier on a fund; fund in `tier_unknown`; silent disappearance with no row |

## 2. Per-agent criteria (these prove the branch fixes)

| # | criterion | evidence | PASS | FAIL |
|---|---|---|---|---|
| 9 | ≤1 slow-run notice per (agent, ticker), at `warning` | container log; `agent_audit_log` severity | one line per run at most, containing "took too much time" and the last tool name | repeats per tool result; severity `error` |
| 10 | `[ToolDeadline]` names tools that really hit the bridge deadline | container log | a row for each failed call ≥ 49.5 s, naming the tool | a 50 s failure with no row |
| 11 | per-box cap holds | prism `requests` ledger; `v3_agent_telemetry` | 0 stall rows; no `elapsed_ms ≈ 1,800,000`; never more than 2 of our GLM requests in flight on a box | any stall row; a 30-minute agent |
| 12 | a retry that cannot fit is refused | `v3_agent_telemetry.failure_reason` | `RETRY_BUDGET_EXHAUSTED` with `cost_partial` true and non-zero `loops_used` where tools ran | a restart begun with < 600 s left; a clean-zero cost row |
| 13 | denied `think` no longer burns turns | `agent_tool_telemetry` for the cycle | 0 rows with `tool_name` ending `think` and `error_message == "POLICY_DENIED"` | any such row (today: 68.9% of runs have ≥1 — see §4) |

## 3. Making the ETF branch real (choose one before the run)

1. **Gatekeeper path, non-deterministic.** Ensure a fund is in the screener pool
   and hope the LLM picks it. Records NOT EXERCISED when it does not. Cheap,
   proves the real path, may prove nothing.
2. **Give the gate a second caller.** Call `ensure_ticker_metadata` on the
   explicit-ticker path too (`pipeline_service.py` after line 1399), so an
   operator-forced ETF is tiered exactly as a gatekeeper-selected one is. This
   is a one-line behaviour change with its own red-first test, and it makes the
   criterion deterministic. It also closes a real hole: today an explicit-ticker
   cycle runs the mega-cap cap and the diversity cap on names with no tier.
3. **New fund, cold.** Pick a fund with NO `ticker_metadata` row so the persist
   branch fires. Requires a fund outside the 85 already tagged.

Recommendation: (2), then (1) as the live proof.

## 4. Findings this audit produced (not previously recorded)

- **The `think` denial costs what it was meant to save.** `think` was added to
  `_V3_DENIED_TOOLS` on 2026-09-02 (51892a90) *to save turns*, and prompt rule 7
  was updated to name it. Measured since: **301 denied calls across 261 agent
  runs — 68.9% of all agent runs that made any tool call** (379 in the window).
  30 runs spent 2 turns on it, 2 runs spent 4. A denial returns `POLICY_DENIED`
  in ~1 ms, but the *turn* is already spent, and turn budgets are 4-6. Before
  the deny, `think` ran at ~100% success (2,699 calls). The deny converted a
  scratchpad turn into a wasted turn; it did not remove the turn.
- **`recovery_stats.recent_events` has never been written non-empty** (0 of 244).
- **AUTORESEARCH is not idempotent per cycle** (4 reports for one cycle).
- **The claim is not atomic.** `eval_worker.poll_system_commands`
  (`app/autoresearch/eval_worker.py:105-110`) does `find_row(status=pending)`
  then `update_docs(status=running)`, while `mongo_store.find_one_and_update`
  (`app/db/mongo_store.py:464`) exists and documents itself as the claim
  primitive for exactly these two queues.
- **16 statusless command rows** sit in `system_commands`, all written
  2026-08-18, with no `status` and no `created_at`: invisible to the poller AND
  to any "stuck command" query. Producer-side the class is closed — only
  `pipeline_service.py:2876` writes that collection now — so this is inert
  residue, not a live defect.
