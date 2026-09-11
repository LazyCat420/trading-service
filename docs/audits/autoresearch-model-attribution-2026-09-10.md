# AutoResearch model attribution audit — September 10, 2026

The recorded tool-evaluation scores do **not** support the explanation that recent AutoResearch used only Nemotron because GLM was unavailable. Recent scored tool traces explicitly identify GLM. The main trading pipeline did use Nemotron on September 9–10, and its Board artifacts had contract failures. These are different populations.

Read-only capture: 2026-09-10 23:09:13.488211+00:00. Day boundaries below are UTC. The capture contains 3,900 scores, 3,782 tool traces, and 1,362 agent-attempt telemetry records over approximately ten days. Scores join on `eval_scores.run_id = agent_traces.id`, as the current scorecard code does; joining to `agent_traces.run_id` would be wrong. 1,024 scores have no matching trace in this capture and remain unattributed.

## Tool-evaluation scores with model attribution

| UTC date | Recorded model | Scored tool rows | Mean score / 100 | Error tool rows |
|---|---|---:|---:|---:|
| 2026-09-04 | deepseek-v4-flash-0731 | 155 | 92.7 | 12 |
| 2026-09-04 | nemotron35 | 121 | 78.8 | 26 |
| 2026-09-05 | GLM-5.3-Flash-EXL3 | 68 | 90.0 | 6 |
| 2026-09-05 | nemotron35 | 582 | 75.3 | 154 |
| 2026-09-06 | GLM-5.3-Flash-EXL3 | 73 | 74.5 | 21 |
| 2026-09-06 | nemotron35 | 377 | 74.7 | 105 |
| 2026-09-07 | GLM-5.3-Flash-EXL3 | 281 | 79.2 | 63 |
| 2026-09-07 | nemotron35 | 69 | 78.1 | 17 |
| 2026-09-08 | GLM-5.3-Flash-EXL3 | 350 | 85.3 | 55 |
| 2026-09-09 | GLM-5.3-Flash-EXL3 | 350 | 91.2 | 26 |
| 2026-09-10 | GLM-5.3-Flash-EXL3 | 450 | 91.7 | 33 |

Recent averages rise from 85.3 on September 8 to 91.2 on September 9 and 91.7 on September 10, all attributed to GLM in this capture. Earlier Nemotron cohorts do have lower scores on some days, but model, task, tool availability and pipeline version changed together. This is observational evidence, not a controlled model comparison.

## Trading-agent failures

On September 9, 100 recorded Nemotron agent attempts contained 21 schema failures; on September 10, 99 contained 22 schema failures. These are attempts, not independent cycles. The separate 72-hour scheduling audit found 42 Board schema failures across 21 cycle/ticker cases, plus one fundamental-report schema failure.

There is direct evidence for harness faults: the adapter's last user message replaced the full task with a retrieval index, which led a Board attempt to search for the company's corporate directors; Board persona examples also omitted required timing fields. A weaker or differently prompted model can be more sensitive to these faults, but the telemetry cannot isolate that causal effect. The task-delivery and contract fixes address observed faults on our side.

## Limits and next validation

The AutoResearch rubric grades individual tool rows using completion status, error text and loop position. It does not measure complete artifact validity, trading returns or learning improvement. A high tool score can coexist with a later rejected Board decision. Successful GLM-attributed rows prove GLM was serving those recorded calls; they do not prove uninterrupted uptime or explain why the main pipeline selected Nemotron.

Keep the frozen learning benchmark and full-cycle validation separate from these historical scores. The live NAS-proxy generation and subsequent paper-cycle check are pending explicit approval after automatic review blocked the probe. No model-routing change was made based on this observational comparison.

Evidence: [attributed score rows and aggregates](autoresearch-model-evidence-2026-09-10.json). The source capture hash is retained in that file. No new LLM calls were made for this audit.
