# Learning boundary validation — September 7, 2026

The live canary confirmed removal of 8,311 characters of unverified memory/workflow context. However, the eight-pair task replay used 42.1% more total tokens and lost its one passing source-quotation result in the after arm. The performance improvement gate failed. The deployed changes enforce the learning boundary and reviewed role contracts; they do not establish better investment performance or faster complete trading cycles. Canonical memory serving and automatic skill proposals remain disabled.

## Implementation and migration

- Trading runtime: `457829ec`, branch `fix/learning-lifecycle-20260907`.
- Owned Prism proxy/model shim: `5ca0fea`, lazy-agent-service branch `fix/trading-learning-boundary-20260907`.
- Prism is unchanged at `27ee332b`. No Prism database migration or deployment was performed.
- Seven unreviewed active skills were quarantined; seven reviewed, bounded methods are active. Exact content eligibility was checked against the production database.
- Migration recorded 4,501 legacy dispositions and retained raw evidence, including 237 expired, unpromoted observations. Snapshot SHA-256: `d04675486cbf34ea3689bd386543a20a78969e432466b8fffa21996b1ac52988`.

The [implementation contract](learning-contract-v2.md) describes controls, receipts, durable queues and recovery. Upstream memory extraction can still run inside Prism; our gateway excludes its unvalidated output from Trading model requests. The live compatibility test covers the configured GLM vLLM path, not every possible provider or model-specific message rewrite.

The reviewed baseline methods restore known role and policy contracts. They are not presented as empirically optimized skills. No model-generated candidate was promoted, and a smaller prompt alone does not qualify learned content for serving.

## Live gateway canary

A request through `/prism-proxy/agent` and the production model shim combined a minimal JSON task, a conflicting tagged memory canary and Prism's actual retrieved context. The model returned exactly `{"result":"CANARY_OK"}`. A persisted Trading gateway receipt reports removal of 8,311 characters in `agent-memory` and `past-workflows` blocks.

The filtered message JSON was 461 characters. This is not total model input: Prism also supplied a tool catalog, and usage reported 21,238 input tokens and eight output tokens. End-to-end request time was 47.82 seconds, overwhelmingly queue/wait time. This test proves filtering on that path and preserved response behavior; it does not measure a latency improvement.

## Four paired contract probes

Eight actual GLM-5.3-Flash-EXL3 requests used temperature zero, min_p zero, thinking disabled and a 128-token output limit. Within each pair, only the old versus reviewed role method changed. AB/BA ordering alternated. These short tasks tested HOLD, a distinct FLAT outcome, a configured 5% limit and a requested historical filing period. No tools or production trading writes occurred.

| Probe | Before input tokens | After input tokens | Before exact JSON | After exact JSON |
|---|---:|---:|---|---|
| Junior: HOLD | 363 | 141 | Fail: extra fields | Pass |
| Bear: FLAT | 362 | 146 | Pass | Pass |
| Board: configured limit | 315 | 149 | Pass | Pass |
| Fundamental: historical period | 359 | 148 | Pass | Pass |
| Total | 1,399 | 584 | 3/4 | 4/4 |

Input tokens fell 58.3%; input plus output fell from 1,447 to 613, or 57.6%. Both versions answered all four substantive questions correctly. The old junior response failed only strict object equality because it added a reason and flags; this is not evidence of a wrong trading decision. The small sample does not establish a quality improvement. Cache counters were unavailable. A first-request wait outlier makes aggregate timing unsuitable for a speed claim.

## Frozen agent-task replay

The separate replay covers eight paired tasks across seven roles using frozen LULU/STX evidence and original role prompts. Before uses captured active skills; after uses reviewed methods plus the actual owned request transformation. Both arms offer the same four read-only tool stubs, returning only frozen evidence. Each task has at most four model calls and 4,096 output tokens per call. This is a controlled task replay, not the full live harness: production budgets and tool catalogs differ.

The preregistered quality checks are typed artifact validity, at least two exact source quotations and applicable decision-policy checks. Failures remain in the denominator. Redundant calls return evidence already in the prompt. Timing and cached-token availability are recorded independently from quality.

Execution notes: an initial corpus setup error was fixed before model calls. Earlier pilots were interrupted during the ownership redesign and shim deployment; their outputs are preserved separately and excluded. The final run was paused after two completed cases and resumed without changing prompts or scoring, retaining those completed results. Requests interrupted without a completed result were restarted. The resumed run is recorded separately in timestamps; timing must not be interpreted as a controlled full-system comparison.

All 16 runs completed. The improvement gate failed: this experiment does not establish lower task cost, reliable evidence support or faster cycles.

| Measure, eight tasks per arm | Before | After |
|---|---:|---:|
| Input tokens, summed across calls | 557,521 | 802,573 |
| Output tokens | 12,805 | 7,829 |
| Total tokens | 570,326 | 810,402 |
| Model calls | 23 | 27 |
| Redundant tool calls | 25 | 33 |
| Valid typed artifacts | 1/8 | 3/8 |
| Valid source-quotation checks | 1/8 | 0/8 |
| Summed task elapsed seconds, not wall time | 1,825.36 | 1,979.32 |

Observed total tokens increased 42.1%, and tool calls increased 32%. A paired bootstrap with 20,000 resamples and seed 20260907 gives a mean token change of +30,009.5 per task, with a 95% interval of [-28,009.4, +111,795.0]. The tool-call change is +1.0 per task, interval [-0.625, +2.75]. Both intervals include zero; the small sample supports neither a general efficiency gain nor a precise general slowdown estimate. The elapsed-time interval also includes zero, and interruption, queue contention, cache state and concurrent background work prevent a controlled latency conclusion. Cached-token counters were unavailable in all runs.

| Task | Before total tokens | After total tokens | Valid artifact, before → after | Quotation check, before → after |
|---|---:|---:|---|---|
| Junior LULU | 71,463 | 72,157 | Fail → Fail | Fail → Fail |
| Junior STX | 7,652 | 69,579 | Fail → Fail | Fail → Fail |
| Fundamental LULU | 64,703 | 64,764 | Fail → Pass | Fail → Fail |
| Quant STX | 77,084 | 70,360 | Fail → Fail | Fail → Fail |
| Bull LULU | 142,835 | 47,935 | Fail → Pass | Fail → Fail |
| Bear LULU | 168,760 | 168,015 | Fail → Fail | Fail → Fail |
| Regime LULU | 7,733 | 7,054 | Pass → Pass | Pass → Fail |
| Board LULU | 30,096 | 310,538 | Fail → Fail | Fail → Fail |

Most failures were incomplete or malformed artifacts after repeated tool calls. The old Board response hit the 4,096-token output limit; the reviewed Board exhausted four tool-bearing turns. These failures remain in the denominator. No policy violation was detected by the narrow replay checks, but invalid artifacts do not demonstrate policy compliance. Missing quotations mean the requested evidence-reporting check failed; they do not, by themselves, prove the response's market claims were false.

The combined after arm changes both role guidance and the compact request transformation. This replay cannot isolate which change caused the observed differences. It also does not include the actual upstream memory injection removed in the separate live canary. Retain the ownership/eligibility boundary and reviewed contract baseline, but do not describe the method as a proven optimization or activate canonical serving or generated skills on these results. The next performance evaluation needs a representative harness and separate arms for each change, with new held-out evidence rather than tuning this corpus.

Machine-readable aggregates and per-task results are in [the replay summary](benchmarks/learning-replay-20260907.json). No memory activation decision depends on the small probes.

## Verification and artifacts

Trading's focused regression suite passed 136 tests, including real Mongo transactions and V3 contracts; the final ownership-specific migration/receipt checks passed 18 tests. The owned wrapper passed 59 focused tests and TypeScript checks; its deployment gate passed 641 tests. Counts overlap and must not be added as independent coverage.

Raw local evidence is retained under the workspace's `.scratch/learning-revamp-20260907/`: `benchmark-protocol.md`, `focused-results.json`, `gateway-canary.json`, `owned-migration-applied.json`, `replay-final/` and the service deployment/test logs. Frozen source evidence and model outputs are intentionally not copied into this report.

Both targeted deploy-kit runs completed with transfer, container restart and application verification. Trading is running revision `457829ec`, started at `2026-09-07T12:16:10Z`; the owned wrapper is running `5ca0fea`, started at `12:04:36Z`. Trading's `/health` and authenticated `/learning/health` returned 200. Receipt delivery and lesson indexing reported ready with zero failures; the backlog reported 25 eligible tickers and 188 unpromoted observations pending processing. Prism remains on its August 26 container. Deploy-kit reported an unrelated existing edge DNS reconciliation warning; the service deployment itself succeeded.
