# Stalls, silent costs and noise — the five findings after cycle-v3-1788660665

Branch `fix/stalls-and-silent-costs`. Successor to
`docs/ETF_TIER_GATE_2026-09-06.md`. Operator rule for this pass: **measure
first, fix by the measurement**; every fix red-first on a verbatim row from the
incident, then sabotaged property by property.

## Measurements that re-diagnosed the findings (all read-only)

### F1 — the "silent 18 minutes" (ABT fundamental analyst)

Prism `requests` ledger, conversation `c07a8ef2`:

| UTC | iteration | status | note |
|---|---|---|---|
| 02:45:12 | 8 | completed | in=35,556 |
| 02:49:42 | 9 | completed | in=35,773 |
| 02:49:48 | 10 | **pending** | no first chunk |
| 02:56:09 | — | completed, success=False | "Provider stream stalled: no data received for 300s" |
| 02:56:15 | 1 (**new conv `59a00f67`**) | completed | the client's retry, prompt rebuilt from scratch (in=28,648) |
| 02:58 / 03:01 / 03:04 | 2 / 3 / 4 | completed | retry progressing… |
| 03:03:45 | — | — | shared 1800 s wall clock (from 02:33:45) kills attempt 2 |
| 03:04:25 | 4 | completed | …server kept generating for the dead client (`persistOnDisconnect`) |

Concurrent on the same box during iteration 10: quant it 2/3/4 (in 32-35k,
out up to 2,113), valuation it 3, bull it 1 (out 5,950) and it 2.
So: not silent, not a detector gap. Prism's watchdog fired; the client retried
**from zero** and the retry inherited an exhausted budget.

`timeToGeneration` for 586 completed GLM rows since 09-03:

| in flight at start | n | p50 | p90 | max |
|---|---|---|---|---|
| 0 | 214 | 6.7 s | 42 s | 194 s |
| 1 | 184 | 46 s | 148 s | 292 s |
| 2 | 147 | 92 s | 201 s | 292 s |
| 3 | 29 | 110 s | 235 s | 264 s |

Completed max = 292 s: the 300 s watchdog clips the tail of a
concurrency-driven prefill queue. **16 stall rows in 3 days** across six agent
kinds. The per-box cap in production is `DGX_MAX_CONCURRENT=6`
(`deploy.sh:98`); the adaptive limiter logged no adjustment in 48 h.

### F3 — the engine caches but does not report

`vllm:prefix_cache_hits_total` = 9.09M of 12.25M queried tokens lifetime for
GLM (74%); three identical ~500-token probes added 0 hits (below the hybrid
model's cacheable chunk). Raw `usage.prompt_tokens_details` is **None** on
every response. Shim: byte pump (ruled out). Prism maps the field only when
> 0 and its accumulator always emits `cacheReadInputTokens: 0`.
trading-service's reader returned the FIRST key present, so that 0
short-circuited the OpenAI-shaped fallback — a real reader defect, dormant only
because the engine reports None.

### F5 — the warning was noise; the signal was in the tool table

31 rows = one level-check line re-firing after every tool result
(`elapsed_s > 180 and tool_call_count > 4`, `logger.error`): fundamental 20×
(191–963 s), junior 8×, valuation 2×, bull 1× — three of the four SUCCEEDED.
`agent_tool_telemetry`, same cycle: get_finnhub_news 50% success at p95 49,753
ms; screener_query / get_institutional_holdings / get_earnings_data pinned at
49.7 s (the bridge's 55 s MCP deadline); **`think`: 11 calls, 0% success**.
Since 09-01: `think` 329 calls, 9% success, 301 `POLICY_DENIED`, 8 of them
taking > 20 s. It is not defined anywhere in our tree or prism's — the model
invents it.

### F4 — six rows are three events

02:56:09 the F1 stall; 03:22:24 HTTP 500 on `?stream=false`; 03:51:27
`RemoteProtocolError` on `?stream=false` = NBIS memory consolidation
(`consolidator.py`, no retry, 6-h cooldown stamped BEFORE the call). Prism
ledger: three JSON-path rows stuck `pending` forever (username `lazycat-sdk`,
project `vllm-trading-bot` — our `call_prism_agent` callers whose names map to
the light-work persona). JSON path: no heartbeat, buffers all events, returns
nothing on client close; proxy answers 500 after 900 s.

### F2 — what autoresearch would do with a stopped cycle (E2, offline)

`run_autoresearch("cycle-v3-1788668370", <verbatim summary>)` with every
pymongo write recorded: overall 90.6, data 97, decision 90, LLM 84.7, **no**
degenerate anomaly, `decision_outcomes` for AVGO only. A clean report. The only
things it could not know: the cycle was partial, and `tickers_final` claimed
LULU had finished.

## Fixes on this branch

| commit | finding | what | proof |
|---|---|---|---|
| c053c70 | F3 | `extract_cached_tokens`: first NON-ZERO spelling wins, flat before nested; all-zero stays 0; never a sum | red on the verbatim 8-key GLM shape; 3 sabotages caught |
| 0f88fe2 | F2 | `enqueue_autoresearch()` from done, stopped and error tails; `partial_summary_fields()` flags partial and narrows `tickers_final` to store-recovered desks | red ImportError; 4 sabotages caught (incl. JSON-string payload); tails located by their own log text |
| de0f47c | F5 | `slow_run_notice()` once per run, WARNING, past 0.5 × runner timeout, names the tool; `[ToolDeadline]` line at ≥ 90% of the 55 s bridge cap | verbatim 191 s/5-turn and 963 s/14-turn rows; 4 sabotages caught |
| 2cf7e90 | F4 | consolidator stamps AFTER the outcome; transient failure re-opens after 10 min | verbatim RemoteProtocolError; 2 sabotages caught |
| 6dc1755 | F4 | `call_prism_agent` inside `aresilient_call(3, exponential)`, explicit `retryable_types` — transport only, 400 not retried | verbatim messages; 3 sabotages caught. First draft relied on the SDK default, which retries a DEGRADED 400 once; its own test stayed red and a patch that asserted before writing produced an amend with the message but not the code — caught because three sabotage runs printed identical results |
| cf000f9 | F1.1 | per-box concurrency pools: `limit_for(box)` runs the KV tiers on that box's own metrics, capped at `MAX_RUNNING_PER_BOX = 2`; the orchestrator names the box via `box_for_agent()` | verbatim 23:48:23 two-box log line; 4 sabotages caught (ceiling removed, global average again, shared counter, box dropped) — the first independence test could not see a shared counter and was tightened |
| 98559d7 | F1.2 | budget-aware retry: past attempt 1, < `RETRY_MIN_BUDGET_S` (600 s) left refuses with `RetryBudgetExhausted`; runner records `RETRY_BUDGET_EXHAUSTED` with cost | verbatim ABT timeline (447 s left); 2 sabotages caught; the test found an UnboundLocalError in the first draft (`time` is a local name inside the attempt). Registration as non-retryable is not test-pinned: the SDK already stops on FATAL past attempt 1 |

## F1 experiments

### E1a — TTFT vs concurrent prefills, idle box (operator-approved, 07:33 UTC)

Unique ~25.3k-token prompts (no prefix-cache help), `max_tokens` 8, streaming,
first delta of any kind; k concurrent, 3 rounds each; box idle before every
round (`running 0, waiting 0, kv 0.0`).

| k | TTFT of each request (s), one round | pattern |
|---|---|---|
| 1 | 23 · 23 · 36 | one 25k prefill ≈ 23 s |
| 2 | 31.5 → 45.4 | second waits for the first |
| 3 | 31.6 → 56.8 → 68.1 | serialised, ≈ +20 s each |
| 4 | 31.6 → 57.2 → 76.3 → 91.4 | max 91 s |

Prefills serialise at roughly 20-23 s per 25k prompt: TTFT(k) ≈ 23 + 22·(k−1).
Reaching the 300 s watchdog by raw concurrency alone would take ~13 concurrent
prefills, and the production cap is 6. **So concurrency count alone does not
reproduce the ledger's 150-292 s tail or the 16 stalls.** The ledger rows
that stalled had neighbours mid-DECODE with 30k contexts resident (bull it=1,
out=5,950) — a different load shape from k idle prefills. E1a-2 tests exactly
that shape: holders mid-decode (max_tokens 1500, 9k contexts), then a 25k
probe; TTFT and `gpu_cache_usage` at the probe. Result appended below.

### E1a-2 — TTFT beside DECODING neighbours (the load shape the ledger showed)

Holders: `max_tokens` 1500 on 9k contexts, started 40 s before the probe so
they are mid-decode with KV resident; probe: a fresh 25k prompt.

| decoding neighbours | KV used at probe | probe TTFT |
|---|---|---|
| 2 | 38%, running 2, waiting 0 | **165.5 s**, 160.6 s |
| 4 | 38-58%, **running 2, waiting 2** | **284.2 s**, then **309.8 s — past the 300 s watchdog: a stall, reproduced on demand** |
| 6 | 38-58%, running 2, waiting 2 | **HTTP 503 after 119.5 s, twice — the box refuses rather than queues** |

Two decoders beside an idle-equivalent prefill: 45 s → 160 s. Prefill is
starved by decode, not by other prefills. And with four holders the box itself
reports `running 2, waiting 2`: **the engine runs two sequences at a time and
queues the rest**, so anything we admit beyond two waits in vLLM's own queue
behind decoders — 284 s at four, past the 300 s watchdog at six. The per-box
ceiling therefore matches the engine's own concurrency: `MAX_RUNNING_PER_BOX
= 2` (env-overridable), not the deployed `DGX_MAX_CONCURRENT=6`.

### E1c — what the limiter actually caps

`AdaptiveConcurrencyController` adjusted 12 times in 7 days, so it is alive —
but it is GLOBAL: `max_capacity=12` (jetson 6 + dgx 6), `running` summed over
both boxes, and the KV figure it keys on is the AVERAGE across endpoints
(`_avg_cache_usage`). With the Jetson idle or offline its 0% halves gold-spark's
reading: the log shows "Limit adjusted 8 → 6 (cache=35.1%, running=5)" while
gold-spark alone carried the five. The cap the stalls need is per box.

The six-holder round completes the curve and adds a failure mode the ledger
could not show: past four in flight the shim stops queueing and answers **503
after ~120 s**. So the box's own admission limit is four (two running, two
waiting), and beyond it a request is refused outright rather than served late.

### E1b — resume vs restart: NOT RUN, and F1.3 is dropped

The probe was approved and written (`e1b_resume_probe.py`) but never run: the
box was needed for E1a-2's six rounds and then a live cycle took it. Nothing on
this branch attempts conversation resume, and `lazycat/llm.py` still sets
`createSession=True` on every attempt.

This is a deliberate drop, not an oversight. F1.2 (refusing a retry that cannot
finish) removes the cost the resume was meant to avoid — a restart that cannot
complete is no longer started at all — so resume is an optimisation, not a
correctness fix, and it belongs with the next box-time window. The plan's own
fallback said exactly this: "if not [resumable], budget-aware retry alone still
stops the 30-minute burn."

## Report-only (prism / utilities-library, never edited)

- Watchdog at 300 s sits below the measured TTFG tail at ≥ 2 in flight.
  CONFIRMED with file:line in `docs/audits/2026-09-06/prism_side.md`; the
  shim's own comment names `DEFAULT_MAX_CONCURRENT["gold-spark"] = 4` and
  `QUEUE_TIMEOUT_MS = 120_000` — exactly the 503-after-119.5 s E1a-2 measured.
- `LocalModelQueue` has no wait bound. CONFIRMED.
- ~~No client stop route for `/agent`~~ — **wrong as stated.** `POST /agent/stop`
  exists; the gap is that nothing reaps a session whose client never returns,
  and trading-service never calls it on cancel. See the prism report.
- JSON path: no heartbeat, events buffered, nothing returned on client close.
  CONFIRMED; proxy cliff is 900 s for `/agent`.
- `cacheReadInputTokens` default 0 masks "absent".
- ~~Light-work persona mapping erases the caller's identity~~ — **wrong layer.**
  Prism rejects an unmapped agent with a 400. The collapse into
  `CUSTOM_SYSTEM_JANITOR_AGENT` is ours: `app/services/prism_agent_registry.py:191-283`.
**Correction (2026-09-06, later the same day).** An earlier draft of this
section said "the `think` tool name is model-invented; the 50 s denials are the
bridge's queue path". Both halves are wrong, and the store says so:

- `think` is a real tool prism force-adds to every custom agent (the
  CORE_AGENTIC set), and WE deny it: commit 51892a90 (2026-09-02) added it to
  `_V3_DENIED_TOOLS` *to save turns* and named it in prompt rule 7.
- A denial is not slow. Over 320 failed `think` rows the median is **1 ms**;
  only 2 rows exceeded 40 s. The p95 of 49.6 s quoted for the incident cycle
  came from a single outlier.
- The suppression did not work. In the four days after the deny landed:
  **301 POLICY_DENIED calls across 261 agent runs — 68.9 % of the 379 agent
  runs that made any tool call.** 30 runs spent two turns on it, 2 runs spent
  four; turn budgets are 4–6. Before the deny, `think` ran 2,699 times at
  ~100 % success.

The turn is spent the moment the model emits the call, so denying it saved
nothing and returned an error instead of a scratchpad. `think` was also in
`_META_TOOLS` (the canary's "never warn" set), so the waste was invisible: a
tool that is both DENIED and exempt from the canary can burn a turn per run
forever without a single log line. That invariant is now a test; the
deny-or-restore decision is an operator call and is listed under "Still open".


## Two independent reviewers, and what they found

The seven commits above were then handed to two reviewers working from the
plan, not from the commit messages: one adversarial (re-run each test, mutate
the production code, report anything that stays green), one completeness
(promised vs delivered). The adversarial pass ran 21 mutations. Six survived
— i.e. six real defects, one of them a blocker — and are fixed in the five
commits that follow.

| # | severity | what was wrong | fix |
|---|---|---|---|
| 1 | **blocker** | The per-box cap never applied. Every `limit_for` branch ended in `max(self.min_concurrency, …)` and the shipped floor is **4** (`ADAPTIVE_MIN_CONCURRENCY`), so each box was capped at 4, not 2 — and E1a-2 measured 284 s and 310 s to first token at exactly four in flight. The cap would have prevented none of the stalls it was written for. Every test built the controller with `min_concurrency=1`, so none could see it. | e73d093 |
| 2 | major | A box run skipped the global check while still incrementing the global counter, so `total_active` could exceed `current_limit` and box-less callers (consolidator, briefings, `VLLMClient.chat`) queued behind runs admitted without consulting their pool. | e73d093 |
| 3 | major | `_retry_was_refused` looked for an `exception` attribute on each attempt record; `AttemptRecord` carries `exception_type` as a **string**, the cause chain is empty, and the class name is absent from `str(exc)`. It returned False for every real refusal, so `RETRY_BUDGET_EXHAUSTED` could never appear in a row. | 7c260a2 |
| 4 | major | Nothing tested that the slow-run notice is *wired in*: replacing the whole hook body with `pass`, or nulling `soft_deadline_s` at both runner call sites, left every test green. | 10c21ae |
| 5 | minor | `tickers_final: []` (a cycle stopped before any desk finished) is falsy, and the consumer read `tickers_final or tickers_requested` — so the one case the narrowing exists for fell back to the full requested list. | 49509ac |
| 6 | minor | Moving the consolidator's stamp after the outcome left an escape: an exception raised before the callee's own `try` stamped nothing, so the gate never closed and the ticker re-attempted every cycle. | 49509ac |

Cleared under attack, worth recording: `extract_cached_tokens` survived ten
value-shape probes (string, negative, float, bool, None under either spelling)
without raising or double-counting; the 404-refresh inside the retry wrapper is
bounded (≤ 6 upstream calls, ≤ 3 refreshes) and does not launder a 404 into a
transient; a 400 still fails fast; and the "took too much time" phrase contract
still holds.

One reported symptom was **not** a defect. The completeness reviewer saw
`test_it_never_fires_twice` and two consolidator assertions fail once each
across ten runs and called it flakiness in the new suite. Those three tests are
exactly the ones the adversarial reviewer's mutations M6, M8 and M9 target, and
both reviewers were working in the same worktree at the same time: the critic
was observing the refuter's temporary sabotages, not a flake. Re-run here 25×
after both finished — 12× with random ordering disabled, 13× with it on — all
111 tests passed every time. The lesson is about the harness, not the tests: two
agents must not share one worktree when one of them mutates it.

## What this pass did NOT do

Stated plainly, because a reader of the fix table would otherwise assume these
were closed:

- **F3's second instrument.** The plan asked for a box-wide
  `prefix_cache_hit_ratio_box` sampled from `vllm:prefix_cache_hits_total` and
  stored on the telemetry row. Not built. Only the reader fix shipped. The
  engine still reports `prompt_tokens_details: None`, so per-request cache
  figures remain unavailable and `cached_tokens` stays 0 — now explained rather
  than instrumented.
- **F4's actual remedy.** `call_prism_agent` still calls the `?stream=false`
  JSON path; only retries were added around it. The plan's live proof ("the URL
  itself disappears") cannot be met by this branch, and the branch's own test
  fixture still targets `/agent?stream=false`.
- **F1.2's per-agent budget.** `RETRY_MIN_BUDGET_S` is a flat 600 s env
  constant. The plan specified the ledger's p90 run time per agent, and listed
  "use the literal 600 when a ledger figure exists" as a sabotage to catch. The
  shipped behaviour is that sabotage.
- **F1.3 resume** — see E1b above.
- **The SDK keepalive** (`httpx.Limits(keepalive_expiry=…)`) — conditional on
  E4a showing a sub-second disconnect, which was never measured.
- **The `think` decision** — measured (above), invariant test added, but the
  deny-or-restore choice is the operator's.

## Suite

Worktree unit suite after each landing: 6,439 (F2-F5), 6,444 with F1.2 (one
failure, `test_vllm_endpoints_view::test_the_poller_keeps_the_model_current`,
which passes alone 3× and on master and did not reproduce in the next run —
timing, not order), **6,453 / 0 failed with F1.1**. After the review fixes:
recorded below once the full suite has run on the merged primary.

## Live proof carried over from the previous pass — check #4

`cycle-v3-1788674782` (06:33 UTC, one ticker, ET BUY, 12/12 SUCCESS) is the
first cycle to reach `done` after `ed62fa1`. Its autoresearch report:
`status=done`, overall 92.8, `recovery_stats = {cycle_id: cycle-v3-1788674782,
total_failures: 0, by_type: {}, resilience_exhausted: 0}` — serialised, on the
right cycle, honestly zero. **Check #4 PASS live.** The same cycle logged
"took too much time" 5× on a healthy one-ticker run (F5's target, still on the
deployed code at that hour).

## Store findings from the verification audit (2026-09-06, read-only)

Measured while preparing the observed verification run. None of these are
caused by this branch; all bear on what a cycle can prove.

- **The UI calls a healthy long turn dead — observed live.** The client marks a
  running cycle stale at 300 s (`RUNNING_STALE_THRESHOLD_MS`), but
  `pipeline_state.updated_at` only advances at named phase boundaries, never
  inside an agent's tool loop. Sampling the live `cycle-v3-1788682529` every
  15 s: **25 of 101 samples showed the state older than 300 s, peaking at
  522 s**, while `v3_quant_analyst` and then `v3_bear_agent` were running
  normally and the cycle went on to keep working. The threshold is not the
  defect; the missing heartbeat is.
- **`recovery_stats.recent_events` has never been written non-empty** — 0 of
  the 244 stored reports that carry the field. The one cycle that produced
  events (`cycle-v3-1788660665`) is the incident whose report is `status=error`
  with `recovery_stats=None`. A clean cycle writes `[]`, so **a clean cycle
  cannot prove the ISO-string repair**; only the writer boundary or an injected
  failure can.
- **AUTORESEARCH is not idempotent per cycle.** `cycle-v3-1788486930` has four
  commands and four `done` reports (2026-09-04, 03:47 / 05:00 / 05:07 / 05:10);
  `cycle-v3-1784554200` has two of each. The enqueue keys its upsert on a fresh
  random `job_id`, so nothing ties one cycle to one report.
- **The queue claim is not atomic.** `eval_worker.poll_system_commands` does
  `find_row(status=pending)` then `update_docs(status=running)`, while
  `mongo_store.find_one_and_update` exists and documents itself as the claim
  primitive for exactly these two queues.
- **74 % of cycles never reach the tier gate.** Over 21 days, 143 of 194 cycles
  took the explicit-ticker path ("discovery & gatekeeper bypassed"); only 17 ran
  `GATEKEEPER_SELECTED`. `ensure_ticker_metadata` has exactly one caller, inside
  the gatekeeper branch and only when more than one name was selected — so the
  ETF classification, the mega-cap cap and `tier_unknown` do not run on the
  common path. Two tickers bought in the last 21 days (ZS, SE) still have no
  `ticker_metadata` row at all.
- **The ETF branch has already run in production, and failed.** The incident
  cycle `cycle-v3-1788660665` selected `['SCHD', 'ABT', 'NBIS']` through the
  gatekeeper, reported `tier_unknown: ['SCHD']`, and **bought SCHD** at 03:29
  with confidence 72. SCHD now carries `asset_class: etf`,
  `market_cap_tier: etf`, `market_cap: $112.3B`. The fix is real; what is
  untested is the fixed behaviour, and a verbatim fixture for it exists.
- **The lifecycle timeline is `pipeline_events`, not `cycle_audit_log`.** The
  audit log holds only warnings and errors (46 rows for the last completed
  cycle, all `warning`/`critical`); the timeline is 65 rows in `pipeline_events`
  carrying `cycle_trigger`, the path marker, `v3_start_*` … `v3_done_*`,
  `v3_policy_*` and `trade_executed_*`.
- 16 `system_commands` rows written 2026-08-18 have no `status` and no
  `created_at` — invisible to the poller and to any stuck-command query. Inert
  residue: only `pipeline_service` writes that collection now.

## Still open (operator decisions)

1. **`think`** — restore it (it ran at ~100 % for weeks and the turn is spent
   either way), or keep the deny and accept ~1 wasted turn in 69 % of runs.
   Restoring is one line; the invariant test passes under either choice.
2. **The UI heartbeat** — the server should stamp `updated_at` inside a long
   agent turn, or the client's threshold should key on something that does
   advance. This is the criterion the observed run is meant to check, and today
   it fails.
3. **Report idempotency** — one report per cycle, or explicit ordered versions.
