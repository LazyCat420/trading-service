# HANDOFF — Five self-contradictions in the V3 trading cycle (2026-08-03)

An audit asked "is there logic in the trading cycle that contradicts itself?".
Five places were: a comment claimed one thing while the code did another, or two
code paths answered the same question differently. All five are fixed. Each
finding below was confirmed against the live DB before being touched.

> Supersedes the 2026-08-03 component-efficacy handoff (that work is live and
> unchanged; see `docs/AUDIT_COMPONENT_EFFICACY_2026-08-03.md`).

## What is live right now

### 1. Cross-desk dissent is now the AGENT's call, not a silent number rewrite

**Was:** `_persist_trade_verdict` detected directional dissent *after* the desk
decided and rewrote `confidence` to 60, commented "deliberately NOT the full
downgrade-to-HOLD". `ANALYSIS_CONFIDENCE_THRESHOLD` is **70**, so every decision
it touched was then blocked as `HOLD_POLICY_BLOCKED_LOW_CONFIDENCE` — a label
that blamed the desk for a number the harness had written. And because no capped
trade could execute, the outcome evidence the gate said it was gathering could
never arrive. That is why the comment reads "only 1 of 7 flagged trades has
resolved". Confirmed live: `NDAQ / cycle-v3-1785137616`, uncapped 62 → 60 →
`HOLD_POLICY_BLOCKED_LOW_CONFIDENCE`.

**Now:** same detector, moved in front of the decision.

1. `compute_contradiction_shadow(desk)` runs in the orchestrator's
   `board_of_directors` branch *before* `_run_board_of_directors`. At that point
   the desk holds no `final_decision`/`trade_decision`, so it cannot mistake the
   agent's own verdict for a corroborating source.
2. `build_dissent_block()` renders it; `agent_runner` injects it for
   `v3_board_of_directors` and `v3_decision_synthesizer` only, at `_KEEP`.
3. The agent answers in `dissent_resolution` — which desk it overrules and why.
4. `HOLD_POLICY_BLOCKED_UNRESOLVED_DISSENT` blocks a BUY/SELL that left it
   unanswered. **Fail-closed.** A HOLD never needs a resolution.

Confidence is never rewritten. See AGENTS.md §15 — **do not reintroduce a
numeric cap here.**

*Blast radius, measured:* under the live `DEBATE_ENGINE=3` the gate would have
fired **0 times in 9** actionable decisions since 07-31. The 26% rate over 21
days was almost entirely the retired tournament's action disagreeing with a
research desk; fundamental-vs-quant alone is 3/176 ≈ 1.7%. In every case where
the old cap blocked, this also blocks — so the worst case equals the old
behaviour, correctly labelled.

### 2. The delta tier now writes a `trade_results` row

**Was:** `_persist_trade_verdict` was gated on `has_artifact("trade_decision")`.
The delta tier publishes only `final_decision`, so it persisted **nothing** —
measured **40 of 40** delta analyses over 21 days with zero trade rows, **5 of
them holding real filled orders** (UNH, ALLY, AXP ×2, DIS). Invisible to P&L,
the scorecard, `record_strategy` and the LLM judge. `_persist_policy_action`'s
docstring justified this as "a path that never produced a trade decision" — true
of glance, false of delta.

**Now:** `_persist_trade_verdict` is a module-level function taking the decision
explicitly; the delta branch calls it. Glance still writes no row, correctly —
it is a hardcoded HOLD@0 from before any agent ran.

### 3. The implausible-level sanitizer runs before persistence on both paths

**Was:** the stop/target decimal-error check lived inside `_apply_policy_gates`,
so it ran wherever that chain happened to be called — and the two callers
disagreed. Full panel gated *before* building the result (drop reached the
executor) but *after* `save_trade_result`/`save_desk` (DB kept the bad number).
Delta gated *after* `_build_v1_compatible_result`, so the dropped level survived
in `result["estimate"]["stop_loss"]`, which pipeline_service hands straight to
`buy()` as a live stop order.

**Now:** extracted to `_drop_implausible_levels(desk)` — a sanitizer, not a gate.
Called at the top of `_persist_trade_verdict` (before the write) and again before
Layer 6 (covers desks that stop at the Board). Idempotent.

### 4. Policy-gate telemetry records a real `triage_tier`

`_record_gate` stamped `cycle_metadata["triage_tier"]`, which **nothing ever
wrote** — all 30 firings in 21 days recorded `null`, so the per-tier block rate
its own comment promised was unanswerable. Now written at every assignment.

### 5. One judge-confidence reader that handles both artifact shapes

`debate_judge` has two live writers: the judge agent emits
`winner`/`final_confidence` (the required schema, and with `DEBATE_ENGINE=3` the
**live** path), while the tournament copy and skip markers emit
`winning_side`/`confidence`. Two readers in `orchestrator.py` each knew only one
shape. New `_judge_confidence()` reads both. This is why the synthesizer's "only
when the verdict is low-confidence" deep-retrieval hook fired on every real judge
verdict — 18 of them in 14 days were actually ≥60.

## Open items

- **`dissent_resolution` adoption is unproven in production.** The prompt asks
  for it; no live cycle has produced one yet. Watch the first cycles where a
  `v3_dissent_*` event fires: if the board consistently omits the field, every
  dissented BUY blocks (same as the old behaviour, so no regression — but the
  agentic upside goes unrealised). If so, the fix is prompt-side, not a new gate.
- **`_persist_policy_action` still warns on glance-tier rows.** Expected and
  correct; the docstring now says so explicitly.
- **3 pre-existing ordering-dependent failures** in
  `tests/integration/test_connection_pool_exhaustion.py` when the whole suite
  runs (they pass in isolation). Some unit test leaks a patch on
  `app.db.connection`. Not caused by this work; not fixed here.
- The no-trade-available gate writes `risk_flags`, which silently arms the
  unmitigated-risk gate — contradicting its own comment that it "does NOT skip
  the Board". Left alone: all 7 boards that BUY'd after it emitted full
  mitigation, so it has never bitten. Worth a comment, not a fix.

## Gotchas

- **`_persist_trade_verdict` had to be hoisted to module level.** The delta
  branch runs at ~line 650, the closure was defined at ~line 1600 — a closure
  defined later in the same function is not bound yet. Verified the hoist was a
  pure move (body diff = 0 lines).
- **The dissent block deliberately returns `""` when the desks agree.**
  Announcing "no disagreement found" on every desk would teach the agent to read
  one absent conflict as confirmation. Do not "fix" the silence.
- **It also filters out `final_decision`/`trade_decision` as sources.** Those are
  the decider's own view; listing them would read as independent corroboration.
- `cycle_metadata` is persisted into `shared_desk.desk_data`, so both
  `dissent_detected` and `triage_tier` survive for replay — which is what makes
  the new gate measurable by `scripts/gate_ablation.py` (taught the new label).
- **`git stash` is repo-wide across sun worktrees.** Do not use the
  stash-and-retest trick here; use a second detached worktree at `master`.

## Where the reasoning lives

- `AGENTS.md` §15 — the dissent contract (detect → inject → answer → enforce).
- `docs/trading-cycle-verification-checklist.md` §5 — what to check on a cycle.
- Each fix carries its measurement inline in the code comment that replaced the
  wrong one.

## Tests

`tests/unit/test_cycle_contradictions.py` (new, 30 cases) pins all five, each
named for the contradiction it prevents. `tests/unit/test_stop_target_sanity.py`
was retargeted to `_drop_implausible_levels` and gained ordering assertions for
both triage tiers plus an explicit "glance is exempt and that is correct" case.
