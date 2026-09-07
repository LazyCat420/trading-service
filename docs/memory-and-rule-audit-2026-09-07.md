# Memory removal and trading-rule audit — 2026-09-07

Scope: audit the current trading cycle and the owned lazy-agent-service boundary; isolate the cost/quality effect of removing upstream memory; specify a selective replacement. No trading policy changes, production trades, Prism mutations, or deployment are part of this audit. Runtime revisions inspected: trading-service 457829ec (documentation HEAD 4a97128e); lazy-agent-service 5ca0fea. The earlier changed-skills replay is not a memory-only A/B test and is excluded from this comparison.

## Findings, ordered by consequence

### 1. A no-price HOLD can reach the BUY execution branch — high priority

`app/v3/orchestrator.py:3037` returns `HOLD_NO_PRICE_DATA` when a would-be executable decision has no stored price history. `_build_v1_compatible_result` retains the agent's original BUY/SELL action; the caller attaches `policy_action` separately (`orchestrator.py:2091`). The execution chain in `app/services/pipeline_service.py:2693` recognizes SELL + `HOLD_NO_POSITION`, and at `:2704` recognizes only the `HOLD_POLICY_BLOCKED` prefix. Consequently an otherwise eligible BUY with `HOLD_NO_PRICE_DATA` reaches `elif action == "BUY"`. `resolve_trigger_registration` at `:371` uses the same prefix and permits a dynamic watch for this refusal.

Reproduced offline with the real gate and result builder, then the executor's actual AST branch conditions with all branch bodies replaced by condition labels. Output: `gate=HOLD_NO_PRICE_DATA`, `result_action=BUY`, selected branch `action == 'BUY'`, trigger result `{sell_side:false,dynamic:true}`. No order or trigger was executed. Earlier preflight and paper-trader checks may still prevent an actual fill; this reproduction proves the policy handoff is inconsistent, not that an invalid trade occurred. The gate was explicitly designed as a final missing-data backstop, including disappearance after preflight.

Proposed repair: a shared typed disposition/permission contract at the gate, executor and trigger helper. Explicit execution permission should accompany a permitted action; every gate refusal must survive downstream. Preserve existing missing-price, holdings, confidence and entry-intent policies. Add an end-to-end fixture that asserts no buy call and no trigger registration for this label, covering all current refusal labels and malformed/unknown dispositions. Renaming one string is a narrow emergency repair, but does not prevent recurrence.

### 2. Tool restrictions disappear when session registration is missing — high priority

`lazy-agent-service/src/services/prism/PrismProxyService.ts:39` holds whitelists only in memory. Restart, 24-hour TTL, or the 5,000-session cap can lose registration. `isToolAllowed` deliberately returns true for unknown or missing IDs. `src/routes/ExecuteRoutes.ts:115` consults it only when the request supplies a cycle ID. This conflicts with the trading harness's rule that tools outside each role's whitelist are rejected.

Pure in-memory reproduction: registered allowed tool=true; registered disallowed tool=false; the same disallowed tool on an unknown session=true; absent ID=true. No tool execution. Other authentication or downstream restrictions may still exist; this is specifically a loss of the role whitelist, not a claim of unauthenticated arbitrary execution.

Proposed repair: carry verifiable trading project/role/session identity to every execution and persist or deterministically reconstruct its bounded whitelist on our side. A positively identified trading request with no valid authorization context must not inherit unrestricted access. Preserve non-trading compatibility explicitly. Implement in lazy-agent-service, never Prism.

### 3. The Board receives mutually incompatible HOLD instructions — medium priority

The shared Board prompt (`app/v3/agents/board_of_directors.py:111`) says to reserve HOLD for a broken thesis; its held-position section (`:117`) says an intact thesis should KEEP/HOLD and a broken thesis should SELL. These are opposite interpretations for an existing position. The CONTRADICTORY persona also permits HOLD when both sides are strong and uncertainty remains. The current reviewed Board skill says not to force trades, while the older shared prompt pressures constructive cases toward BUY and confidence 70–78.

The versioned `enter_on_condition` contract correctly distinguishes a conditional BUY from immediate execution. That earlier ambiguity has been addressed; it does not resolve HOLD/KEEP semantics or confidence pressure.

Proposed repair: one action table keyed by held/not-held and entry intent; remove conflicting directives and examples. State confidence as the forecast the evidence supports, independently of the execution threshold. Do not change numerical risk thresholds or turn every uncertainty into a veto.

### 4. Board output must begin with both XML and JSON — medium priority

`board_of_directors.py:145` requires a `<thought_process>` block first and then raw JSON. The real runner's final directive (`app/v3/agent_runner.py:1213`) requires the entire final response to be one JSON object starting with `{`. Both appear in the captured Board prompt. A model following the first fails the second; longer prose also competes with the artifact for output budget. This is a prompt contradiction, not evidence that it alone caused measured latency or failed runs.

Proposed repair: require only the artifact, with concise decision rationale in its existing field. Keep private reasoning out of the public output contract. Let one schema and its validator define formatting rather than duplicate prose requirements.

### 5. Prompt thresholds can drift from configured thresholds — medium priority

The Board prompt hardcodes data quality <40 as a blocking condition. Enforcement reads `DATA_QUALITY_FLOOR`; `app/services/parameter_store.py:104` permits 20–70 with default 40. This is a latent mismatch whenever configured away from 40, not proof that today's stored value differs. Similar confidence-band language assumes 70 although the execution threshold is configurable from 50–90. The reviewed skill says to use configured risk limits, so a stale literal remains misleading even if a tool can fetch parameters.

Proposed repair: inject one compact, versioned policy snapshot generated from the same values the executor will use. Distinguish policy facts from probabilistic analysis. If a value changes during a run, make the applied version visible rather than silently attributing its consequences to the agent.

### 6. Retrieval housekeeping remains a user instruction — lower priority

`TradingLearningBoundary.ts:prepareTradingRequest` appends a compact retrieval-index message as the last user turn. `filterTradingPayload` strips the routing marker and upstream system-memory tags, but leaves that user message for generation. It tells the model to finish the original task, so a failure is not established; it still mixes retrieval metadata with instructions and is the user text persisted by the upstream conversation view. Both A/B arms retain it identically.

Proposed repair: where the external interface permits, separate retrieval query from generation input. Otherwise tag the exact wrapper-owned indexing message and remove only that message at the final owned seam after retrieval. Preserve original user evidence and validate stored conversation fidelity. Do not remove arbitrary last-user messages or modify Prism.

### 7. Confidence instructions mix different prediction targets — medium priority

`fundamental_analyst.py:75` defines confidence as the share of the thesis verified numerically; `:81` defines the same field as the probability that its directional thesis is right over the declared horizon. Verification coverage and forecast probability are different quantities. Across roles the declared targets differ again: the Junior Analyst estimates fact-check survival; the Regime Engine estimates regime-label correctness; other roles forecast direction over their own horizons.

Meanwhile `app/agents/base_agent.py:422` builds a common calibration block from resolved `decision_outcomes` WIN/LOSS records (excluding FLAT), and `data_report.py:493` places it in the shared briefing. Its instruction calls the agent overconfident when its number exceeds the trade-win rate of that bucket. The captured prompts contain this block. A trade's probability of winning conditional on WIN/LOSS resolution does not calibrate research verification or regime classification, nor automatically calibrate a different forecast horizon.

Proposed repair: define each confidence field's target, horizon and outcome population once. Calibrate against labels for that same target; use evidence coverage as a separate field if needed. Supply decision-level trade calibration to deciders with the appropriate interpretation, and use role prediction receipts for role calibration. Do not force different probabilities into one numeric distribution or rewrite confidence after the agent decides.

## Documentation drift and false positives excluded

The trading AGENTS.md table says Bull/Bear have 3 turns and later says they have zero tools; current budgets are 5 turns and both have verification tools. The Board's documented 3-call cap and tool list also differ from the current path. The claim that no order execution path exists is stale: pipeline_service invokes the paper trader. This does not imply a live broker integration. Global guardrail prose about all missing data blocking work and requiring a 3–5-year BUY thesis is not imported into these V3 role prompts; it must not be reported as a current runtime conflict. Some anti-hallucination blocks are used by other persona code.

Explicit null risk/trigger fields are preserved by the versioned decision-contract merge. An initial suspicion that older `or` fallbacks resurrected deleted values was disproved for this path. Confidence gates and reduced position sizing have different effects; their coexistence is not itself a contradiction. Auditing means identifying incompatible behavior, not removing controls simply because they are conservative.

## Rebuild memory around demonstrated missing capabilities

Keep the current upstream-memory exclusion and reviewed-skill boundary while collecting candidate evidence. The system already has reviewed methods, research questions/answers, delivery receipts and candidate storage; extend those instead of adding another broad injection channel.

1. **Record an observation when a step fails or consumes avoidable work.** Capture role, task step, tool/schema version, actual input/error, supplied evidence, output, retries, timestamps and the specific unmet contract. Do not automatically turn every successful workflow or cycle narrative into a memory. Store the full trace in an audit log with retention; it is not default prompt content.
2. **Classify the cause before proposing memory.** Contradictory rules, broken tools, schema mismatches, missing feeds and execution bugs require code/data repairs. Missing local procedure or reusable verified research may justify memory. Lack of a favorable outcome alone does not.
3. **Create the smallest candidate that changes the failing step.** A method has a precise trigger, one action/check, scope and version. A factual answer has entity, period, source, supporting excerpt, as-of time and expiry/invalidation. Never combine a remembered recommendation with authority to change policy.
4. **Test baseline versus baseline+candidate.** Hold the task, evidence, model and policies fixed; alternate order and include held-out tickers, dates and related tasks. Compare artifact acceptance, supported answers, tool failures/redundancy, tokens and latency. Count failures and data gaps. Reject candidates that merely reproduce the original example or improve speed by skipping required work. Policy/entry-intent violations must not increase.
5. **Promote reviewed candidates only after a measured benefit.** Record exact text hash, evidence set, reviewer and version. Small samples remain provisional; there is no magic three-example threshold. A sensible acceptance rule is a positive held-out effect with uncertainty reported and no meaningful regression on unaffected tasks. Keep candidate generation and active serving separate.
6. **Retrieve only at the matching step.** Filter role, entity, time, tool version and unresolved question before semantic similarity. Supply a short matched instruction or answer with provenance; fetch more only when needed. Start with at most two short records and a combined ~500-token budget per step as a tunable engineering limit, not an experimentally established optimum. Zero matching memories is normal.
7. **Retire stale or useless entries.** Track delivered, used, helpful and contradicted separately. Expire event/current facts at their validity boundary, invalidate procedures when tools change, and periodically ablate active entries. Archive an entry that is redundant with the current prompt/model or no longer improves held-out work.

| Example observed difficulty | Correct destination | Serving condition |
|---|---|---|
| Board emits XML before required JSON | Fix the conflicting output contract | Always enforce one schema; no episodic memory needed |
| Agent repeatedly confuses HRP portfolio target with remaining order headroom | Prefer a typed/precomputed sizing field; test one short method only if confusion persists | Sizing step, relevant units/version, measured benefit |
| Research already verified a particular filing's guidance and the next cycle repeats it | Dated research answer with original source/quote and period | Same unresolved question and still-applicable period |
| Current earnings time or institutional holding is missing | Fetch authoritative current data; retain dated result if useful | Exact entity and validity window, never a general remembered fact |
| Tool renamed an argument or began timing out | Repair schema/adapter or outage handling; incident record | Do not teach a permanent workaround for a transient outage |
| A trade won after a long tool sequence | Outcome observation, not an automatically promoted workflow | Aggregate appropriately before any causal or policy claim |

We cannot reliably inspect the model's complete training corpus or ask it to certify that a fact was absent. Use an operational test: can the current model complete the step from its ordinary verified task context, and does the proposed record add reproducible benefit? Private tool contracts, task state and new dated observations are likely candidates; generic investing explanations usually are not. Evaluate again when the model changes.

Proposed minimal record: `id, kind, role, step, trigger, entity, period, content, sources, observed_failure_ids, tool_version, policy_version, model_evaluated, created_at, expires_at, invalidates_on, candidate_status, text_hash, heldout_eval, delivered_count, evidenced_use_count, retirement_reason`. Policy versions scope compatibility; a memory does not author policy. Embed or index only the compact record and link to the retained trace.

The general rationale for just-in-time context is consistent with [Anthropic's context-engineering guidance](https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents). [Lost in the Middle](https://arxiv.org/abs/2307.03172) demonstrates that more context and its position can affect retrieval performance on evaluated models; it does not establish an effect size for this deployed GLM. Our local paired measurements must establish that separately.

## Evidence and reproduction

Benchmark protocol, source conversation IDs/hashes, frozen inputs, all outputs and logs are retained locally under `.scratch/memory-isolation-20260907`. Sensitive conversation payloads are not copied into the checked-in report. The filter input builder asserts that messages differ only by the three removed system-memory tag classes. `tests/unit/test_memory_audit_capture.py` captures real assembled prompts with production database tripwires and a mocked model; `test_memory_audit_reproductions.py` characterizes the no-price handoff. Both require `MEMORY_AUDIT_CAPTURE=1`. The lazy-agent-service script `scripts/audit-trading-tool-boundary.ts` reproduces whitelist behavior entirely in memory.

Benchmark results and limitations are in the companion `memory-isolation-benchmark-2026-09-07.md`. Audit-only files do not change a deployable runtime, so no container rebuild/restart is needed. The follow-up implementation should repair the confirmed contract conflicts first, then evaluate individual memory candidates against that consistent baseline.
