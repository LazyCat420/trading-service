# Live harness observation — September 7–8, 2026

The cycle completed, but full-cycle validation did **not** pass cleanly: LULU reached a reasoned HOLD, MSFT aborted on a Quant timeout, and optional LULU Valuation failed twice. The run also exposed two concrete boundary defects that were fixed during this task. No order execution was requested; `trade=false` was retained throughout.

## Method and result

The supported cycle queue launched fresh collection and full V3 analysis for LULU and MSFT. The replacement observation `cycle-observe-1788844926` ran from **2026-09-08 05:22:07.181 UTC to 07:01:56.555 UTC**: **99 minutes 49 seconds**. The top-level status is `done`; that means orchestration ended, not that both ticker pipelines succeeded.

| Ticker | Terminal desk | Result |
|---|---|---|
| LULU | PM_DONE | Board HOLD at 68%; synthesis HOLD at 66%, `watch_only`, `monitor`, `board_reasoned` |
| MSFT | ABORTED | Quant exceeded 1,800 seconds; no completed Board/synthesis artifacts |

LULU's decision contract is version 1, valid with no contract errors, `decision_relation=preserve`, and `source_board_ref=board:2cbdfed771abbce82c437f19`. Board and synthesizer delivery receipts both record the decision contract and defense evidence as delivered. Five defense answer records exceeded the bounded context allocation; omissions were explicitly marked in the judge, Board, and synthesizer receipts. The judge retained unresolved risks.

## Passed checks and limitations

- All **21 recorded tool calls succeeded**, including LULU's `whiteboard_annotate` and MSFT's `whiteboard_write`/`whiteboard_annotate`. No replacement-trial cycle-identity permission denial was observed.
- The successful Bull, Bear, Defense, Judge, Board, and Synthesizer stages exercised the sequential debate and attribution contracts. No Valuation artifact was fabricated after its failure. The final synthesis did not explicitly name the failed Valuation stage; absence of that report remains a limitation in downstream failure visibility.
- There are 17 telemetry records: 14 SUCCESS (including a cached regime result and deterministic contradiction shadow), two AGENT_ERROR attempts, and one TIMED_OUT attempt. These are records, not 17 independent model runs.
- **11 provider-stream stalls** reported no data for 300 seconds. MSFT Junior and LULU Fundamental recovered after roughly 17½ minutes each. LULU Valuation exhausted both outer attempts, taking 1,545.05 and 1,480.91 seconds. MSFT Quant timed out at 1,800.05 seconds. Inner provider retries plus an outer stage retry account for substantial delay.
- Serving snapshots showed both one-running/one-deferred and two-running/no-waiting Gold requests while generation advanced. Those observations do not establish a root cause for the stream stalls. They also persisted after MSFT aborted. The streaming adapter was inspected but not changed speculatively.
- Reported token usage totals **893,234**. This is incomplete: failed attempts retain partial cost, and the second Valuation attempt reported zero tokens despite serving activity. Zero is not evidence of zero compute.
- Twelve learning artifact receipts were delivered: seven eligible and five ineligible. The generic reason `degraded_or_repaired` does not prove repair occurred: LULU Junior has quality 87 and no degraded flag, but four data gaps exceed the learning eligibility limit of two.
- News collection and refreshes logged thin-content, unavailable-source, and stored-content fallback warnings. These remain part of the observation, not silently discarded.
- No decision-outcome, strategy-performance, or learning-outcome rows were created for this cycle. One synthetic HOLD analysis row was retained for audit. MSFT's live watch arming was explicitly skipped; synthetic watch guards and disabled trading prevent this observation from becoming a live order request.

## Defects found and corrected

1. **Observation identity in tool transport.** The first trial, `cycle-observe-1788843390`, was stopped after a cycle-ID extraction defect caused a whiteboard permission denial. Agent-service commit `5736438` preserves trusted opaque cycle IDs instead of accepting only production IDs. Sixteen targeted transport tests, typechecking, and the 670-test deployment gate passed. The affected agent-service container was deployed and its running code and health checked before the replacement trial. The replacement's live whiteboard calls exercised the fix.
2. **Synthetic episodic learning.** At completion, the replacement wrote one raw `episodic_observations` row and one `episodic_memory` row. These were backed up and removed by their exact IDs plus cycle ID. The observation was still unpromoted and had no canonical evidence references. Trading commit `c9ddbffc` blocks synthetic writes, excludes older synthetic rows from episode/consolidation readers, rejects explicitly supplied synthetic consolidation evidence, and rejects canonical memories with synthetic lineage. **41 targeted checks passed**, including real Mongo reader/writer isolation and preservation of production/legacy rows. The targeted NAS deployment completed; the running container reports commit `c9ddbffc`, state `running/healthy`, and HTTP `/health` returns OK. A post-deploy check exercised both synthetic writers and confirmed zero rows for its sentinel and the cleaned observation cycle, plus the consolidation exclusion filter.

The post-run memory fix is validated by targeted tests and deployment checks; the 100-minute full cycle was **not rerun after that fix**. The stream-stall and nested-retry behavior remains unresolved.

## Ticket comparison

The separate [frozen ticket/no-ticket protocol](ticket-harness-protocol-2026-09-07.md) uses fixed synthetic evidence and actual runner contracts with fixture-backed tools. It began only after this cycle was terminal and Gold reported zero running and waiting requests. Initial benchmark startups were rejected by pytest's outbound-HTTP guard before any inference; the explicit live-HTTP fixture corrected that setup. The first inferred cohort was interrupted by an inherited 300-second pytest timeout in workflow 7; it is retained separately. The complete replacement cohort uses an explicit 1,900-second whole-workflow deadline and unchanged 600-second role deadlines. Results are reported separately and must not be treated as a full-cycle latency or profit benchmark.
