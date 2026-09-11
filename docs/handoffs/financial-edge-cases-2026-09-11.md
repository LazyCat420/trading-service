# Financial decision edge cases: release and next-developer plan

All six requested changes are implemented and validated. NAS deployment verification is pending and will be recorded below. The fresh audits accepted **24/24 first responses**. The user's exact previously blocked case 03 now passes by retaining its original validated reward/risk selection. A separate fresh repair of that saved initial response produced repetitive, incomplete JSON and correctly remained blocked. That remaining failure is the first follow-up for the next developer.

## Scope and implementation

| Requested change | Implementation and verification |
| --- | --- |
| 1. Explicit decision budget | `financial_evidence.decision_budget` supplies the minimum of known concentration, cash and per-order bounds, reservation status and source hash. The validator checks the model's actual positive BUY size independently of selected rationale. Strict paper execution rechecks current portfolio capacity and pending orders and refuses clipping or changed sizing. |
| 2. Targeted repair | `financial_repair.merge_repair` accepts a versioned patch or a compatible full replacement. It retains validated answers to unchanged questions from the **original model selections**, compares aliases by underlying fact references, and records preservation. Changed plans require explicit reselection of dependent answers. Conflicting authored prose/facts and unknown IDs still fail. |
| 3. Compound question coverage | A generated component checklist identifies every required calculation. Current and hypothetical reward/risk are separate components; range position remains required after HOLD. Missing components produce question-specific errors. Checklist IDs are valid source IDs, avoiding an initially observed copying error. |
| 4. Price versus RSI | The existing explicit price/oscillator relationship is enforced through complete component coverage and nonempty evidence selection. A new scenario supplies support 95 and RSI 95 to verify that identical numbers do not make the quantities interchangeable. Unavailable facts remain distinct from empty selections. |
| 5. Memory isolation | Retrieval rejects another ticker's records; decision-context delivery rechecks exact ticker, validity dates and source eligibility. Memory provenance survives retrieval. Untyped historical text is excluded from financial decision prompts. Budget inputs come exclusively from captured snapshots. |
| 6. Separate measurements | Attempt records and summaries separate initial acceptance, repair success, prevented/final answer regressions, action mix and execution checks. Missing measurements are not zero, and replayed HTTP receipts are not counted as new requests. |

The implementation does not choose a replacement action, silently resize a rejected purchase, raise a confidence score, or invent a missing fact. A valid smaller BUY remains possible. `calc_proposal_fits` is specifically the supplied **concentration** scenario; the complete current budget also checks cash and per-order limits. Do not confuse that single calculation with order authorization.

Primary files:

- [Evidence and budgets](../../app/v3/financial_evidence.py), [claims validator](../../app/v3/financial_claims.py), [renderer](../../app/v3/financial_reasoning.py).
- [Repair merge](../../app/v3/financial_repair.py), [attempt metrics](../../app/v3/financial_metrics.py), [runner](../../app/v3/agent_runner.py).
- [Memory eligibility and delivery](../../app/services/memory/retriever.py), [orchestrator](../../app/v3/orchestrator.py), [portfolio snapshot](../../app/v3/book_brief.py).
- [Pending-order capacity](../../app/trading/order_capacity.py), [paper executor](../../app/trading/paper_trader.py), [dispatch](../../app/services/pipeline_service.py).
- [Benchmark summaries](../../scripts/summarize_financial_live.py), [live/replay harness](../../tests/unit/test_financial_live_benchmark.py), [edge regressions](../../tests/unit/test_financial_edge_cases.py), [new scenarios](../../tests/benchmarks/fixtures/financial_reasoning_edges_v1.json).

## Audits and evidence

The [sanitized validation receipt](../benchmarks/evidence/financial-edge-contract-2026-09-11/release-validation.json) contains source hashes, cohort counts, local artifact hashes and limitations. Raw prompts/responses and portfolio data remain local and uncommitted.

| Cohort | First responses accepted | Final accepted | New endpoint calls | Interpretation |
| --- | ---: | ---: | ---: | --- |
| `edge-contract-original-r1` | 10/12 | 10/12 | 14 | Development run. Cases 01 and 03 corrected their evidence but hit an overly strict alias-preservation check. |
| `edge-contract-original-final` | 12/12 | 12/12 | 12 | Fresh original scenarios after fixing aliases and checklist labels. |
| `edge-contract-new-scenarios-final` | 12/12 | 12/12 | 12 | Fresh cash, reservation, order-cap, compound-question, unit, missing-data, return-sign and invalid-range scenarios. |
| `edge-contract-original-release-replay` | 12/12 saved | 12/12 | 0 | All original fresh responses revalidated on release source. |
| `edge-contract-edges-release-replay` | 12/12 saved | 12/12 | 0 | All new fresh responses revalidated on release source. |
| `edge-contract-case03-replay-release` | 0/1 saved | 1/1 | 0 | The exact user's original failed pair from `hardening-fresh-r2`; one reward/risk omission prevented using previously selected evidence. |
| `edge-contract-case03-live-repair-release` | 0/1 saved | 0/1 | 1 | One fresh repair of the saved oversized initial response. Repetitive, incomplete JSON remained blocked. |

A final repair-preservation extension followed the two 24-case fresh cohorts. Their exact raw outputs were subsequently replayed on release source; those replays are not additional generated-model successes. The new scenarios are synthetic perturbations of **four base source families**, not twelve independently sourced market cases. The original twelve cases likewise contain repeated scenario variants.

Across the fresh 24 cases: **23 HOLD and one BUY**. The BUY selected **0.15%**, matching the new per-order cap. No generated SELL appeared in those fresh cohorts; deterministic BUY/SELL/HOLD controls passed. Acceptance therefore does not demonstrate useful investment selection or profitability.

Validation:

- Release unit suite: **7,329 passed, 114 skipped**, 236 warnings, 337.45 seconds. Earlier superseded full suites also passed; their results are not substituted for this run.
- Deterministic controls: 12/12 valid full-text and 12/12 valid structured controls accepted; **129/129 altered claims** and **48/48 structured tampering cases** rejected.
- Independent arithmetic review: **85 calculations across 24 fresh cases**, zero discrepancies. All 68 question records were inspected, including explicit unresolved observations. This reviewer checks core arithmetic, not arbitrary qualitative investment claims.
- Strict executor regression confirms a changed pending reservation blocks the purchase before opening the transaction.
- A guarded read of the actual stored paper portfolio produced complete valuation/reservation snapshots and a known budget, with **zero database write attempts**. It did not refresh a market feed or invoke a model/order.
- Every listed benchmark execution check admitted zero detected invalid proposals. No benchmark submitted an order. A green benchmark pytest exit checks transport/harness completion; inspect the recorded financial audit separately.

## Remaining failure: first priority for the next developer

`edge-contract-case03-live-repair-release/03.json` contains the remaining generated repair failure. Its saved initial output requested BUY 2.5% despite 0.6 percentage points of headroom. The fresh response began with HOLD and size zero, then repeated catalog IDs and ended inside a string. It was not a valid JSON object. The runner retained the original proposal for review and the execution gate blocked it.

The request allowed `maxTokens=8192`; the returned text was 2,435 characters and provider usage fields were zero. **Do not conclude it exhausted 8,192 tokens.** The cause of truncation—generation, provider/proxy processing, or another limit—has not been established from this receipt.

Concrete next steps:

1. Trace the saved request through the supported proxy interface and available provider receipts. Preserve exact response text and obtain an authoritative finish/stop reason. Treat zero reported token usage as unavailable telemetry, not zero inference work. Do not edit `prism-service`; any necessary integration adaptation belongs on our side under the workspace ownership rules.
2. Test a repair-specific system prompt that requires the small patch when the original structured decision has valid answers. The current system still describes the full decision contract and allows a patch as an alternative; the model chose a full rewrite. Avoid sending conflicting “complete decision” and “only changed fields” instructions.
3. If supported by the existing external interface, evaluate schema-constrained patch generation. Limit changes to the failed fields and their dependencies, with catalog enums for required selections. Do not accept a truncated prefix as a valid HOLD, invent answers, or add unlimited retries.
4. Replay the exact failed response and run separately labeled fresh saved-initial repairs. Record every attempt, completion/finish reason, measured latency, endpoint calls, and accepted versus blocked outcomes. Do not rerun until success and discard the failures.
5. Require complete JSON, preserved validated answers, correctly recomputed plan-dependent answers and continued independent order rejection in the regression tests. Keep the existing one shared correction allowance.

## Further work after the repair failure

- Evaluate economic decision quality separately. Use new source cases and independent assessment; an always-HOLD policy can satisfy an evidence-consistency contract. Keep valid smaller BUY and SELL controls, and report generated action mix.
- Expand typed question components beyond the current recognized financial relationships. Unknown questions must remain unresolved rather than being “answered” by a true but unrelated fact. Clearly distinguish a concentration-only proposal-fit calculation from full budget compliance.
- Test combined timing, schema and financial failures against the single shared repair allowance. Avoid spending that allowance on a narrow correction that cannot resolve the full invalid decision.
- Strict dispatch now refuses differences introduced after the model's size decision, including governed sizing reductions. If these cause repeated non-execution, move the relevant policy bounds into the pre-decision contract or design an explicit reconsideration workflow; do not silently restore clipping or disable risk controls.
- The execution path is the existing single-process paper trader. Pending paper orders are not live broker reservations. Reassess reservation/transaction atomicity before introducing additional writers or multiple replicas.

## Reproduction and deployment

Start from the released revision below, read the repository `AGENTS.md`, and inspect `git status`. The workspace contains other developers' untracked audit receipts; do not add, remove or overwrite them.

```bash
# Run from trading-service. These tests do not make live model calls by default.
.venv/bin/python -m pytest tests/unit/test_financial_edge_cases.py tests/unit/test_financial_runner.py -q
.venv/bin/python -m pytest tests/unit -q

# One fresh repair of the saved initial response. Use a NEW unique cohort name.
RUN_FINANCIAL_BENCHMARK=1 \
FINANCIAL_BENCH_INDICES=3 \
FINANCIAL_BENCH_SEEDED_INITIAL_COHORT=hardening-fresh-r2 \
FINANCIAL_BENCH_COHORT=nextdev-case03-UNIQUE \
.venv/bin/python -m pytest tests/unit/test_financial_live_benchmark.py -q -s

# Inspect acceptance and safety; pytest transport success is insufficient.
.venv/bin/python scripts/summarize_financial_live.py \
  docs/benchmarks/evidence/financial-reasoning-2026-09-11/nextdev-case03-UNIQUE
```

The raw saved cohort is local. If it is unavailable in another checkout, use the self-contained `test_exact_case03_repair_pattern_retains_reward_risk_despite_added_true_references` regression to exercise preservation; do not claim it reproduces the live repetition/truncation failure.

Deploy only the affected service through the existing deploy-kit workflow. Initialize Linux Node through nvm in a noninteractive WSL shell, then run from `deploy-kit`:

```bash
export NVM_DIR="${NVM_DIR:-$HOME/.nvm}"
. "$NVM_DIR/nvm.sh"
npm run deploy -- --only=trading-service --skip-pull
```

The live-cycle preflight must permit the restart. Verify actual image transfer, container restart, health and runtime source hashes; a build alone is not deployment success.

## Released revision and NAS verification

Pending deployment. The validation receipt identifies the exact tested runtime file hashes.
