# Ticket harness comparison — September 8, 2026

Ticket mode delivered usable answer records more reliably in the complete replacement cohort. It did **not** improve the correctness of the simple question answers, eliminate role failures, or establish a consistent latency advantage. This supports retaining the existing research-question contract for handoff and tracking; it does not establish better investment decisions or evaluate the proposed unified inbox.

## Primary result

Four paired workflows, eight workflows total, 24 role attempts: Fundamental → Quant → Board. Two repetitions cover complete evidence and a date-specific volume question that the supplied current-only snapshot cannot answer. The actual V3 prompt assembler, parser, whiteboard persistence, answer verifier, and ticket completion/defer receipts run against a disposable Mongo database. Tools return frozen fixture evidence; model calls use Gold's `GLM-5.3-Flash-EXL3`.

| Measure | No tickets | Tickets |
|---|---:|---:|
| Contract-verified correct answers / answerable questions | 5 / 10 | 10 / 10 |
| Correct answer content, including prose / answerable questions | 10 / 10 | 10 / 10 |
| Correctly retained historical unknowns | 2 / 2 | 2 / 2 |
| Usable, non-degraded role artifacts | 10 / 12 | 11 / 12 |
| AGENT_ERROR role attempts | 2 | 1 |
| Tool calls | 26 | 17 |
| Tool errors | 4 | 5 |
| Reported input tokens | 229,102 | 184,080 |
| Reported output tokens | 19,753 | 20,978 |
| Mean workflow wall time | 207.0 s | 204.2 s |

All three AGENT_ERROR attempts exhausted the six-turn replay allowance without a usable final artifact. They remain in the counts and timings. The other research artifacts returned DATA_GAP because the fixture deliberately omits broader financial/technical inputs; those are valid artifacts with missing data, not clean full-research successes. All eight Board artifacts returned SUCCESS/HOLD. The pytest result, **8 passed**, means the driver completed all workflows, not that every model role passed its task.

Tickets completed **10 answered, 2 deferred, 0 delivery pending**. No-ticket queue completion is not a meaningful metric. In two control workflows, otherwise correct answers appeared under `peer_research_answers` or as a JSON string inside `sub_analyses_requested`; the real verifier correctly did not accept them as top-level `research_answers`. Both arms were instructed to return the answer format. Ticket mode additionally carries the production claimed-question contract, so this compares the complete contract against unassigned notes; it does not isolate queue storage from prompt salience.

Ticket mode used about **19.7% fewer input tokens** and fewer tool calls, but more output tokens and one more tool error. Usage was reported for every recorded primary-cohort model turn. These are reported token counts, not dollar costs or uncached-compute estimates.

## Paired timing and repeated work

| Case / repetition | No tickets | Tickets | Ticket minus control |
|---|---:|---:|---:|
| Complete / 1 | 172.7 s | 214.1 s | +41.4 s |
| Complete / 2 | 201.4 s | 208.5 s | +7.1 s |
| Missing history / 1 | 179.2 s | 231.2 s | +51.9 s |
| Missing history / 2 | 274.7 s | 162.8 s | −111.9 s |

Tickets were slower in three of four pairs. Failed roles can terminate early, and shared hardware plus wall-clock measurements make these diagnostic timings, not a reliable speed benchmark. The nearly equal means should not be read as a ticket speed improvement.

There was one exact duplicate non-whiteboard research call in the control and none with tickets. However, Quant repeated six already-answered structured records in the ticket arm. Board also restated answers; that is recorded separately and is not automatically wasted research. Tickets did not eliminate redundant answer production.

## Reasoning review

Manual review checked the question answers against the fixture and separately inspected Board reasoning. Correct quotes and schema-valid artifacts did not guarantee correct conclusions. Concrete or unsupported claims were identified in seven of eight Board outputs, including:

- Calling price 100 the geometric midpoint of support 95 and resistance 115.
- Calculating range position as 41.7% instead of 25%.
- Deriving PEG from revenue growth when EPS growth was not supplied, or reversing the ratio.
- Using the price support 95 as an RSI trigger value of 95, and claiming about 14 observations would make SMA-200 computable.

The per-workflow review is in the [results data](ticket-harness-results-2026-09-08.json). This was a documented manual check, not an exhaustive production error-rate estimate. Both modes preserved the intentionally missing historical fact; neither was shown to make better investment decisions. The setup forbids live orders and asks for observation only; all Boards chose HOLD. Those identical actions are not evidence of equal investment performance.

## Interrupted pilot and limits

The first inferred cohort completed six workflows, then pytest's inherited **300-second whole-test timeout** killed workflow seven; workflow eight never started. All outputs were retained. Its six completed workflows tied at **8/8 accepted answer records per arm**, which shows the observed reliability difference is not stable enough to generalize from this small sample.

The whole eight-workflow cohort was restarted after explicitly setting a **1,900-second workflow timeout**. There was no best-of selection or selective replacement. The primary cohort kept the same evidence, role prompts, model parameters, tool limits, and counterbalanced order. Earlier startup failures were pytest's HTTP guard at model discovery and occurred before inference.

The [protocol](ticket-harness-protocol-2026-09-07.md) and runner hashes remained unchanged during the replacement cohort. Limits were output 4,096 tokens, six tool turns per invocation, 600 seconds per role, temperature/min_p zero, and thinking disabled. Junior, regime, and debate inputs were fixed; chart persistence was disabled to prevent outside-fixture price retrieval. Production uses different limits and real tool transport. No profit, live execution, or unified-inbox claim follows from this experiment.

The [deployed full-cycle observation](harness-live-observation-2026-09-07.md) is reported separately: it found and led to deployed fixes for cycle identity and synthetic-memory isolation, while exposing unresolved provider stalls, nested retries, and incomplete failed-attempt cost accounting. The memory fix was tested and deployed before the replacement benchmark; that full cycle was not rerun after the memory fix.

Both benchmark databases and the memory-regression database were removed. Raw outputs and the interrupted database backup remain local; the committed results retain aggregate metrics, per-workflow outcomes, manual reviews, delivery hashes, and raw-output hashes without raw prompts.
