# Approved Board delivery and frozen memory validation

The user explicitly approved sending repository Board instructions and synthetic test/paper-cycle context to the NAS proxy on September 10 PDT. The tests below ran after that approval.

## Delivery

Nemotron returned HTTP 200, valid JSON, and both markers from the ends of an 8,163-character task in 6.09 seconds. The retained provider receipt says `task_delivery=exact`, names `nemotron35`, and records the removal of unpermitted tools. The test requested no tools or orders. Reported usage: 4,307 input and 112 output tokens.

## Frozen method/memory cohort

Twelve serial attempts reused the four cases from `decision_quality_v1.json`: headroom, missing history, held deterioration, conditional entry. Each case received current reviewed methodology, no methodology prefix, or current methodology with explicitly stale unrelated memory. Order was counterbalanced by case. Expected answers were excluded from prompts. All arms used the same Nemotron model, current Board persona and entry contract, frozen facts/questions, one turn, 8,192 output tokens, no tools, and a 180-second timeout. All attempts and the pre-inference manifest are retained; there were no retries or after-the-fact prompt repairs.

This is a separately labeled NAS-proxy cohort. It changes model and transport from the earlier GLM replay and does not replace that result. Tools are disabled, so it isolates prompt/memory behavior rather than production calculator use or full-cycle reliability. It tests reviewed guidance and stale-memory exposure, not whether newly generated learning has improved performance. No learning proposals were promoted.

| Arm | Attempts | Minimum schema valid | Entry contract valid | Usable with no identified material factual error | Fully answers required work |
|---|---:|---:|---:|---:|---:|
| Current guidance | 4 | 4 | 3 | 0 | 0 |
| Guidance omitted | 4 | 4 | 2 | 1 | 0 |
| Stale-memory exposure | 4 | 3 | 2 | 1 | 0 |

All 12 calls reported Nemotron, and retained provider snapshots contain the exact requested system and user text. Thus missing prompt delivery does not explain these observed errors.

Examples: current guidance called the 95–115 range position of price 100 one-third instead of 25%; confused gross margin 30% with current operating margin -2%; and gave reward/risk 1.5 and 3.5 where the supplied prices imply 0.75 and 6. The no-method arm also confused financial fields and used an incompatible HOLD entry mode. The stale-memory arm produced one artifact with answers/rationale but no decision fields. Eleven of twelve omitted the requested research_answers array; some answers appeared only partly in the rationale. A correct HOLD or SELL label does not compensate for false supporting claims.

This small sample provides no demonstrated learning-quality improvement. Four cases are the independent units; one unblinded reviewer performed the factual review. Source evidence, exact outputs, hashes, per-artifact correct observations, errors and omissions are in [the retained cohort](evidence/board-memory-proxy-2026-09-11/review.json). A normal paper cycle separately checks the deployed tool and contract-repair paths.

Reported usage was present for all 12 attempts: 46548 input tokens and 9859 output tokens. Summed request duration was 153.83 seconds. Shared hardware and different output lengths preclude a speed comparison.

A post-run, read-only coverage check applied the existing explicit-operand arithmetic guard to copies of all 12 retained artifacts. It recognized zero checkable expressions and therefore flagged zero errors in each. This is lack of coverage, not verified arithmetic correctness; the manual errors above remain. The original responses are unchanged. See `arithmetic-guard-coverage.json`.

## Normal paper cycle and automatic audit

`cycle-v3-approved-033e8fffee` ran one full MSFT cycle through the normal deployed worker. It completed in 2,193,721 ms (36.6 minutes), including data collection. All 11 model-driven stages produced accepted artifacts. The separate deterministic contradiction-shadow record also reports success; it is not a twelfth model generation. The cycle reported GLM in fresh response metadata; the frozen cohort above explicitly used Nemotron. These are separate validations, not a controlled model comparison or evidence about historical uptime.

The Bear first returned no final content and recovered through the configured tool-free repair using six retained tool results. The Board first narrated without producing an artifact and also recovered. Both failures and repairs remain recorded. There were no terminal model-role failures, but this was not an 11/11 first-pass result. The Board's initial provider request contained its exact 10,387-character system instructions and 39,419-character user task. All 38 retained tool-enabled provider payloads have exact task-delivery receipts. Repair prompts/responses are retained separately; these do not supply an additional provider-payload receipt for the tool-free chat path.

The final Board and synthesis agreed on BUY, enter_now, 1.5% sizing and a monitor trigger, with synthesis declaring preserve and referencing the current Board artifact. The paper executor produced exactly one linked order/fill: 2.2441856772452096 simulated MSFT shares, value $1,105.1849647760796. This was the verified paper-trader implementation, not a live broker order. Its stored reference quote was 6.76 hours old; the simulated fill is not evidence of an executable live-market quote or real fill quality. The cash, position, order, fill and lot ledger checks found no inconsistencies before or after; cash residuals remained below $0.000000001.

All 40 tool traces were graded automatically before the model-dependent audit completed, with zero pending. Their mean was 93.25 and all 40 have response_metadata attribution. AutoResearch `ar-79808ac6f1d5` completed with `llm_execution_time_v2` evidence and an execution-time seven-day window, which separately shows older unverified GLM/Nemotron records and this cycle's confirmed records. The dashboard's overall 83.8 is not proof of investment quality or learning improvement. Its decision component 62.5 combines the neutral unresolved-outcome prior (50, weight 75%) and a per-cycle judge score (100, weight 25%); zero mature eligible outcomes exist. The score's call count of 12 includes the deterministic telemetry record, while the model-role count here is 11.

The recorded model-role token usage sums to 620,410, with no partial-cost flags in those rows. This does not include every background audit call. The collection phase caused one 20-second HTTP status timeout while stored source traces kept advancing; it recovered without interruption. Three NAS containers remained healthy after the cycle, and the client, health, trace snapshot and OTLP endpoints passed HTTP checks.

Reasoning remains a limitation in the live run as well. Quant's stored warning says its volatility premium was corrected from 0.28 to 0.3007 and its conclusion was stale; synthesis still referred to +0.28. This observation and the frozen errors above prevent interpreting accepted artifacts or high tool scores as verified reasoning. No retrospective change was made to the paper decision or stored model outputs.

The live report also exposed ambiguous UI wording about the neutral prior. Frontend commit `2fb7c84c` clarifies that the saved decision component may include a per-cycle judge score and is not measured outcome performance. Production build passed; deployment verification is recorded below when complete.

[Retained cycle, ledger, grade, repair and model evidence](evidence/approved-paper-cycle-2026-09-11.json).

Frontend deployment completed: NAS revision `2fb7c84c` is running and healthy, and its built JavaScript contains the corrected explanation. Backend `99f3f825` and adapter `83bbd73` also remain healthy. Final score-evidence requests to the backend and frontend proxy returned HTTP 200, with all 40 cycle traces graded and zero pending. No validation request remains blocked by approval.
