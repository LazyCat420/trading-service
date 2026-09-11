# Board failure isolation and repair audit

The five saved failures were reproduced, a production parser defect was isolated, and a bounded repair change was tested against the actual model. Final structural recovery was **4/5**, versus **0/5** with the original harness in this selected-failure replay. This is not a claim that four investment decisions became factually correct.

The previous audit also contained an evaluator error: **all 12 original raw responses contain three research answers**. The benchmark reader extracted `final_decision` and discarded its sibling `research_answers`, leading to false missing-answer conclusions. The original response files, requests, review, and runner remain unchanged. [The corrected review](evidence/board-failure-isolation-2026-09-11/original-review-corrected.json) considers both the answers and the rationale and supersedes those conclusions. It finds additional answer-level arithmetic errors, so it still does not establish a learning benefit.

## The five original failures

| Original row | Reproduced defect | Further factual issue |
|---|---|---|
| 04, missing history / stale memory | HOLD with `enter_now` | Moves the August 17 volume question to September 3 |
| 05, missing history / no method | HOLD with `enter_now` | RSI 14 instead of 46; mixes operating margin, gross margin and ROIC |
| 06, missing history / current method | Research trigger purpose with no structured numeric trigger | A missing historical observation is being confused with an executable numeric watch |
| 09, held deterioration / stale memory | Research answers and rationale, but no action/confidence/reasoning decision fields | Operating margin 28% instead of −2%; invented historical leverage |
| 11, conditional entry / no method | HOLD with `enter_on_condition` and monitor purpose | Calls conditional reward/risk 3 while also calculating 30/5 = 6 |

The direct benchmark already unwrapped named decisions for entry validation. The production parser did not: rows 04/05/06/11 have valid outer JSON containing a complete `final_decision` object, yet production classified the wrapper as WRONG_SHAPE. It spent its one repair attempt asking for a top-level object instead of correcting the actual entry fields. This was a harness defect, separate from model-generated invalid field combinations. Row 09 genuinely lacks the decision and must not be reconstructed by parser defaults.

Initial deterministic tests used the extracted artifacts and demonstrated that existing contract correction accepted supplied valid corrections. Replaying the **raw responses** exposed the wrapper discrepancy. The regression tests now use raw responses and preserve sibling answers.

## Experiments and retained attempts

All new model requests went serially to the previously authorized NAS proxy, using `nemotron35`, no tools, no orders, and the original synthetic evidence. There were 30 HTTP-200 model calls: ten first-pass paired calls and twenty harness repair calls. Every request and response is retained. Five initial test-fixture network blocks reached no provider; their records remain in `blocked-network-preflight` and are excluded from model counts. A provider-success assertion was added before the actual live experiment.

The ten first-pass calls repeated the five original requests with either the original prompt or an appended output-contract clarification. Both arms supplied all three research answers in all five raw responses. Minimum schema plus entry validity was 1/5 for the original prompt and 5/5 for the clarified prompt. However, the clarified outputs still confused gross and operating margins and introduced `15/20 = 1.5`. That broad first-pass prompt candidate was **not promoted**. See [corrected paired review](evidence/board-failure-isolation-2026-09-11/paired-review-corrected.json).

The repair experiment replayed each original raw first response through `run_v3_agent`, then used its actual repair prompt for one live model call. Frozen evidence and questions were delivered as the desk's data report. Mongo persistence was blocked or mocked; this was not a live trading cycle. Each repair cohort was frozen separately and used a fresh conversation. Tests asserted that the provider was reached and that an accepted artifact passed entry validation; a passing experiment test does not mean the model succeeded.

| Harness version / retained directory | Accepted decisions | Finding |
|---|---:|---|
| Original / `live-repairs` | 0/5 | Named envelopes consumed shape repair; missing-decision repair still omitted `reasoning` |
| Parser fix / `live-repairs-after-parser` | 0/5 | Correct repair branch reached, but model copied invalid entry fields |
| Explicit correction / `live-repairs-explicit-correction` | 3/5 | Rows 04, 05 and 11 recovered; research-trigger and missing-decision cases remained |
| Final / `live-repairs-final` | 4/5 | Rows 04, 06, 09 and 11 recovered structurally; row 05 changed its resolution question and was rejected |

These are successive development cohorts, one attempt per selected failure per version. They are not independent large samples, and 4/5 is not a general reliability estimate. [Repair summary](evidence/board-failure-isolation-2026-09-11/repair-summary.json) records every outcome and rejection.

## Production changes

* Recover only an exact Board envelope containing `final_decision` and optional `research_answers`, with all required decision keys inside. Preserve the answers. Do not unwrap conflicting answers, extra top-level fields, arbitrary nested fragments, or partial decisions. Normal schema and entry validation still run.
* Tell shape repair explicitly that action, confidence and reasoning are required and that `rationale` or answers alone are not a replacement.
* Tell contract correction to change the invalid timing fields, rather than copy them. Explain that HOLD uses watch-only and that a research question alone does not require a numeric trigger.
* Extend correction preservation checks to research answers and resolution questions. Existing preservation of action, confidence, rationale, size, stop, target and cited override evidence remains. The repair limit and original deadline are unchanged.

## Remaining failures and limits

The final row 05 response had valid timing but added a resolution question where the original had null. The harness rejected it as `correction changed resolution_condition`; the fix does not silently accept the changed question. Different repeats recovered different rows, showing continued model variability.

The final row 09 schema repair produced a SELL with proper decision fields but still used gross-margin history as operating margin. Its requested research answers were absent from that repaired output. Rows 04 and 11 preserve their original wrong date or arithmetic claims because a timing correction is not authorized to rewrite financial evidence. Schema acceptance must not be reported as factual correctness. A future factual verifier needs source/metric/date/unit checks and calculation coverage; the existing arithmetic guard's lack of recognized expressions is not a pass. Adding broader prompt instructions alone did not solve this in the paired experiment.

## Validation and release

Full unit suite after the parser change: 7,102 passed, 102 skipped. After the final repair and preservation changes, the related suites and frozen live-response regressions passed: 71 passed, five deliberately skipped opt-in network experiments. All five final live experiment checks reached HTTP 200; four model outputs were accepted and one was rejected as described above. The raw original evidence is unchanged; hashes are retained in `original-evidence-hashes.json`.

Deployment verification will be appended after the targeted `trading-service` transfer, restart and health check. The preflight found the last pipeline cycle completed and the existing container healthy.
