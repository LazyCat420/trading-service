# Corrected AutoResearch attribution audit — September 10, 2026

**Correction:** the earlier report incorrectly treated grading dates as execution dates and compared tool-call scores with the AutoResearch dashboard's combined score. Its claim that September 10 scores proved GLM was serving that day is withdrawn. The supplied records do not establish current GLM availability.

## What the dates actually show

The read-only capture contains 3,900 tool-evaluation scores, 3,782 tool traces, and 1,362 agent-attempt rows over approximately ten days. Scores join on `eval_scores.run_id = agent_traces.id`; 1,024 scores lack a matching trace in that capture and remain unattributed.

| Grading day (UTC) | Underlying execution day (UTC) | Recorded model | Scored tool rows |
|---|---|---|---:|
| September 8 | September 6 | GLM | 350 |
| September 9 | September 6 | GLM | 113 |
| September 9 | September 7 | GLM | 237 |
| September 10 | September 7 | GLM | 340 |
| September 10 | September 8 | GLM | 110 |

The quoted September 10 mean of 91.7 belongs to delayed grading of September 7–8 tool calls. It is not the September 10 dashboard score and does not show GLM running on September 10. The 333 September 9 and 306 September 10 tool traces recorded as Nemotron had **zero matched scores** in the frozen capture. Their absence from the score average reflects the grading backlog, not evidence that Nemotron was unused.

## The score visible in AutoResearch

The latest completed report in the follow-up read is `ar-ebfc8ec68b10`, for `cycle-v3-1789076658` (CRWV): data quality 93.9, decision quality 50.0, LLM performance 64.8, overall 69.6. Its named issue is 2 failures among 7 agent attempts. Its decision cohort explicitly says `cold_start`: zero eligible resolved outcomes and at least three required. The 50 is a neutral insufficient-evidence default, not a measured judgment of Nemotron.

The LLM score combines this cycle's attempt failures with historical judge and tool averages. Prior code selected the latter by grading time, allowing old model executions to enter a new window. It also graded only 50 oldest pending tools after a model-dependent report finished, so a failed report could prevent even deterministic grading. These facts explain why model availability cannot be inferred from that score.

Board schema failures are independently observed: the 72-hour audit identified 42 rejected Board attempts across 21 cycle/ticker cases. The adapter replaced the full task with a retrieval index, and persona examples omitted contract fields. Those are harness defects regardless of which model was more sensitive to them. No controlled experiment has isolated a Nemotron-versus-GLM quality effect.

## Corrective changes

- Record execution and grading timestamps separately. Select the seven-day tool window by execution time and expose pending counts and delay.
- Grade the current cycle first and drain a bounded historical batch before model-dependent reflection. Deterministic grading works with one or zero model endpoints online.
- Retain requested model/provider separately. New tool rows stay unconfirmed until the stream reports model identity; confirmation is scoped to the exact attempt. Legacy labels remain explicitly unverified.
- When model-not-found recovery changes the selected model, change the provider with it and honor an explicit endpoint override. Ordinary routing already supports one available endpoint; regression tests cover that behavior.
- Show the selected cycle's recorded models, failures, tool counts and both clocks in AutoResearch. Mark a cold-start decision score as unmeasured and explain its neutral contribution to the saved overall score.
- Preserve historical score records. Do not silently rewrite old reports as if the corrected instrumentation had produced them.

Evidence: [original score rows, joins and corrected date cross-tab](autoresearch-model-evidence-2026-09-10.json). The original capture hash remains in that file. New test and deployment results are recorded after validation.

## Repair and validation, September 10 PDT

Backend revision `99f3f825` was transferred and restarted on the NAS; the container is healthy and its HTTP health and score-evidence endpoints returned 200. The deterministic repair graded 906 pending stored traces in batches of 500 and 406, reducing the seven-day execution window backlog from 906 to zero. No model or trading execution was invoked by this repair. All 33 retained tool traces for `cycle-v3-1789076658` now have grades, and the endpoint exposes seven agent attempts. Historical reports were preserved.

Validation: full backend suite 7,080 passed, 97 skipped; focused regression suite 60 passed; three integration tests passed against an isolated real Mongo database. Frontend production build and a muted browser interaction check passed. The browser test verified the evidence expansion and absence of page errors.

Repair evidence: [before and after counts](autoresearch-backlog-repair-2026-09-10.json), [NAS API and client proxy responses](autoresearch-score-http-verification-2026-09-10.json).

Live Board delivery, a fresh paper cycle, and the frozen learning benchmark remain unvalidated: automatic approval review rejected transmitting the repository Board instructions and cycle/test context to the NAS proxy. The specific authorization request is pending. Offline regression results and the stored-trace repair do not establish live model delivery or trading decision quality.

Frontend revision `aefbce81` was subsequently transferred and restarted successfully. Final NAS inspection: trading-service `99f3f825`, trading-client `aefbce81`, and lazy-agent-service `83bbd73` all running and healthy. Final HTTP checks passed for both health endpoints, the client page, score evidence through both backend and client proxy, retained trace snapshots, and OTLP export; cross-cycle blob access correctly returned 404. The first frontend Docker build failed on a transient Google font download; the targeted retry completed successfully. Deploy-kit reported existing edge DNS conflicts while leaving its Caddyfile unchanged; service availability passed.

## Subsequent approved live validation

The user subsequently approved the specific Board instructions and test/paper-cycle context transmission to the NAS proxy. The delivery test, normal paper cycle, automatic audit and separately labeled frozen Nemotron method/memory cohort have now run. Full task delivery passed; the cycle completed with two recovered artifact failures; all 40 cycle tool traces were graded automatically. The frozen outputs still contain reasoning and entry-contract errors, so no learning-quality improvement is claimed. See [the complete validation report](../benchmarks/board-memory-proxy-2026-09-11.md). Earlier pending-approval statements above describe the state at that earlier capture.
