# Financial evidence validation: implementation and benchmark status

The implementation is complete as a local candidate. **Live-model improvement and NAS release are still pending.** Automatic approval review rejected the live benchmark destination twice. A request for explicit authorization to send the synthetic EVLT evidence, Board prompts, and generated responses to `http://10.0.0.16:5591/prism-proxy/agent?stream=false` is pending. No live calls from this new benchmark have run, and this candidate has not been deployed.

## Implemented in sequence

1. **Freeze the failures.** The original twelve responses and their review remain unchanged. The old expression-only arithmetic audit checked zero relationships in those responses. The existing manual review found material factual errors in nine responses and no fully complete response. A separate source-only fixture transcribes the supplied metrics, units, dates, periods, missing observations, and source identities; it contains no expected answers.
2. **Capture source facts and calculate in code.** The existing technical, fundamental, valuation, and portfolio builders now capture the same snapshot they render. Older coalesced fields retain their own dates and vendors. A timed-out builder cannot update the accepted cycle snapshot later. Incomplete portfolio marks do not become verified exposure. Formulas compute range location, holding return, forward PEG when the correct earnings-growth operand exists, concentration headroom, and separate current/conditional reward-risk. Per-order and total-concentration caps remain distinct. Unknown quote/reporting currencies are labeled explicitly, not silently called USD.
3. **Check claims and questions.** The Board receives a financial evidence contract outside the prose truncation budget. Numerical assertions belong in `financial_claims`, with metric, value, unit, date, and source checked against the supplied record. Rationale and answer prose cite those claims rather than independently restating numbers. Comparisons require compatible metrics and a correct relation. Tests cover wrong RSI, lost signs, gross/operating confusion, unsupported prior debt/equity, range-location words, guidance overstatement, and source-count corruption. Question ids and text must survive exactly. Date-specific answers and named filing months require matching evidence. An unrelated true metric cannot answer the requested calculation, and an answerable calculation cannot be hidden behind abstention.
4. **Reconsider once; enforce at execution.** Financial repair shares the runner's existing one-correction allowance with schema/timing repair. It can change the investment conclusion when corrected evidence warrants it. Both original and candidate outputs, evidence, discrepancies, and validation results are traced. Failed repair leaves the authored output available for review. The policy gate recomputes validation and returns `HOLD_POLICY_BLOCKED_FINANCIAL_EVIDENCE`; the order dispatcher independently rechecks the stored record and flattened order fields. A model-authored “consistent” audit or an altered policy label is not authority to trade. Board activation carries the contract into synthesis; legacy and direct Delta-only paths are not silently relabeled as checked.
5. **Prepare the live comparisons.** The opt-in driver supports frozen replay, fresh original-case runs, and matched baseline/candidate runs on eight unseen variants. Tools and orders are disabled, the destination is a literal checked endpoint, source briefs have a no-truncation assertion, and the original method/persona arms are retained without double injection. Test fixtures block pipeline database writes; the proxy would retain its normal request/session logs.

## Offline evidence

| Check | Result |
|---|---:|
| Original frozen responses reviewed | 12 |
| Original responses with reviewed factual errors | 9 |
| Old expression-audit checks across those responses | 0 |
| Correct authored controls accepted, including eight unseen variants | 12 / 12 |
| Deliberately corrupted claims rejected | 129 / 129 |
| Focused latest financial tests | 65 passed |
| Full suite before final date-scope refinements | 7,202 passed, 114 skipped |
| Exact committed runtime full-suite verification | 7,205 passed, 114 skipped |
| Real order-dispatch regression suite | 6 passed |

[Offline control results, r2](evidence/financial-reasoning-2026-09-11/offline-controls-r2.json) retain every control and corruption, original response hashes, and checker source hashes. These controls are constructed test inputs, **not generated model successes**. The retained r1 predates the filing-month check; r2 also corrects the holdout's question month to match its deliberately shifted source dates, before any live responses exist.

The first broad run crossed midnight and exposed an existing ADV-test clock race: its fixture date was captured before midnight while the query used the following day. Pinning that test's clock fixes reproducibility without changing the production ADV query. Its focused suite passed; the subsequent broad suite passed as listed above. The final committed-runtime run took 531 seconds. An additional test then drove the real pipeline dispatch loop with mocked trading calls: the correct control reached the mocked buy function, while a forged audit/policy pass with an incorrect claim reached neither buy nor sell. [Validation record](evidence/financial-reasoning-2026-09-11/offline-validation.json) includes exact source and test-log hashes, and explicitly records zero new live-model calls and no deployment.

## Live release criteria and remaining work

Run the four-case pilot, then retain two fresh twelve-response cohorts and a frozen replay cohort. Run matched baseline/candidate requests on the eight unseen cases. Independently review every final rationale and research answer against the source facts, in addition to the automated contract audit. Report first-pass versus repaired results, factual errors, calculation errors, question completeness, useful abstention, action consistency, and schema/timing success separately. Transport-test success is not a quality score.

Release requires no known factual/calculation regression escaping validation, correct controls remaining accepted, no order authority for unresolved material errors, and a measured improvement in useful model outputs rather than a higher refusal rate alone. After that validation, deploy only `trading-service` through deploy-kit and verify the NAS container revision and HTTP health. Deployment remains the already-authorized completion step; the pending authorization concerns the benchmark HTTP request.

Reproduction commands from the `trading-service` directory:

```bash
# Offline controls: use a new output filename for each retained run.
.venv/bin/python scripts/benchmark_financial_controls.py --out /tmp/financial-controls-new.json

# Run only after the explicit NAS request authorization is resolved.
RUN_FINANCIAL_BENCHMARK=1 FINANCIAL_BENCH_COHORT=pilot-r1 FINANCIAL_BENCH_INDICES=1,6,7,12 .venv/bin/python -m pytest tests/unit/test_financial_live_benchmark.py -q -s
RUN_FINANCIAL_BENCHMARK=1 FINANCIAL_BENCH_COHORT=fresh-r1 .venv/bin/python -m pytest tests/unit/test_financial_live_benchmark.py -q -s
RUN_FINANCIAL_BENCHMARK=1 FINANCIAL_BENCH_COHORT=fresh-r2 .venv/bin/python -m pytest tests/unit/test_financial_live_benchmark.py -q -s
RUN_FINANCIAL_BENCHMARK=1 FINANCIAL_BENCH_MODE=frozen FINANCIAL_BENCH_COHORT=frozen-r1 .venv/bin/python -m pytest tests/unit/test_financial_live_benchmark.py -q -s
RUN_FINANCIAL_BENCHMARK=1 FINANCIAL_BENCH_FAMILY=holdout FINANCIAL_BENCH_VARIANT=baseline FINANCIAL_BENCH_COHORT=holdout-baseline-r1 .venv/bin/python -m pytest tests/unit/test_financial_live_benchmark.py -q -s
RUN_FINANCIAL_BENCHMARK=1 FINANCIAL_BENCH_FAMILY=holdout FINANCIAL_BENCH_VARIANT=candidate FINANCIAL_BENCH_COHORT=holdout-candidate-r1 .venv/bin/python -m pytest tests/unit/test_financial_live_benchmark.py -q -s
```

## Scope and limits

This validates consistency with supplied evidence; it does not establish that a vendor is correct, prove every qualitative inference, or predict investment returns. The prose checks address observed contradiction classes, not general semantic entailment. Numerical claims elsewhere in an unsupported prose source must not be mistaken for verified evidence. Independent review remains necessary for the live benchmark.

The production catalog covers captured technical, fundamental, valuation, and portfolio snapshots. Arbitrary tool prose and memory are not automatically promoted into facts. Typed next-year EPS growth and pending-order reservations are not currently captured, so exact forward PEG and fully reserved headroom remain unknown when those inputs are absent. The held-exposure upper bound and existing executor checks still prevent opening above a known total cap. A computed conditional ratio remains hypothetical and never turns a trigger into an executable quote.

No Prism, adapter, or client repository was modified by this work.
