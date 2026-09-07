# Memory-only benchmark — 2026-09-07

**Established:** the actual removed upstream memory costs **1,346–2,857 additional input tokens per request**, depending on role. **Not established:** full-cycle latency improvement or better trading/research quality. The role replay needs a corrected, authorized rerun; the completed component probe measures a fixed eight-token diagnostic response.

The earlier replay changed skills and other inputs and did not contain actual upstream memory. It cannot answer this memory-removal-only question. This experiment freezes real role-scoped `<agent-memory>`, `<past-workflows>` and `<project-skills>` blocks obtained through supported conversation GET APIs, then uses the deployed lazy-agent-service filter. An equality assertion proves that those blocks are the only message difference. The current compact retrieval query appears identically in both arms. Prism was not edited or written to.

## Completed component measurements

GLM-5.3-Flash-EXL3; temperature 0, min_p 0, thinking disabled, max output 64 tokens; no tools. Seven roles × two pairs in AB then BA order: **28 scored calls plus two excluded warmups**. Both arms request exactly the same diagnostic JSON. Token counts below are actual provider usage, not character estimates.

| Memory source role | Removed characters | Input tokens before → after | Tokens saved/request |
|---|---:|---:|---:|
| Junior Analyst | 6,452 | 1,599 → 70 | 1,529 |
| Fundamental Analyst | 6,479 | 1,616 → 70 | 1,546 |
| Quant Analyst | 10,772 | 2,927 → 70 | 2,857 |
| Bull | 8,627 | 2,071 → 70 | 2,001 |
| Bear | 6,713 | 1,664 → 70 | 1,594 |
| Regime Engine | 10,332 | 2,521 → 70 | 2,451 |
| Board | 5,690 | 1,417 → 71 | 1,346 |

One request for each of these seven roles saves **13,324 input tokens** in this diagnostic. This is not a measured per-cycle total: the real pipeline has additional stages, variable routing, retries and multiple model turns. The very large percentage reduction of a 70-token diagnostic is not representative of a full role prompt; report the removed-token count instead.

Across all 14 attempts per arm, median wall time was **4.27s before / 0.57s after**, with **14/14 before and 13/14 after** producing the requested JSON. The remaining after call timed out at 180 seconds; its usage is unknown, not zero. The last two roles overlapped an invalid role-test setup run, adding self-generated queue load. These full-run medians are descriptive, **not a clean speed estimate**.

For the first five roles before that overlap (an explicitly exploratory subset, ten calls per arm), median wall time was **2.73s → 0.55s**, and median time to first content **2.47s → 0.42s**, with 10/10 valid outputs in each arm. Even this subset had large outliers: a before Bull call took 89s and an after Quant call took 24s. Shared DGX load and cache state were not controlled. Two paired repetitions per role are insufficient for a reliable uncertainty estimate. These results suggest less prompt-processing work, but do not establish an 80% faster trading cycle or better reasoning.

All attempts, warmups, load snapshots, exact usage and per-role timing are retained in `docs/benchmarks/memory-isolation-20260907.json`; no failures are silently discarded.

## Prepared role replay and current blocker

Eight cases across LULU/STX, covering all seven roles. The inputs were captured from the real current `run_v3_agent` assembler with external model calls replaced and production database tripwires enabled. The current reviewed skills, evidence, role tools and turn budgets are identical between arms; the Board uses the CONTRADICTORY persona. Budgets are Junior 7, Fundamental 12, Quant 14, Bull/Bear/Regime/Board 5, with an 8,192-token output cap. Every whitelisted tool has a checked-in schema. Tool arguments are schema-validated; local whiteboard state is separate for each arm. Frozen evidence tools return only their relevant source; missing recorded results return explicit offline-unavailable errors. No trades, scheduling, learning or external tool actions occur.

Scoring covers raw JSON, minimum required artifact fields, mandatory Junior whiteboard writes, tool calls/errors, exact regime VIX and Board sizing/entry constraints. Field presence is not a complete semantic quality score. Frozen reports contain historical summaries/legacy lessons identically in both arms, so this isolates only the upstream system-memory removal, not every earlier learning change. This is a bounded offline replay, not the full production harness, live research or P&L evaluation.

The first role run was invalid because a local `jsonschema` dependency was missing. It was stopped and all results were preserved under `.scratch/memory-isolation-20260907/invalid-setup-run`; setup errors and a timeout are not scored as model failures. The corrected script imports the installed system validator, preflights every tool schema, logs each turn, and uses the shim's 900-second non-stream response budget. Its entry-mode check matches the actual three-value contract. There are **no valid completed role-quality pairs to report**.

Automatic approval review rejected the corrected rerun, stating that conversation-derived prompts and evidence needed explicit payload/destination authorization. The destination is the existing owned NAS route `http://10.0.0.16:5591/vllm-shim/gold-spark`, configured in `vault-service/projects.json`, which forwards to the user's DGX `10.0.0.141:8000`; ownership was also checked against the deployed container and owned shim source. Approval has been requested for exactly that inference-only transfer. No alternate endpoint or indirect execution was used after rejection.

Once authorized, run the corrected role comparison first and then repeat the component probe sequentially, preserving the first component run. Do not claim a quality gain before those pairs finish and are checked against the frozen evidence. Do not introduce new memory candidates into this A/B test; evaluate each candidate separately after fixing contradictory contracts.

The companion `memory-and-rule-audit-2026-09-07.md` contains seven rule/integration findings and the proposed failure-driven memory admission, retrieval and retirement design.

Reusable runners are checked in at `scripts/benchmarks/memory_component.py` and `scripts/benchmarks/memory_role_replay.py`. They default to the frozen local scratch corpus above; `MEMORY_BENCHMARK_DIR` can select another prepared corpus. Initialize Linux nvm and set `REPLAY_NODE` to the Linux Node executable before running them with the trading virtualenv's Python. The corpus is intentionally local: it contains private conversation/evidence text. The role runner requires the installed system `jsonschema` package. Both runners make real inference requests only when explicitly executed, and the current rerun remains subject to the pending authorization described above.
