# Financial cycle hardening: implementation and verification

This is the follow-up to the supplied **Trading Cycle Financial Evidence Validation Review & Next-Steps Implementation Plan**. The earlier release and benchmark are the baseline, not evidence that this follow-up was implemented. Baseline checkout: `ff9a14a9`, which already contains the other developer's catalog-ID repair prompt and nonempty-selection instruction. Those changes are preserved. Direct pre-deployment verification also found `ff9a14a9` running on the NAS, healthy; the old report's `f5605166` was historical, not the current deployment.

## Audit corrections and acceptance criteria

- The old 7,257-test result applies to `b374c5e9`, not the new checkout. Run the relevant tests and full suite on the final implementation and record their actual results.
- A scripted correct repair proves runner behavior, not that a live model stops emitting empty or invented IDs. Run a new 12-case cohort and a saved-failure repair; retain failures and measure first-response and final acceptance separately. The suggested 11/12 first-response target is an empirical target, not a guarantee.
- A frozen-initial repair cohort always starts with an old failed answer. Its zero first-response count is not a measure of fresh model latency or regression.
- `RUN_MODE=paper` does not disable simulated orders. Staging must use the actual `trade=False` cycle parameter and independent order-call guards.
- The service finishes at `done`. Observe its real state transitions; do not fabricate `completed → idle` or reset the production singleton for a test.
- Source builders consume stored market data. A source capture proves real stored-data integration, not a successful fresh Polygon/Finviz/SEC collection. Retain original dates and distinguish those claims.
- There is no real brokerage integration here. Validate a copied paper portfolio without claiming real broker reservations. Missing reservations and next-year EPS observations remain unknown.
- No claim of guaranteed financial correctness or profitability follows from the sampled tests. Execution validation remains independent and fail-closed for detected evidence errors.
- Do not roll back by resetting another developer's checkout. If necessary, prepare a targeted revert preserving their work and deploy the validated revision through deploy-kit.

## Implementation

1. Renderer diagnostics enumerate actual allowed IDs and relevant evidence candidates for the failed question. The model must make a new selection; rejected artifacts, action and confidence are not silently rewritten.
2. Missing-field tests reproduce the production catalog's absent volume-history step. The evidence builder now represents a missing comparison explicitly as unknown. A known five-session trend is computed from the captured dated daily volumes. A lower five-day average than the prior fifteen does not establish a decline across each of the five sessions.
3. The technical price/volume query now uses the canonical single-vendor selector; the repository's existing unpinned-query ratchet decreased from two to one for this module. The technical baseline preserves the price observation's date/source separately from the indicator date. Volume computation keeps session positions intact; missing volume cannot shift an older observation into the latest five or relabel ten samples as twenty.
4. `scripts/test_live_financial_record.py` runs the real production builders, blocks database writes, retains source dates and checks fact types/catalog references. Empty snapshots fail the probe.
5. `scripts/validate_financial_cycle.py` runs the real cycle service/orchestrator and model in a temporary database populated with bounded copies of real source rows. It disables collector refresh, agent tools, orders, external notifications and post-cycle review. It records actual states, persisted decisions, model calls and cleanup. These restrictions limit what the staged result proves.

6. The first full real-data cycle exposed an additional integration defect: an empty financial question list was confused with a debate-frame topic (`[DATA_SUFFICIENCY]`). All four Board attempts repeated an invalid question identity. Primary and repair prompts now enumerate the authoritative question IDs and explicitly require `research_answers: []` when the record has no questions. Debate topics remain available for reasoning, but cannot become invented research IDs. A new runner regression exercises this exact rejection and repair.

7. The next real cycle accepted the Board but exposed a persistence defect when synthesis failed: the fallback appended an operational note to code-verified reasoning. That made an otherwise consistent Board artifact fail its final audit. Fallback now preserves the reasoning verbatim and records the note in `fallback_note`. A regression checks saved authored fields, the recomputed audit, and the execution boundary. The staging success predicate now also requires evidence version 1 and no execution errors.

Implementation batches: `90a2bc5d` (source/diagnostics/harness), `2194e3a6` (real-cycle question scope), `f55811cb` (fallback persistence). Model selections and financial audit requirements are preserved; no rejected answer is auto-filled.

## Validation status

Implementation and final code validation are complete. NAS deployment verification is recorded separately below.

- Fresh baseline financial tests: 125 passed.
- Missing-data regression before the fix: 4 failures, each caused by the absent `volume_five_session_trend` fact.
- Affected tests after the implementation: 95 passed (financial renderer/evidence/runner and technical fabrication guards).
- First real AAPL source probe: 24 known facts; original close/indicator date September 11 and fundamentals September 8. After volume capture: 25 known facts. No database write attempts.
- First new synthetic cohort: 12/12 financially consistent, 7/12 first-response passes, 17 endpoint calls. Saved failure 08 repaired in one new call. Provider snapshots independently confirm all 18 requests.
- Every first-cohort explanation/answer was manually reviewed; 13/13 passed current-code execution-boundary checks. Forged passes cannot admit changed claims or order actions. Some answers include excessive but true source observations; consistency is not concise or useful investment reasoning by itself.
- First complete new unit run: 7,282 passed, 114 skipped in 351.20 seconds at `90a2bc5d`; superseded by the subsequent question-scope fix.
- The first real cycle after routing correction completed its scheduler lifecycle but aborted at the Board because of the invented question ID. It is a failed integration result, not a success. No orders were attempted and its temporary database was removed.
- Final question-scope regression suite: 118 passed. Full suite at `2194e3a6`: **7,285 passed, 114 skipped**, 236 warnings, 335.16 seconds; superseded by the fallback fix.
- Final fresh synthetic cohort: **11/12 financially accepted, 8/12 first-response passes, 16 endpoint calls**, with 16 matching provider receipts. This falls below the proposed 11/12 first-response target. Case 03 initially requested BUY size 2.5 against 0.6 headroom; its proposed HOLD repair omitted reward/risk from the required question answer. The repair was rejected and the original artifact retained and blocked. No replacement rerun hides this failure.
- All 12 final explanations and 36 answers were reviewed. Current execution-boundary verification passed all 12 cases: 11 accepted outputs remained consistent; the unresolved case remained blocked. Tampered actions, claims and forged saved passes were rejected. Accepted final cases were HOLD; the earlier cohort and frozen repair also exercised SELL.
- Final-code offline replay of all 25 retained assessments passed their expected acceptance/blocking checks, without new inference calls.
- Across the two fresh cohorts and saved-failure repair there were 25 assessments, 34 new endpoint requests and 34 matching provider receipts. These reuse case families and are not 25 independent scenarios. Staging and routing probes are additional requests.
- Real-data run 4 correctly blocked the corrupted fallback artifact (`HOLD_POLICY_BLOCKED_FINANCIAL_EVIDENCE`), with zero order attempts and cleanup complete. Focused fallback/runner tests after fixing it: 21 passed. Final full suite at `f55811cb`: **7,286 passed, 114 skipped**, 236 warnings in 329.41 seconds. Real-data run 5 **passed**: actual `idle → starting → running → done`, persisted evidence version 1/reasoning version 2, 34 checked claims, consistent audit, no execution errors, zero order attempts or staging orders, no production write attempts, and temporary database removed. Synthesis failed its separate attribution/preservation contract; the corrected Board fallback preserved its accepted model-authored fields. This validates the full persistence path, not independent synthesis success.

The staging transport's first attempt used an invented agent identifier and received HTTP 500. A one-field probe using the known route returned HTTP 200; the harness now uses the production agent-ID resolver. Another development run was interrupted when the source implementation changed. All attempts remain identified separately. Model calls exceeded 30 seconds in several research/debate stages, so the supplied plan's sub-30-second assumption is not supported.

## Release verification

Validated runtime commit: `f55811cb`. Targeted NAS deployment and post-restart source/health verification are pending.

## Evidence retention

The committed `evidence/financial-hardening-2026-09-11/release-validation-summary.json` contains sanitized results and source hashes. Raw prompts, responses, provider receipts, full logs and development-attempt receipts remain local and uncommitted. They include the final `/tmp/financial-aapl-cycle-r5.json` receipt whose hash is recorded in the summary. Raw trading data was excluded following automatic approval review of repository publication.
