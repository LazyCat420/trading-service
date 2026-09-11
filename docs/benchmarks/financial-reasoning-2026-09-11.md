# Financial evidence validation: benchmark and release

**Validation and NAS deployment are complete.** The final candidate passed 10 of 12 fresh cases, 11 of 12 saved-response repairs, and all eight variant repeats. Its first held-out run also passed all eight cases; matched baseline review found five responses with material factual errors and only two complete, consistent responses. The improvement is in code-calculated, code-rendered financial explanations and evidence coverage—not learned arithmetic or investment performance.

Automatic approval review initially rejected the revised run twice; neither command started. The user subsequently authorized the revised benchmark and follow-up validation. Fresh testing resumed, and the validated release was deployed after all checks below completed.

## What the candidate does

The original twelve responses and review remain frozen. The old expression-only arithmetic audit checked zero relationships in those responses; independent review found factual errors in nine and no fully complete response.

Source builders capture the same technical, fundamental, valuation and portfolio snapshots they render. Each fact retains its metric, value, unit, source, period and original date. A timed-out builder cannot later update the accepted snapshot. Incomplete portfolio marks do not become verified exposure. Unknown reporting currencies remain unknown. Code calculates holding return, range location, forward PEG from the correct earnings-growth operand, concentration headroom, and separate present versus hypothetical reward/risk. Per-order and total-concentration caps remain distinct.

The financial validator checks explicit claims against those records, verifies question coverage and dates, and rejects incorrect relationships. It checks rationale fields, including mispricing and override explanations. Sentence-scoped checks prevent an unrelated negation from disabling an entire paragraph's validation. Requested purchase-fit answers must include the actual fit calculation. A true but unrelated metric cannot substitute for a requested calculation; an available calculation cannot be hidden behind abstention.

The runner gives the agent one correction opportunity shared with schema and timing repair. A failed correction preserves the original output for review. Policy and order dispatch independently recompute financial validation; an authored “consistent” audit or forged policy label cannot authorize an order. Unresolved decisions receive `HOLD_POLICY_BLOCKED_FINANCIAL_EVIDENCE`.

The live pilots exposed a skipped schema correction for tool-disabled, evidence-backed decisions. That path now uses the same correction allowance. A behavioral regression also found that parsed tool-call text could be classified as a schema error: transport-failure classification now takes precedence and the unexecuted call is not repaired into fictitious research.

## Structured financial explanations

The early model repeatedly copied correct numbers but interpreted them incorrectly, or copied its rejected prose into the correction. Enabling reasoning with either output allowance tested below returned no decision. Those settings were not adopted.

The revised format uses `financial_reasoning_version: 2`. The model authors action, confidence, sizing, timing and the selection of relevant reasoning steps. It selects steps for every question. Code renders the financial statements, exact source values and original question text from those selections. Examples include whether the supplied purchase fits capacity, whether price is nearer support or resistance, the sign of a reported metric, and present versus hypothetical reward/risk.

This is **code-verified financial explanation**, not a claim that the model became better at arithmetic. The raw response remains available. A question-keyed map and an array are equivalent supported selection forms. Source IDs can identify their own observations; they do not add unselected operands. A requested relationship such as price versus oscillator units is completed only when the model selected both inputs.

The renderer does not overwrite conflicting authored prose, questions or numerical claims. Such conflicts fail validation. Its output is recomputed at the execution boundary. Legacy full-text claims still undergo their existing strict checks. Action and confidence are not rewritten by the renderer.

## Local validation

| Check | Result |
|---|---:|
| Original frozen responses with reviewed factual errors | 9 / 12 |
| Original fully complete responses | 0 / 12 |
| Original expression-audit relationships checked | 0 |
| Correct typed controls accepted | 12 / 12 |
| Corrupted numerical claims rejected | 129 / 129 |
| Correct structured selection controls accepted | 12 / 12 |
| Structured tampering cases rejected | 48 / 48 |
| Final focused financial tests | 124 passed |
| Final complete unit suite | 7,257 passed, 114 skipped |
| Final saved fresh-response replays | 24 / 24 consistent; zero new endpoint calls |
| Final saved frozen-response replays | 11 / 12 consistent; conflicting authored prose still rejected |

[Offline controls, r6](evidence/financial-reasoning-2026-09-11/offline-controls-r6.json) retain inputs, outputs, audits and source hashes. The structured controls include nonzero BUY, SELL and HOLD selections. They are constructed controls, not generated model successes. Tampering tests cover conflicting prose, altered values, changed questions and unknown steps. Additional real-runner tests verify that a smaller BUY passes financial validation and that tampering is rejected at execution.

The final complete unit suite passed 7,257 tests with 114 skips in 486.93 seconds. [Final validation record](evidence/financial-reasoning-2026-09-11/candidate-validation-r3.json) records runtime commit `b374c5e9`, exact source hashes, test-log hashes, all cohort counts and execution-boundary checks. Earlier validation records and failed development attempts remain preserved.

## Retained live development attempts

| Cohort | Cases | Endpoint calls | Financial passes at that revision | Observation |
|---|---:|---:|---:|---|
| pilot-r1 | 4 | 4 | 0 | Three truncated responses and one nested decision. |
| pilot-r2 | 4 | 8 | 0 | Readable decisions, unresolved facts and answer format. |
| pilot-r3 | 4 | 8 | 0 | Questions preserved, rejected prose copied into repair. |
| pilot-r4 | 4 | 8 | 0 | Fresh corrections still made factual errors. |
| thinking-ablation-r1 | 1 | 1 | 0 | Full 8,192-token allowance used; no final decision. |
| pilot-r5 | 1 | 2 | 0 | Focused review improved purchase-fit wording but left other errors. |
| pilot-r6 | 1 | 2 | 0 | Fact references shortened output; interpretation errors remained. |
| thinking-16k-ablation-r1 | 1 | 1 | 0 | Full 16,384-token allowance used; no final decision. |
| structured-pilot-r1 | 1 | 2 | 0 | Repair selected the needed evidence and HOLD, but used equivalent unsupported shapes. |
| structured-pilot-r2 | 4 | 7 | 2 | Unknown or empty selections reached an inappropriate generic schema-repair prompt. |
| structured-pilot-r3 | 4 | 6 | 4 | Precise selection diagnostics; two first-response passes and two corrected passes. |

These are development attempts, not a completed release benchmark. The first nine cohorts made 36 endpoint calls across 21 cases with no financial pass at their respective revisions. The next two structured pilots added eight cases and 13 calls, with six passes. Every attempt is retained; a financial failure does not imply every statement was false.

The final renderer accepts the saved structured repair without changing its HOLD action, confidence or evidence selections. The headroom, PEG and range answers retain their original question identities and source dates. The [final replay](evidence/financial-reasoning-2026-09-11/structured-pilot-r1-release-replay/01.json) is recorded separately from live attempts and makes no new inference requests. It cannot establish a repeatable improvement or generalization.

[Provider receipts](evidence/financial-reasoning-2026-09-11/provider-receipts/) retain prepared provider requests. Across development and validation there were **113 live-assisted case runs, 151 endpoint calls and 155 provider request snapshots**. These runs include repeated cases and saved-initial repair cohorts; they are not 113 independent test scenarios. The four additional provider snapshots came from upstream recovery during early development. For example, `pilot-r4` has nine provider requests for eight endpoint calls. The runner's two-call limit concerns endpoint calls; a provider snapshot does not by itself prove completed inference. Offline replays are counted separately and generate no new requests.

## Full benchmark results before the final format fixes

All rows below ran against runtime commit `28cbc032`, with the same frozen synthetic sources. First-response passes require no correction. A financial pass checks supplied-source consistency and question coverage, not investment merit.

| Cohort | Cases | New endpoint calls | First-response passes | Final financial passes |
|---|---:|---:|---:|---:|
| Fresh r1 | 12 | 17 | 7 | 11 |
| Fresh r2 | 12 | 15 | 9 | 10 |
| Original saved responses plus one fresh repair | 12 | 12 | 0 | 10 |
| First held-out candidate run | 8 | 10 | 6 | 8 |

The three fresh failures changed the oversized BUY to HOLD in their raw correction, but repeated a step ID. The renderer rejected the correction and retained the original oversized proposal as unresolved. One frozen repair had the same duplicate-reference problem. Another frozen repair included conflicting authored financial prose, including an incorrect margin-history comparison; it was rejected.

[Independent arithmetic and explanation reviews](evidence/financial-reasoning-2026-09-11/fresh-r1-review.json) recompute core calculations directly from raw fixture values, separately from the production validator. The [repeat-cohort review](evidence/financial-reasoning-2026-09-11/fresh-r2-review.json), [frozen review](evidence/financial-reasoning-2026-09-11/frozen-r1-review.json), and [held-out candidate review](evidence/financial-reasoning-2026-09-11/holdout-candidate-r1-review.json) retain every question, selected value and explanation. No arithmetic discrepancy was found in their checked claims. Missing claims or unresolved artifacts are not counted as successes merely because that arithmetic check had nothing to inspect.

The matched [held-out baseline review](evidence/financial-reasoning-2026-09-11/holdout-baseline-r1-manual-review.json) found material factual errors in five of eight responses; two were complete and consistent with supplied evidence. It was judged on factual content and semantic question coverage, not penalized for lacking the candidate-only schema. Errors included saying a price below its moving averages was above them, using the wrong range denominator, comparing operating margin with sector ROIC, and incorrect current/future reward-risk arithmetic. The baseline also produced correct answers and useful abstentions, retained in the review.

The candidate answered all eight first-held-out cases consistently, including the changed headroom, positive versus negative holding returns, current filing values, source independence and different current/future reward-risk ratios. Both baseline and candidate chose HOLD in these eight cases. Two frozen-response repairs produced evidence-consistent SELL decisions. This is an improvement in financial explanation and coverage; it does not establish an improvement in investment selection or profitability.

## Final format fixes and confirmation

The [follow-up plan](evidence/financial-reasoning-2026-09-11/followup-format-plan.json) was recorded from fresh/frozen development failures **before reviewing any held-out response**. Runtime commit `b374c5e9` makes two changes:

- Repeated known step references are idempotent: raw selection lists remain intact, while each statement and source record is rendered once. Repetition cannot manufacture independent corroboration. Unknown IDs and conflicting authored fields still fail.
- Every financial schema repair uses the structured financial contract, including malformed legacy responses. It no longer requests authored prose that the same contract forbids.

New regressions failed in five cases before these changes and passed afterward; all 124 financial tests and all corruption controls passed. [Exact-response replay verification](evidence/financial-reasoning-2026-09-11/final-offline-replay-verification.json) confirms that all 24 saved fresh cases now pass using identical model responses, with no new endpoint calls and no changes to accepted action, confidence, size or selections. Eleven of twelve saved frozen pairs pass; the remaining authored-prose conflict is still rejected. These are offline replays, not new model successes.

Final live confirmation ran on the committed version:

| Final cohort | Cases | New endpoint calls | First-response passes | Final financial passes |
|---|---:|---:|---:|---:|
| Fresh confirmation | 12 | 18 | 6 | 10 |
| Original saved responses plus fresh repair | 12 | 12 | 0 | 11 |
| Variant regression repeat | 8 | 10 | 6 | 8 |

The two fresh failures left `q-units` without any selected evidence even after correction. The frozen failure selected an unknown `held_deterioration` step ID. All three remain blocked. The prior duplicate-reference failures and the conflicting schema-repair prompt were resolved. [Fresh review](evidence/financial-reasoning-2026-09-11/final-fresh-r1-review.json), [repair review](evidence/financial-reasoning-2026-09-11/final-frozen-r1-review.json), and [variant-repeat review](evidence/financial-reasoning-2026-09-11/variant-regression-r1-review.json) retain all outputs, including failures. The repeat variants are not newly unseen data.

The final 32-case execution check rejected all three unresolved outputs despite forged saved pass labels, and rejected value tampering in all 29 accepted outputs. Accepted action, confidence, size and evidence selections exactly match the model's final response. The final runs include two accepted SELL decisions and a conditional BUY: that BUY waits for a price condition and re-analysis, rather than authorizing an immediate order. All benchmark tools and orders were disabled.

## Benchmark protocol and release

The benchmark includes development pilots, two fresh twelve-response cohorts, saved-response repair, and matched baseline/candidate runs on eight variants frozen before live candidate testing. Every attempt is retained. Review considers delivered explanations, question completeness, selected evidence, action consistency and useful abstention separately from schema/transport status. First-response and corrected results are reported separately. Final repeat validation follows the two format fixes.

Release criteria were met: no known factual/calculation error escaped final validation; accepted outputs supplied useful financial answers and explicit unknowns; unresolved errors failed the independent execution check. Remaining model instruction-following failures are reported above. Only `trading-service` was deployed through deploy-kit. The container-revision and HTTP-health checks below passed.

## NAS deployment

Deployment completed through `npm run deploy -- --only=trading-service --skip-pull`: one service passed, zero failed, and nineteen were skipped. The image was transferred and the container restarted after deploy-kit confirmed the pipeline was idle.

[Deployment verification](evidence/financial-reasoning-2026-09-11/nas-deployment-r1.json), captured at 18:39 UTC on September 11, confirms:

- Running, Docker-healthy container `trading-service`, started at 18:37:35 UTC.
- Release revision `f5605166`, including tested runtime commit `b374c5e9`.
- All four deployed financial module hashes match the tested source files.
- HTTP 200 with `status: ok` from `http://10.0.0.16:3031/health`.

Deploy-kit reported a non-blocking DNS reconciliation warning. The [read-only diagnostic](evidence/financial-reasoning-2026-09-11/nas-dns-check-r1.json) found missing Cloudflare credentials, so it could not reconcile DNS; it did not establish a specific bad record. The diagnostic made no DNS changes, and the edge Caddyfile was unchanged. NAS service availability was verified independently.

Reproduction from the `trading-service` directory:

```bash
# Offline controls; always choose a new output file.
.venv/bin/python scripts/benchmark_financial_controls.py --out /tmp/financial-controls-new.json

# Offline replay only: no new model request.
RUN_FINANCIAL_BENCHMARK=1 FINANCIAL_BENCH_COHORT=replay-new FINANCIAL_BENCH_REPLAY_COHORT=structured-pilot-r1 FINANCIAL_BENCH_INDICES=1 .venv/bin/python -m pytest tests/unit/test_financial_live_benchmark.py -q -s

# Revised live runs: authorized; preserve every attempt under a unique cohort name.
RUN_FINANCIAL_BENCHMARK=1 FINANCIAL_BENCH_COHORT=structured-pilot-r2 FINANCIAL_BENCH_INDICES=1,6,7,12 .venv/bin/python -m pytest tests/unit/test_financial_live_benchmark.py -q -s
RUN_FINANCIAL_BENCHMARK=1 FINANCIAL_BENCH_COHORT=fresh-r1 .venv/bin/python -m pytest tests/unit/test_financial_live_benchmark.py -q -s
RUN_FINANCIAL_BENCHMARK=1 FINANCIAL_BENCH_COHORT=fresh-r2 .venv/bin/python -m pytest tests/unit/test_financial_live_benchmark.py -q -s
RUN_FINANCIAL_BENCHMARK=1 FINANCIAL_BENCH_MODE=frozen FINANCIAL_BENCH_COHORT=frozen-r1 .venv/bin/python -m pytest tests/unit/test_financial_live_benchmark.py -q -s
RUN_FINANCIAL_BENCHMARK=1 FINANCIAL_BENCH_FAMILY=holdout FINANCIAL_BENCH_VARIANT=baseline FINANCIAL_BENCH_COHORT=holdout-baseline-r1 .venv/bin/python -m pytest tests/unit/test_financial_live_benchmark.py -q -s
RUN_FINANCIAL_BENCHMARK=1 FINANCIAL_BENCH_FAMILY=holdout FINANCIAL_BENCH_VARIANT=candidate FINANCIAL_BENCH_COHORT=holdout-candidate-r1 .venv/bin/python -m pytest tests/unit/test_financial_live_benchmark.py -q -s

# Final confirmation used final-fresh-r1, final-frozen-r1 and variant-regression-r1.
# Existing cohort names are retained and cannot be overwritten: choose new names for another run.
```

The driver uses the literal `http://10.0.0.16:5591/prism-proxy/agent?stream=false`, checks it against the frozen manifest, and disables tools, function calling, workspaces and orders. Pipeline database inputs/writes are mocked. The fixtures are unchanged synthetic EVLT observations; they are not live portfolio or security data. The proxy retains its normal request/session logs.

## Limits

Consistency with supplied evidence does not prove vendor truth, investment merit or future returns. The structured catalog covers supported financial relationships; it does not prove arbitrary qualitative claims. Unsupported questions remain unresolved. Arbitrary tool prose and memory are not automatically promoted into facts. Next-year EPS growth and pending-order reservations remain unknown when not captured, so exact forward PEG and fully reserved headroom are unavailable in those cases.

Explanations can remain repetitive and may include extra, relevant or irrelevant observations. Numerical answers live in the referenced typed claim records, rather than duplicated numbers in prose. Single-evaluator review was not blinded, and the sample contains a small set of synthetic case families. These tests do not establish real-portfolio performance.

No Prism, adapter or client repository was modified by this work.
