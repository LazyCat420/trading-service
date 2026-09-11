# Financial evidence validation: candidate and benchmark results

**The candidate is implemented, but fresh live validation and NAS deployment remain pending.** The early live pilots did not establish improved financial reasoning. A revised structured explanation passed local controls and an offline replay of one saved model repair. That is not yet evidence of repeatable live improvement.

Automatic approval review rejected the revised four-case run twice. A renewed request is pending for the exact NAS endpoint, synthetic fixtures, revised Board prompts and generated responses. Neither rejected command started. The candidate has not been deployed.

## What the candidate does

The original twelve responses and review remain frozen. The old expression-only arithmetic audit checked zero relationships in those responses; independent review found factual errors in nine and no fully complete response.

Source builders capture the same technical, fundamental, valuation and portfolio snapshots they render. Each fact retains its metric, value, unit, source, period and original date. A timed-out builder cannot later update the accepted snapshot. Incomplete portfolio marks do not become verified exposure. Unknown reporting currencies remain unknown. Code calculates holding return, range location, forward PEG from the correct earnings-growth operand, concentration headroom, and separate present versus hypothetical reward/risk. Per-order and total-concentration caps remain distinct.

The financial validator checks explicit claims against those records, verifies question coverage and dates, and rejects incorrect relationships. It checks rationale fields, including mispricing and override explanations. Sentence-scoped checks prevent an unrelated negation from disabling an entire paragraph's validation. Requested purchase-fit answers must include the actual fit calculation. A true but unrelated metric cannot substitute for a requested calculation; an available calculation cannot be hidden behind abstention.

The runner gives the agent one correction opportunity shared with schema and timing repair. A failed correction preserves the original output for review. Policy and order dispatch independently recompute financial validation; an authored “consistent” audit or forged policy label cannot authorize an order. Unresolved decisions receive `HOLD_POLICY_BLOCKED_FINANCIAL_EVIDENCE`.

The live pilots exposed a skipped schema correction for tool-disabled, evidence-backed decisions. That path now uses the same correction allowance. A behavioral regression also found that parsed tool-call text could be classified as a schema error: transport-failure classification now takes precedence and the unexecuted call is not repaired into fictitious research.

## Structured financial explanations

The early model repeatedly copied correct numbers but interpreted them incorrectly, or copied its rejected prose into the correction. Enabling reasoning with either output allowance tested below returned no decision. Those settings were not adopted.

The revised format uses `financial_reasoning_version: 2`. The model authors action, confidence, sizing, timing and the selection of relevant reasoning steps. It selects steps for every question. Code renders the financial statements, exact source values and original question text from those selections. Examples include whether the supplied purchase fits capacity, whether price is nearer support or resistance, the sign of a reported metric, and present versus hypothetical reward/risk.

This is **code-verified financial explanation**, not a claim that the model became better at arithmetic. The raw response remains available. A question-keyed map and an array are equivalent supported selection forms. Source IDs can identify their own observations; they do not add unselected operands. A requested relationship such as price versus oscillator units is completed only when the model selected both inputs.

The renderer does not overwrite conflicting authored prose, questions or numerical claims. Such conflicts fail validation. Its output is recomputed at the execution boundary. Legacy full-text claims still undergo their existing strict checks. Action and confidence are not rewritten by the renderer.

## Local validation

| Check | Result |
|---|---:|
| Original frozen responses with reviewed factual errors | 9 / 12 |
| Original fully complete responses | 0 / 12 |
| Original expression-audit relationships checked | 0 |
| Correct typed controls accepted | 12 / 12 |
| Corrupted numerical claims rejected | 129 / 129 |
| Correct structured selection controls accepted | 12 / 12 |
| Structured tampering cases rejected | 48 / 48 |
| Latest focused tests | 123 passed |
| Final candidate full unit suite | 7,247 passed, 114 skipped |
| Saved model-response replay on final candidate | 1 / 1 consistent; zero new endpoint calls |

[Offline controls, r4](evidence/financial-reasoning-2026-09-11/offline-controls-r4.json) retain inputs, outputs, audits and source hashes. The structured controls include nonzero BUY, SELL and HOLD selections. They are constructed controls, not generated model successes. Tampering tests cover conflicting prose, altered values, changed questions and unknown steps. Additional real-runner tests verify that a smaller BUY passes financial validation and that tampering is rejected at execution.

The earlier candidate's complete suite passed 7,205 tests with 114 skips. During live refinements, a broad run reported 7,217 passed, 114 skipped and one failure: a source-string assertion still expected the old tool-only repair condition. It was updated, and a stronger behavioral test found and verified the transport-classification fix described above. A later full run was deliberately stopped when the final answer-completeness refinement superseded it. The final run passed 7,247 tests with 114 skips in 341.65 seconds. [Final validation record](evidence/financial-reasoning-2026-09-11/candidate-validation-r2.json) records the exact runtime commit, source hashes and test-log hashes.

## Retained live development attempts

| Cohort | Cases | Endpoint calls | Financial passes at that revision | Observation |
|---|---:|---:|---:|---|
| pilot-r1 | 4 | 4 | 0 | Three truncated responses and one nested decision. |
| pilot-r2 | 4 | 8 | 0 | Readable decisions, unresolved facts and answer format. |
| pilot-r3 | 4 | 8 | 0 | Questions preserved, rejected prose copied into repair. |
| pilot-r4 | 4 | 8 | 0 | Fresh corrections still made factual errors. |
| thinking-ablation-r1 | 1 | 1 | 0 | Full 8,192-token allowance used; no final decision. |
| pilot-r5 | 1 | 2 | 0 | Focused review improved purchase-fit wording but left other errors. |
| pilot-r6 | 1 | 2 | 0 | Fact references shortened output; interpretation errors remained. |
| thinking-16k-ablation-r1 | 1 | 1 | 0 | Full 16,384-token allowance used; no final decision. |
| structured-pilot-r1 | 1 | 2 | 0 | Repair selected the needed evidence and HOLD, but used equivalent unsupported shapes. |

These are development attempts, not a completed release benchmark. In total they made 36 endpoint calls across 21 cases. Zero passes at those revisions does not mean every statement was false; it means no output satisfied the entire financial contract at that revision.

The final renderer accepts the saved structured repair without changing its HOLD action, confidence or evidence selections. The headroom, PEG and range answers retain their original question identities and source dates. The [final replay](evidence/financial-reasoning-2026-09-11/structured-pilot-r1-release-replay/01.json) is recorded separately from live attempts and makes no new inference requests. It cannot establish a repeatable improvement or generalization.

[Provider receipts](evidence/financial-reasoning-2026-09-11/provider-receipts/) retain the prepared provider requests for all completed live attempts. They confirm full prompt delivery and reveal upstream recovery attempts: **40 provider request snapshots for 36 endpoint calls**. For example, `pilot-r4` has nine provider requests for eight endpoint calls. The runner's two-call limit concerns endpoint calls; it is not a guarantee of exactly two underlying inference attempts. Some recovery requests change sampling settings. A provider request snapshot does not by itself prove completed inference.

## Remaining release checks

After renewed authorization, run a fresh four-case pilot, then two twelve-response fresh cohorts, a frozen-response repair cohort, and matched baseline/candidate runs on eight unseen variants. Keep every attempt. Review delivered explanations, question completeness, selected evidence, action consistency and useful abstention separately from schema/transport status. Compare first-pass and corrected results. Distinguish code-rendered financial correctness from raw-model mathematical reasoning.

Release requires no known factual/calculation regression escaping validation, useful correct outputs rather than a higher refusal rate alone, and independent order enforcement for unresolved errors. After passing that evaluation and local validation, deploy only `trading-service` through deploy-kit and verify the NAS container revision and HTTP health. Deployment is already authorized; the outstanding approval concerns the revised benchmark request.

Reproduction from the `trading-service` directory:

```bash
# Offline controls; always choose a new output file.
.venv/bin/python scripts/benchmark_financial_controls.py --out /tmp/financial-controls-new.json

# Offline replay only: no new model request.
RUN_FINANCIAL_BENCHMARK=1 FINANCIAL_BENCH_COHORT=replay-new FINANCIAL_BENCH_REPLAY_COHORT=structured-pilot-r1 FINANCIAL_BENCH_INDICES=1 .venv/bin/python -m pytest tests/unit/test_financial_live_benchmark.py -q -s

# Revised live runs: pending renewed authorization.
RUN_FINANCIAL_BENCHMARK=1 FINANCIAL_BENCH_COHORT=structured-pilot-r2 FINANCIAL_BENCH_INDICES=1,6,7,12 .venv/bin/python -m pytest tests/unit/test_financial_live_benchmark.py -q -s
RUN_FINANCIAL_BENCHMARK=1 FINANCIAL_BENCH_COHORT=fresh-r1 .venv/bin/python -m pytest tests/unit/test_financial_live_benchmark.py -q -s
RUN_FINANCIAL_BENCHMARK=1 FINANCIAL_BENCH_COHORT=fresh-r2 .venv/bin/python -m pytest tests/unit/test_financial_live_benchmark.py -q -s
RUN_FINANCIAL_BENCHMARK=1 FINANCIAL_BENCH_MODE=frozen FINANCIAL_BENCH_COHORT=frozen-r1 .venv/bin/python -m pytest tests/unit/test_financial_live_benchmark.py -q -s
RUN_FINANCIAL_BENCHMARK=1 FINANCIAL_BENCH_FAMILY=holdout FINANCIAL_BENCH_VARIANT=baseline FINANCIAL_BENCH_COHORT=holdout-baseline-r1 .venv/bin/python -m pytest tests/unit/test_financial_live_benchmark.py -q -s
RUN_FINANCIAL_BENCHMARK=1 FINANCIAL_BENCH_FAMILY=holdout FINANCIAL_BENCH_VARIANT=candidate FINANCIAL_BENCH_COHORT=holdout-candidate-r1 .venv/bin/python -m pytest tests/unit/test_financial_live_benchmark.py -q -s
```

The driver uses the literal `http://10.0.0.16:5591/prism-proxy/agent?stream=false`, checks it against the frozen manifest, and disables tools, function calling, workspaces and orders. Pipeline database inputs/writes are mocked. The fixtures are unchanged synthetic EVLT observations; they are not live portfolio or security data. The proxy retains its normal request/session logs.

## Limits

Consistency with supplied evidence does not prove vendor truth, investment merit or future returns. The structured catalog covers supported financial relationships; it does not prove arbitrary qualitative claims. Unsupported questions remain unresolved. Arbitrary tool prose and memory are not automatically promoted into facts. Next-year EPS growth and pending-order reservations remain unknown when not captured, so exact forward PEG and fully reserved headroom are unavailable in those cases.

No Prism, adapter or client repository was modified by this work.
