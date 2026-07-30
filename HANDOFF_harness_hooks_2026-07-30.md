# HANDOFF — harness audit → invariant hooks (2026-07-29/30)

**All merged, pushed and deployed.** Last commit of this work: `0df184d`
(master has since moved on — another session added `bae38a8`, `ecfe530`,
`e722da6`; nothing of theirs conflicts with this).

Suite baseline: **2,099 passed, 19 skipped, 2 failed.** Both failures reproduce
identically on clean master — `test_whitelists_grant_write_to_pm_and_board_only`
(long-standing) and `test_prism_prompt_injection` (VLLM 10.0.0.141:8000
offline, environmental). Treat that as green.

---

## The one-paragraph version

The premise under test was *"the harness is over-engineered; filters conflict
and make behaviour worse."* Measured end to end: **wrong.** The gates barely
fire and the decisions they produce are directionally correct. What was
actually broken is that large parts of the system were **invisible** — paths
computing values written nowhere, artifacts losing fields on persist, a full
panel running on tickers with no data. Those are fixed. The durable output is
seven **invariant hooks** that turn "something silently didn't happen" from
archaeology into an alert.

---

## Findings that should change decisions

**1. The gates are not the constraint.** Over 133 decisions the entire 10-gate
cascade produced 13 blocks; six gates have never fired in recorded history.
`gate_ablation.py` replays the real `_apply_policy_gates` at **99.3% fidelity**
(148/149) over 1,311 desks.

**2. The board's decisions discriminate.** At h=1/3/5, BUY > HOLD every time;
at h=5, SELL isolates the worst names (−9.03%). **The zero-BUY state is not a
malfunction** — the bot declines things that then fall. Do not "fix" it.

**3. The tournament RANKS — do not delete it.** It was queued for deletion on
Brier 0.3090. Brier scores *calibration*; the board consumes `winning_side`, a
**label**. As a ranker: **AUC 0.608, Mann-Whitney p=0.0072**, bear-flagged
−2.85% vs bull −0.49%. It is the strongest single predictor of board action
(OR=6.5, p=3.2e-09). Recalibrate its confidence; keep its ordering.

**4. The confidence floor of 70 is correct.** Two measures disagreed; it is a
**horizon effect**. Confidence is a ~6-week signal (`decision_outcomes`, ~43d
lag: +4.8 to +6.3pp) and worth nothing at ≤20d. An isotonic fit independently
put the step at **exactly 70** (65→0.500, 70→0.560). `scripts/confidence_audit.py`.

**5. The synthesizer's downgrade earns its place.** Among board-≥70 decisions,
trades it cut returned −1.61% at a 32% win rate vs −0.34%/52% for those it kept
(**Fisher OR=2.30, p=0.039**). Do not remove it to unblock trades.

**6. Budget goes to deliberation, not research.** 128M tokens/7 days, ~1.2M per
ticker, buying **~6 external lookups per ticker**. `v3_tournament_debate` is
**31% of all tokens at loops=1.0 — zero tool calls.** Only junior (5.4) and
fundamental (5.8) actually research; `valuation_analyst` uses **one** tool.

---

## What shipped

| fix | evidence |
|---|---|
| Dead-ended pipelines no longer vanish | `INIT→PM_DONE` is illegal; `save_desk` sat inside the `try`, so ORCL lost 215s of work. 5 tickers in 2 days, rate rising 0→5→15% |
| Triage FULL-override now queues work | override set `triage=FULL` but every branch was guarded on `not fa_skipped` → queued **nothing** |
| Price pre-flight before the panel | LUCK emitted a decision at confidence 48 on **zero** price rows |
| Refresh the set we analyse | 127 of 199 watchlist tickers were outside the sp500-only refresh; now `sp500 ∪ watchlist ∪ positions` (636) |
| Survivors keep `direction` | 506/506 stored rows read back `"?"` |
| `get_sec_filings` | 142/510 calls (27%) rejected pre-execution on a key-name mismatch |
| 0-rowcount UPDATE now logged | delta/glance wrote `policy_action` to zero rows (~13% of tickers) |

Plus three instruments: `scripts/gate_ablation.py`,
`scripts/score_tournament_ranker.py`, `scripts/confidence_audit.py`.

### The invariant hooks (`app/v3/invariants.py`)

Per-ticker (orchestrator, end of `run_v3_pipeline`):
`TICKER_ANALYSED_BUT_NO_DESK`, `DESK_PERSISTED_BUT_NO_TRADE_ROW`,
`PIPELINE_COMPLETE_BUT_NO_DECISION`, `ARTIFACT_FIELD_LOST_ON_PERSIST`

Per-cycle (`pipeline_service`, on `status=done`):
`ANALYSED_UNIVERSE_NOT_REFRESHED`, `TOOL_FAILURE_RATE_CEILING`,
`DECISION_DISTRIBUTION_DRIFT`, `AGENT_BURNS_TOKENS_WITHOUT_RESEARCH`,
`TELEMETRY_ATTRIBUTION_DECAY`

Violations land in `v3_invariant_violations`. **Records, never raises** — an
observer that can abort a cycle is a new failure mode.

---

## Open work, in priority order

### 1. A sixth per-cycle invariant — closes a hole I created

**`HOOD` stalled at `DEBATE_DONE`**: desk written, no `analysis_results` row, no
`trade_results` row, work abandoned mid-flight. **6 of 204 desks (~3%) in 7
days.** Invisible to all seven checks, because `check_ticker_complete` runs at
the *end* of a pipeline HOOD never reached, and every cycle-level check keys off
`analysis_results` — the table whose absence *is* the symptom.

Fix: at cycle completion, assert every `shared_desk` row for the cycle reached a
terminal phase (`PM_DONE`, `ABORTED`, or `INIT` for a legitimate triage skip).
**Key it off `shared_desk`, not `analysis_results`.**

> Method note worth keeping: keying observability off the same table the bug
> corrupts builds a blind spot. I did exactly that.

### 2. `PreToolUse` — the biggest lifecycle gap

Every tool problem this session was caught *after* the fact. A pre-hook would
have injected the missing ticker *before* `get_sec_filings` failed 142 times.
Mostly **unification, not new machinery** — `ToolLoopDetector`, the whitelist
resolver and `ToolCallGuard` (lazy-agent-service) already do pieces of this.

### 3. `SubagentStart`/`SubagentStop` + a hook registry

Per-agent cost at the boundary instead of reconstructing it from telemetry —
that reconstruction is what surfaced the tournament finding. A registry makes
hooks declarative, discoverable and **individually toggleable**, hence ablatable
via `gate_ablation.py`.

### 4. Rebalance the budget

Switch `DEBATE_ENGINE=1` (the probabilistic panel is built and wired) to free
~40M tokens/week, then spend it on research loops gated by an
evidence-completeness check rather than a fixed loop count.

### 5. Cross-repo: tool attribution

`agent_name`/`ticker` reach `tool_usage_stats` empty. Root cause is **two call
sites in `lazy-agent-service`**: `McpAdapter.ts:70` passes no context at all,
`ToolOrchestratorService.ts:1583` passes agent+cycle but never ticker. It is a
**regression** — attribution was 100% in June, 0.6% by 07-27. Not fixed here:
different repo, shared service (html-notes/canvas/music route through the same
file).

**But note:** `agent_tool_telemetry` (written by the `_on_tool_result` hook)
**already has full attribution** and answers "which agent researches". I fixed
the broken duplicate before noticing. The two tables are **not** redundant —
`tool_usage_stats` consumers (`tool_optimizer.py:58`, `reflector.py:176`) read
only `tool_name`/`success`/`execution_ms`/`called_at`, all correct. Retiring
either breaks a live consumer.

---

## Traps

- **`PooledCursor` has no `rowcount`** and no `__getattr__`. Read it off
  `cur._cursor` — `getattr(cur, "rowcount", -1)` silently returns −1 forever.
- **`_apply_policy_gates` imports `get_param` inside the function body**, so
  patching the orchestrator attribute never intercepts it. Patch
  `parameter_store`.
- **`runtime_parameters` is empty by design** — resolution falls through to
  registry defaults, so historical parameter values are recoverable only from
  git. The floor moved 65→70 as a *code default*.
- **A worktree missing `tool_schemas.json`** silently halves tool counts and
  looks like a refactor bug.
- **Verify a detector fires, not just that it's silent.** Two of five cycle
  checks passed the silence test and would have missed their own motivating
  defect. Drift needed a 150-decision baseline (adjacent windows showed +5pp;
  the real shift was +28pp) and the cost check was summing per-cycle, which
  scales with ticker count.
- **A probe that sets its own context proves nothing** — it tests the sink, not
  the producer.

## Loose end

`.claude/worktrees/fidelity-followup` is fully merged but still checked out (it
was a session cwd, so it could not remove itself). `git worktree remove --force`
it and delete branch `worktree-fidelity-followup`.
