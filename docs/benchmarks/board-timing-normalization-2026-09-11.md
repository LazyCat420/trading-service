# From 4/5 to reliable label repair on the frozen cases

The final implementation reached **5/5 structural acceptance in each of two replays** of the same five failures. Four cases now require no model repair. The genuinely missing decision still uses one model call. This is structural recovery, not proof that the model's financial analysis is correct, and the eight deterministic results are not eight independent model successes.

## What prevented 5/5

The prior final failure (row 05) had already corrected `entry_mode`, but regenerated the whole JSON and changed `resolution_condition` from null to a new question. The preservation guard correctly rejected it. Other repeats showed that the failed row can move: the model sometimes copied `research` purpose while leaving its numeric trigger null, or emitted a SELL without the required timing labels.

A small output change alone did not solve it. Retaining the investment persona while requesting a patch scored 3/5 twice. A compact repair-only prompt scored 4/5 and 3/5. The latter eliminated the original question rewrite but still copied the invalid research label; one missing-decision repair also omitted timing fields. All attempts are retained, including these unsuccessful candidates.

## Final change

After normal schema validation, the Board runner considers only existing **HOLD and SELL** decisions with invalid timing labels. It enumerates the contract-valid `entry_mode` / `trigger_purpose` combinations while keeping every other field unchanged. It applies a correction only when there is one uniquely smallest set of label changes. Otherwise it keeps the existing bounded model repair/rejection path.

This means:

* HOLD with `enter_now` and no trigger becomes `watch_only`; its action, sizing, rationale and question do not change.
* HOLD with a research question but no numeric trigger changes purpose to `none`; the research question remains intact.
* HOLD waiting on an existing price monitor becomes `watch_only` and retains the same monitor and threshold.
* An existing SELL can receive its uniquely determined exit labels. The harness does not invent SELL, action, confidence or reasoning when they are missing.
* BUY timing is never selected by this normalizer. Invalid numeric triggers are not removed or replaced. Equally small monitor/research alternatives remain ambiguous and require model repair.

The original artifact, exact patch and normalized artifact are recorded in `artifact.timing_normalized` with rule `unique_minimal_nonentry_labels`. No retry budget was increased. The experimental patch-only model interface was not shipped; the existing model fallback is unchanged.

## Results and integrity

| Retained cohort | Structural acceptance | Actual model repair calls |
|---|---:|---:|
| `live-repairs-timing-patch-r1` | 3/5 | 5 |
| `live-repairs-timing-patch-r2` | 3/5 | 5 |
| `live-repairs-labels-r1` | 4/5 | 5 |
| `live-repairs-labels-r2` | 3/5 | 5 |
| `live-repairs-unique-labels-r1` | 5/5 | 1 |
| `live-repairs-unique-labels-r2` | 5/5 | 1 |

There were 22 new tool-disabled Nemotron calls through the authorized NAS proxy, all retained. The final two replay sets each replayed four original responses entirely locally and made one provider call for row 09. No orders or database writes were performed by these tests. Each model repair stayed within the existing one-call budget.

Comparing every original authored field with the final artifacts in both final repeats found exactly these changes: row 04 entry_mode, row 05 entry_mode, row 06 trigger_purpose, row 11 entry_mode. Research answers, questions, triggers and financial fields were unchanged. The original 12-response corpus and previous reviews remain intact. [The prior audit](board-failure-isolation-2026-09-11.md) remains the record of the earlier 4/5 result and evaluator correction.

The final row 09 repairs still contain financial inconsistencies, and the normalized HOLD artifacts intentionally preserve the original wrong financial claims. Source/metric/date validation and arithmetic coverage remain separate work. Neither 5/5 nor a successful unit test means investment-quality correctness.

## Validation and release

The final full unit suite passed: **7,119 passed, 102 skipped**. NAS verification will be appended after deployment. Focused tests cover unique correction, unchanged data, ambiguity, invalid triggers, exclusion of BUY, and strict preservation on model fallback.
