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
