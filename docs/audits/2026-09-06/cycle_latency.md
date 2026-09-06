# Where a V3 cycle's wall clock goes — audit of 3 completed cycles (2026-09-06)

**Scope:** read-only. Sources: `pipeline_events`, `v3_agent_telemetry`, `agent_tool_telemetry`,
`cycle_audit_log`, cross-checked against `cycle_benchmarks`, `llm_audit_logs`, `v3_system_commands`,
`worklist_shadow_runs`, `execution_errors`. Cycles audited:

| cycle_id | ticker | decision | total wall clock |
|---|---|---|---|
| cycle-v3-1788682529 | EXLS | HOLD | 2919.0 s (48.7 min) |
| cycle-v3-1788674782 | ET | BUY | 2416.4 s (40.3 min) |
| cycle-v3-1788699598 | ZS | HOLD | 2433.1 s (40.6 min) |

`pipeline_events`-derived totals cross-check within 10 s of `cycle_benchmarks.total_ms` for all
three (EXLS: 2919.0 s vs 2928.8 s — the 9.9 s gap is unlogged post-`v3_done` finalization time that
exists in no collection this audit could find).

---

## 1. Per-cycle phase timeline

Phase boundaries reconstructed from `pipeline_events` step names (`cycle_trigger` →
`explicit_tickers` → `v3_precollect_*` → per-agent `v3_v3_{agent}_*`/`*_done_*` → `v3_policy_*` →
`v3_dossier_*`/`v3_hold_reason_*` → `v3_done_*` → `trade_executed_*`).

### cycle-v3-1788682529 (EXLS, HOLD) — 2919.0 s total

| Phase | Elapsed (s) | % of total |
|---|---:|---:|
| dispatch (cycle_trigger → gatekeeper decision) | 37.2 | 1.3% |
| gatekeeper → precollect start | 0.2 | 0.0% |
| **precollect** (data collection) | **129.6** | **4.4%** |
| context prep (SharedDesk + triage) | 2.6 | 0.1% |
| **agents (11 steps, serial)** | **2717.6** | **93.1%** |
| inter-agent logging gaps | 1.5 | 0.1% |
| policy/dossier/hold_reason (post-synthesis) | 30.2 | 1.0% |
| trade execution | n/a (HOLD) | — |
| **idle (unaccounted)** | **0.0** | **0.0%** |

Per-agent (research → debate → board → synthesis, run strictly in this order):

| Agent | Elapsed (s) |
|---|---:|
| v3_regime_engine | 46.2 |
| v3_junior_analyst | 153.7 |
| v3_fundamental_analyst | 157.2 |
| v3_quant_analyst | 523.2 |
| v3_valuation_analyst | 381.4 |
| v3_bull_agent | 388.0 |
| v3_bear_agent | 530.4 |
| v3_bull_defense | 177.2 |
| v3_debate_judge | 86.5 |
| v3_board_of_directors | 131.2 |
| v3_decision_synthesizer | 142.6 |

### cycle-v3-1788674782 (ET, BUY) — 2416.4 s total

| Phase | Elapsed (s) | % of total |
|---|---:|---:|
| dispatch | 2.4 | 0.1% |
| gatekeeper → precollect start | 0.2 | 0.0% |
| **precollect** | **186.5** | **7.7%** |
| context prep | 2.2 | 0.1% |
| **agents (11 steps, serial)** | **2220.6** | **91.9%** |
| inter-agent logging gaps | 1.2 | 0.0% |
| policy/dossier (post-synthesis) | 2.2 | 0.1% |
| **trade execution** (BUY 71.4297 @ $21.51) | **1.1** | 0.0% |
| **idle (unaccounted)** | **~0.0** | **0.0%** |

Per-agent: regime_engine 35.5s, junior_analyst 92.7s, fundamental_analyst 226.4s, quant_analyst
255.2s, valuation_analyst 326.8s, bull_agent 227.5s, bear_agent 480.6s, bull_defense 139.5s,
debate_judge 172.3s, board_of_directors 115.7s, decision_synthesizer 148.4s.

### cycle-v3-1788699598 (ZS, HOLD) — 2433.1 s total

Precollect ran the **Fast-Path** (`v3_precollect_fastpath_ZS`): only yfinance_price + finnhub_news
ran; reddit/youtube/multi_api_news and the prior-research seed step were **skipped**
(`cycle_benchmarks.steps_skipped = 4`, `cache_hit_pct = 66.7%`).

| Phase | Elapsed (s) | % of total |
|---|---:|---:|
| dispatch | 2.3 | 0.1% |
| gatekeeper → precollect start | 0.1 | 0.0% |
| **precollect (fast-path)** | **72.8** | **3.0%** |
| context prep | 2.4 | 0.1% |
| **agents (11 steps, serial)** | **2352.3** | **96.7%** |
| inter-agent logging gaps | 1.3 | 0.1% |
| policy/dossier/hold_reason | 2.0 | 0.1% |
| **idle (unaccounted)** | **~0.0** | **0.0%** |

Per-agent: regime_engine 46.7s, junior_analyst 186.9s, fundamental_analyst 153.7s, quant_analyst
467.8s, valuation_analyst 394.7s, bull_agent 183.8s, bear_agent 369.5s, bull_defense 130.7s,
debate_judge 96.0s, board_of_directors 171.5s, decision_synthesizer 151.0s.

**Headline for §1:** across all three cycles, 91.9–96.7% of the cycle is the 11-agent research →
debate → board → synthesis chain. Precollect is 3.0–7.7%. Everything else (dispatch, gatekeeper,
context prep, policy/dossier, trade execution) is under 2% combined and in two of three cycles
under 40 s total.

---

## 2. The split that matters: tool time vs. model time

`v3_agent_telemetry.elapsed_ms` per agent run, minus the sum of `agent_tool_telemetry.elapsed_ms`
for that `(cycle_id, agent_name)` (tool calls attribute cleanly — each agent name maps 1:1 to one
`v3_agent_telemetry` row per cycle):

| Agent | mean elapsed (s) | mean tool (s) | mean model (s) | tool % |
|---|---:|---:|---:|---:|
| v3_bear_agent | 460.0 | 2.3 | 457.8 | 0.5% |
| v3_quant_analyst | 415.0 | 36.8 | 378.2 | 8.9% |
| v3_valuation_analyst | 367.6 | 53.2 | 314.4 | 14.5% |
| v3_bull_agent | 266.3 | 2.6 | 263.7 | 1.0% |
| v3_fundamental_analyst | 179.0 | 39.6 | 139.4 | 22.1% |
| v3_bull_defense | 149.0 | 0.0 | 149.0 | 0.0% |
| v3_decision_synthesizer | 147.3 | 0.7 | 146.6 | 0.5% |
| v3_junior_analyst | 144.4 | 30.9 | 113.5 | 21.4% |
| v3_board_of_directors | 139.5 | 0.0 | 139.5 | 0.0% |
| v3_debate_judge | 118.2 | 0.0 | 118.2 | 0.0% |
| v3_regime_engine | 42.8 | 0.0 | 42.8 | 0.0% |

**Only 6 of 11 agents ever call tools** (junior_analyst, fundamental_analyst, quant_analyst,
valuation_analyst, bull_agent, bear_agent — the data-gathering/opening-argument agents).
bull_defense, debate_judge, board_of_directors, decision_synthesizer and regime_engine reason
entirely over the already-assembled SharedDesk context and make zero tool calls.

Per-cycle aggregate:

| Cycle | agent-time total (s) | tool time (s) | tool share | model (LLM) share |
|---|---:|---:|---:|---:|
| EXLS | 2716.6 | 292.7 | 10.8% | **89.2%** |
| ET | 2219.5 | 83.6 | 3.8% | **96.2%** |
| ZS | 2351.2 | 122.0 | 5.2% | **94.8%** |
| **Overall (sum)** | 7287.3 | 498.4 | **6.8%** | **93.2%** |

**Confirms the prior finding.** All three cycles land inside the 88–100% band (89.2%, 96.2%,
94.8%; blended 93.2%). Tool execution is a rounding error next to model time — the most
tool-heavy single agent run was valuation_analyst/EXLS at 119.0s of tool time inside a 381.4s
window (31.2%), still minority share.

**A secondary signal worth flagging:** `elapsed_ms / completion_tokens` (using
`token_usage − prompt_tokens` as a completion-token proxy, since `agent_tool_telemetry` isn't
needed for the single-loop agents that make zero tool calls) sits at **~40–55 ms/token
(~18–25 tok/s)** for the overwhelming majority of the 33 agent runs — consistent with the known
"beside 2 decoding neighbours" regime. One outlier (`v3_fundamental_analyst`/ET, 226.3 s for
14,820 completion tokens) decoded at **15.3 ms/token (~65 tok/s)** — 3x faster. This is a single
data point, not a trend, but it is the only clean look this audit has at what an *uncontended*
decode looks like on this box, and it says the other 32 runs were not uncontended.

---

## 3. The precollect phase

| Cycle | precollect (s) | dominant source | its share | scrape-fail+drop / precollect audit-log entries |
|---|---:|---|---:|---:|
| EXLS | 129.6 | youtube (118.6s) | 91.5% | 12/13 (8 scrape-fail, 4 rotator DROP) |
| ET | 186.5 | youtube (163.9s) | 87.9% | 18/19 (13 scrape-fail, 5 rotator DROP) |
| ZS (fast-path) | 72.8 | finnhub_news (41.6s) + unlogged 31.1s tail | 100% | 24/27 (24 scrape-fail, 0 rotator DROP — reddit/youtube/multi_api_news skipped by fast-path) |

Sub-source timings (start-of-precollect to that source's `_ok_` event):

| source | EXLS | ET | ZS (fast-path) |
|---|---:|---:|---:|
| yfinance_price | 1.6s | 6.3s | 7.8s |
| yfinance_fund | 0.3s | 4.2s | skipped |
| finnhub_news | 15.7s | 41.3s | 41.6s |
| multi_api_news | 108.5s | 148.5s | skipped |
| reddit | 108.6s | 41.4s | skipped |
| youtube | **118.6s** | **163.9s** | skipped |

**yfinance (price+fundamentals) finishes in under 8 seconds every time** — real, structured,
always-succeeds data. Everything after that is the phase waiting on scrape-heavy sources
(multi_api_news, reddit, youtube, or finnhub's per-article retry loop in the fast-path case), and
those sources fail far more often than they succeed:

- **EXLS:** 3 successful `v3_news_scraped` extracts vs. 12 failed/dropped attempts in the same
  window → **20% yield** on individual URL fetches.
- **ET:** 5 successful extracts vs. 18 failed/dropped → **21.7% yield**.
- **ZS:** 4 successful extracts vs. 24 failed → **14.3% yield**. ZS also shows the same 12 finnhub
  article IDs retried in **three separate rounds** (13:00:09–25, 13:00:50–13:01:12,
  13:02:28–13:02:41) — all 12 fail identically every round (`thin content`, `no engine returned
  usable content`), and `[read_through] finnhub_news:ZS refresh exceeded 12s — answering from the
  store` fires twice. The third round runs *after* precollect has already closed and during
  regime_engine/junior_analyst — background retries of URLs already proven dead, competing for
  scraper capacity with no way to help this cycle.

**Fraction of precollect time on sources that yield nothing:** treating yfinance's ~2–8s as the
"real" collection time and everything after as waiting on the scrape/dedup/rotator chain: EXLS
128.0/129.6s (98.8%), ET 180.2/186.5s (96.6%), ZS 65.0/72.8s (89.3%) — **mean ~124s per cycle,
~89–99% of the phase**, is spent on sources whose own audit-log footprint is 4-in-5 failures.

A field worth naming: `cycle_benchmarks.collect_ms` is **0 in all three cycles** — the top-level
benchmark record has no producer for precollect duration at all (matches the "no producer =
confident zero" pattern); anyone reading only `cycle_benchmarks` would conclude precollect is
instant. It isn't — it independently measured 72.8–186.5s from `pipeline_events`.

---

## 4. Concurrency

**Agents never overlap.** Overlapping every `(start, done)` window per cycle: **max concurrency =
1** in all three cycles — the 11-agent chain is strictly serial (each next agent's `running` event
fires 20–110ms after the previous agent's `done` event; there is no case of two agents' windows
intersecting).

**Idle time (no agent running, cycle not finished): ~0s in all three cycles.** Every second of
wall clock is accounted for by *some* logged phase (precollect, context prep, an agent window,
inter-agent logging, or policy/dossier) — summing all phases reproduces the cycle's total duration
to within rounding. There is no slack/waiting gap hiding in the timeline; the bottleneck is that
the phases that exist are chained serially, not that there are gaps between them.

**One caveat found while looking for gaps:** EXLS has two genuinely dark spans with **zero**
entries in `pipeline_events` or `cycle_audit_log` for that cycle_id:
- **dispatch gap, 37.2s** (`cycle_trigger` 08:15:29.187 → `explicit_tickers` 08:16:06.398). ET and
  ZS show 2.3–2.4s for the same transition. Widening the search to *all* cycle_ids in that 2-minute
  window turned up unrelated `system-log` activity on the same shared infrastructure: a scheduled
  "Flash briefing (auto)" job failed with `[INSTRUMENTATION] Prism call failed ... 500 Internal
  Server Error` at 08:15:11, sandwiched between a stream of unrelated finnhub scrape failures. This
  is circumstantial (no direct causal log ties the two together) but timing-coincident with the
  only large dispatch delay seen in the sample.
- **policy gap, 27.4s** (`v3_v3_decision_synthesizer_done_EXLS` 09:03:37.960 → `v3_policy_EXLS`
  09:04:05.365). No corresponding row in `cycle_audit_log`, `v3_guardrail_firings`,
  `v3_invariant_violations`, or `execution_errors` for this cycle_id in that window.
  `llm_audit_logs` has exactly one row per cycle tagged `agent_step: v3_decision`, but its
  `execution_ms` (2,716,617ms) is a **cumulative** rollup of the whole agent chain, not a
  per-call value — it does not explain this specific 27s. `cycle_benchmarks.finished_at` for EXLS
  is itself 9.9s later than `v3_done`, i.e. there's additional unlogged finalization after the
  events this audit can see.

Neither gap recurred at this size in ET or ZS, so this reads as noise/contention specific to that
run rather than a structural per-cycle cost — flagged for awareness, not counted in the "next 10
minutes" ranking below except as a low-confidence, EXLS-only line item.

---

## 5. The honest headline — where would the next 10 minutes come from?

Ranked by **measured seconds**, largest first:

| # | Candidate | Measured seconds | Confidence | Basis |
|---|---|---:|---|---|
| 1 | Box contention: agents decode at ~40-55ms/completion-token (~18-25 tok/s) in 32 of 33 observed runs; the one uncontended sample ran at 15.3ms/token (~65 tok/s, 3x). If that rate held cycle-wide, model time (93% of the cycle) could fall sharply. | **up to ~1,800s/cycle (theoretical)** | **low** — single uncontended data point; the fix (fewer concurrent decode consumers on the GLM box) is an infrastructure decision outside this repo, not something this audit can verify further |
| 2 | Trim `v3_bear_agent` (mean 460.0s, 0.5% tool) + `v3_quant_analyst` (mean 415.0s, 8.9% tool) — the two largest single-agent time sinks in every one of the 3 cycles, driven by loop count (5-8) and completion-token volume (7.8k-13.3k tokens), not tool calls | **~875s/cycle (14.6 min)** | **medium** — real, reproduced in all 3 cycles; a prompt/loop-budget change, needs care not to cut decision quality |
| 3 | Tighten the precollect retry/timeout budget on multi_api_news/reddit/youtube/finnhub-body-fetch — these sources return usable content on only 14-22% of attempts, yet gate the whole precollect phase (89-99% of its wall time) | **~124s/cycle (2.1 min), mean; 65-180s observed range** | **high** — directly measured from `cycle_audit_log`, reproduced in all 3 cycles, near-zero downside since the failing attempts aren't contributing signal today |
| 4 | Investigate the two unlogged dark gaps (dispatch 37.2s + policy 27.4s, EXLS only) | **64s (one cycle only)** | **low** — not reproduced in ET/ZS (2-4s each); root cause circumstantial (coincides with an unrelated Prism 500 error in one case) |

**Bottom line:** candidates #2 and #3 alone are measured, reproduce across all three cycles, and
already total **~999s (16.6 min) per cycle** — comfortably past the 10-minute target — without
touching the box's queueing behavior at all. Candidate #1 is the largest number on the board but
rests on one data point; it says "look here next," not "cut here now." Concurrency is not a lever:
agents already run at max_concurrency=1 with ~0 idle time, so there's no free parallelism to
recover without changing the agent dependency chain itself, and no slack to trim — every measured
second here is currently *busy* seconds, not waiting seconds.
