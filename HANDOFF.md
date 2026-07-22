# HANDOFF — Agent research-quality wave (audit-driven replacement for the Wave-1 checklist)

**Commits:** `bad7904` + `89586f6` + `db75b5f` + `fbfe0be` (trading-service),
`a0533a6` + `98420d5` (lazy-agent-service) · deployed to synology (final
deploy `fbfe0be` 2026-07-22 ~14:25 PT) · prism personas synced live (11
personas incl. new CUSTOM_V3_DELTA_ANALYST).

## ✅ VERIFIED by live cycle `cmd-verify-e56fcafa` (NVDA, analyze-only, ~25 min)

- **Quant analyst ran 15 loops (7-day average was 1.6), quality 81** — the
  precomputed block + prompt rewrite genuinely woke it up. Its report carries
  the full new schema: vol_signal=EXPANSION, predicted_vol_annualized_pct,
  vol_prediction_premium; summary cites GARCH. (diversification_ratio /
  hrp_weight_suggestion were null — correct: the active bot held only NVDA,
  no book to diversify against; the HRP line appears from 2+ positions.)
- **Regime scored the new factors from the FRED lines exactly per guidance**:
  yield_curve 0.35 (+0.39pp flat-ish curve), credit_stress 0.2 (2.69pp calm).
- **trade_results row**: HOLD @ 45, internal_consensus_score=55 persisted,
  dynamic_trigger={"type":"breakout_reclaim","value":213.99} — numeric value
  on a HOLD, no more null-value dead watches.
- **Zero dangerous meta-tools** (no execute_command/write_file/execute_python
  anywhere). One discover_and_enable_tools from the junior — its run started
  BEFORE the delta-persona fix below; expect zero next cycle.
- **Found + fixed during verification**: the junior/delta persona UNION left
  3 tools of permanent discovery headroom (junior's requests never enable
  delta's tools) → delta now has its own CUSTOM_V3_DELTA_ANALYST persona
  (POST /custom-agents) and `prism_agent_registry` maps to it (`fbfe0be`).
- Watchpoint: the synthesizer invented trigger type "breakout_reclaim" —
  order_triggers only evaluates sma_*/rsi_*/trailing_drop dynamic types, so
  unknown types register but never fire (pre-existing gap, now at least
  visible in the persisted column). Tournament artifact quality scored 9 this
  run — the pitch invalidation fields came back empty; watch whether pitches
  fill them over the next cycles.

## Context

The user proposed a "Wave 1 checklist" (synthesizer weight/size formulas in
prompts, regime ^TNX fetches, bull/bear schema fields). The audit
(`.agents/AUDIT-agent-research-2026-07-21.md`) rejected 5 of its 7 commits:
premises contradicted by live data, formulas placed in prompts that telemetry
proves agents ignore (quant avg 1.6 loops of 14; regime 1.1; board 1.0 with
zero tool calls ever), verify queries against nonexistent columns, and
bull/bear edits targeting a path that runs ~6×/14d while the tournament runs
176×. This wave implements the audit's replacement plan.

## What shipped (trading-service `bad7904`)

1. **Precomputed quant math** — `app/quant/context_block.py` computes GARCH
   forecast, HRP target weight + diversification ratio + covariance
   condition, drift breaches, and strategy health in CODE at desk build
   (~5s, off-loop, 25s timeout, fail-open). Injected as a "PRECOMPUTED QUANT
   MATH" section for `v3_quant_analyst` + `v3_board_of_directors` only.
   Quant prompt now says "cite the block; tools only for deeper dives".
   This is the fix for the audit's headline finding: the 07-21 GARCH/HRP
   tools were behind tool calls the quant never makes.
2. **Regime enrichment in code** — `fred_curve_credit_lines()`
   (retrieval_context) appends FRED 10Y−2Y (INVERTED flag) + HY OAS lines to
   the macro briefing; regime schema gains `yield_curve` + `credit_stress`
   factors with scoring guidance. Zero new fetches.
3. **Artifact validators** — `app/v3/artifact_validators.py`, wired into
   agent_runner post-parse (aliased import `_coerce_artifact`; a plain
   import shadowed the existing `validate_artifact` from app.v3.artifacts
   and crashed every run — caught by tests). Regime enum coercion (fixes
   literal `"HIGH_VOLATILITY|DEEP_DISCOUNT|CONTRADICTORY"` rows), factor
   clamping to [0,1], dynamic_trigger normalization: **a null trigger value
   made order_triggers skip the watch forever** (`value is not None` gates
   the whole dynamic branch). trailing_drop defaults 0.10; sma_*/rsi_* get a
   0.0 placeholder (evaluation compares vs the live metric); unknown types
   without a value are dropped.
4. **Persistence** — `trade_results` + `internal_consensus_score` (INTEGER)
   + `dynamic_trigger` (JSONB), migration + saver + Mongo mirror. Both were
   artifact-only before; the checklist's own verify queries were impossible.
5. **Synthesizer prompt** — trigger value REQUIRED (example no longer shows
   null); told that consensus/data_quality now mechanically scale size.
6. **Code-side sizing** — `resolve_buy_size_pct(consensus_score,
   data_quality)`: explicit sizes ×= max(0.5, consensus/100), then ×0.5 when
   board `conviction_vector.data_quality < 60`. Fallback/watch-only paths
   unchanged.
7. **Debate schema where debate actually runs** — tournament pitch prompt +
   result + h2h now carry `invalidation_condition` + `catalyst_window`,
   surfaced in the compressed desk context for the board's stop/trigger
   calibration. Bull agent got the same fields for the fallback path.
8. **skip_debate honored, safely** — `_queue_debate_phase` skips the
   ~9-min tournament ONLY when the regime engine suggested it AND its own
   volatility factor ≥ 0.9; the skip writes a stub `tournament_result` with
   `risk_flags=["debate_skipped_by_regime"]`, so the existing
   UNMITIGATED_RISK gate demands stop+trigger+size for any trade (the jury
   veto's replacement). Board queued directly.
9. **Meta-tool lockdown (audit F2)** —
   - `scripts/sync_prism_v3_personas.py`: pins each CUSTOM_V3_* persona's
     `availableTools` AND `enabledByDefaultTools` to the code whitelists in
     MCP naming (`mcp__lazy-tool-service__*`); tool-less personas get a
     `__no_tools__` sentinel because an EMPTY availableTools means UNSCOPED
     (full-catalog discovery headroom). **Gotcha: PUT /custom-agents/:id
     wants the Mongo `_id`, not the agentId** (500 "24 character hex"
     otherwise). Ran live: all 10 personas pinned, 0 missing.
   - `base_agent` also advertises MCP-prefixed name aliases in enabledTools
     (plain names ≠ persona's MCP names was the "discovery headroom" that
     let live agents reach execute_command/write_file).
   - Re-run the sync script after ANY whitelist change.

## lazy-agent-service (`a0533a6`, `98420d5`)

The four 07-21 portfolio-math tools had **no lazy-tool schema — they were
unreachable from prism agents entirely**. Added to
`tool_schemas/trading/general.json`, rebuilt flat artifacts via
`trading-service/scripts/build_tool_schemas.py` (fans out to
lazy-agent-service + trading-service + trading-client roots; trading-client's
copy is gitignored — derived artifact, nothing to commit there), synced
`python/tool_schemas.json` mirror, deployed. Verified live: bridge executes
`get_strategy_health` end-to-end through `/api/v1/agent-tools/execute`
(bearer `API_SERVER_KEY`).

## Verification

- 958 unit tests pass (21 new in `test_agent_research_wave.py`); the only
  failure (`test_call_prism_agent_prepends_system_prompt`) is pre-existing
  and network-flaky — fails identically on a clean tree.
- Live smokes: quant block on NVDA (GARCH EXPANSION 48.6% vs 35.4%, HRP 1.0%,
  4 drift breaches, 5.0s); FRED lines (10Y−2Y +0.39pp, HY OAS 2.69pp calm);
  migration columns present; personas pinned; new tool executes via bridge.
- Verification cycle `cmd-verify-e56fcafa` (NVDA analyze-only) inserted —
  when it completes, check: regime factors include yield_curve/credit_stress;
  quant report cites the precomputed GARCH/HRP numbers; trade_results row has
  internal_consensus_score + dynamic_trigger; `agent_tool_telemetry` shows
  **zero** discover_and_enable_tools/search_tools/enable_tools calls for the
  cycle.

## Watchpoints / follow-ups

- The meta-tool lockdown's real proof is telemetry over the next days:
  `SELECT tool_name, count(*) FROM agent_tool_telemetry WHERE created_at >
  now() - interval '1 day' AND tool_name IN
  ('discover_and_enable_tools','search_tools','enable_tools','execute_command','write_file')
  GROUP BY 1` should trend to zero for v3_* agents.
- skip_debate has never fired (regime is 97% DEEP_DISCOUNT/CONTRADICTORY);
  it will first trigger in a real vol spike — the stub artifact path is
  unit-tested but not cycle-tested.
- Deferred from the audit: search reliability (DDG-lite 20% failure — the
  single biggest research-quality lever left), junior-analyst depth (scrape_url
  never used), tournament cost (554s avg — early-exit/caching), the "?"
  unattributed agent rows, FA sec_filings underuse.
