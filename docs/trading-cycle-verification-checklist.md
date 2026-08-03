# V3 Trading Cycle — Verification Checklist

How to verify a V3 cycle end-to-end after any change. Each item lists the check, where to look,
and what "working" means. Audit of `cycle-v3-1784026688` (2026-07-14) — status per item at time of writing.

DB: `postgresql://trader:…@10.0.0.16:5433/trading_bot`. Cycle logs: `/app/logs/cycles/<cycle_id>.jsonl` in the container.

## 1. Command intake & lifecycle
- [ ] `v3_system_commands` row moves pending → running → completed; `result.cycle_id` present.
  NOTE: `completed` means *launched*, not finished — the poller must stay free to process STOP_CYCLE,
  so it cannot await the cycle. Track real progress via `pipeline_state` / `GET /status`, not this row.
- [ ] Payload `tickers` are honored: `cycle_run_summaries.tickers_requested` must equal the payload list,
  and every requested (US-resolvable) ticker must be analyzed. **WAS BROKEN** — requested tickers were
  only "seeds" into discovery/scoring/gatekeeper and could be entirely replaced (AMD/UBER/GOOG → AAPL/JPM/FCF).
- [ ] Payload `trade`/`collect`/`analyze` flags are honored and recorded. **WAS BROKEN** — flags were
  ignored (a `trade:false` cycle placed real paper orders) and the summary recorded hardcoded defaults.
- [ ] `max_tickers` respected (gatekeeper prompt previously hardcoded 15).
- [ ] Duplicate START while running → `deduplicated` result (works).

## 2. Ticker selection (auto-discovery path, empty `tickers`)
- [ ] Discovery engine merges watchlist + 24h news/reddit/youtube trends + institutional leads.
- [ ] FALSE_TICKERS and non-US tickers filtered; gatekeeper hallucinations dropped against the pool.
- [ ] Freshness gate skips stale names and logs a `freshness_gate/STALE_SKIPPED` event with reasons.
- [ ] Gatekeeper timeout (180s) falls back to top scorers instead of hanging the cycle.

## 3. Per-ticker pipeline (events in `pipeline_events`, one sequence per ticker)
- [ ] `v3_start` → `v3_precollect*` (fast-path or scrape) → `v3_ctx` (SharedDesk created) → `v3_triage`.
- [ ] Regime engine runs and classifies (beware: default regime is CONTRADICTORY, so a silent
  regime failure still looks like a "classification" — check the regime event exists).
- [ ] Research layer: junior, fundamental, quant analysts all reach `_done` with `SUCCESS` in
  `v3_agent_telemetry` and quality scores > 0.
- [ ] Agents use whitelisted tools (verify via prism stream / `agent_tool_telemetry`, not code — see
  memory note on prism-side discovery leakage).

## 4. Debate layer (tournament mode)
- [ ] `v3_tournament_*` events: 4 persona pitches → backtest filter → head-to-head → jury.
- [ ] `debate_history` row written per ticker: `winner`, `final_action`, `final_confidence`,
  `pro_argument`, `con_argument`, `persona_outcomes` populated. (`thesis_*`/`counter_*` columns are
  legacy from the pre-tournament design and stay NULL — expected.)
- [ ] Board of Directors actually SEES the tournament verdict in its compressed context.
  **WAS BROKEN** — writer used `winning_side`/`confidence`, reader looked for `winner`/`final_confidence`,
  so every board saw "Winner:  @ 0% confidence".
- [ ] Veto semantics: jury veto encodes as `vetoed=true` (confidence 0 alone is ambiguous), and
  HOLD→SELL remapping only happens when position context justifies it.
- [ ] Tournament sub-agents are absent from `v3_agent_telemetry` (they bypass `run_v3_agent`) — known
  observability gap, not a failure.

## 5. Decision & policy gates
- [ ] Board `final_decision` → `v3_decision_synthesizer` → `trade_decision` artifact; `v3_policy_*`
  event shows the gate outcome (EXECUTE_BUY / EXECUTE_SELL / BLOCK_*).
- [ ] `analysis_results` row per ticker with the final verdict; `d_result` in the result JSON populated
  in tournament mode (**WAS BROKEN** — always null in tournament cycles).
- [ ] Every policy firing in `v3_guardrail_firings` carries a non-null `detail->>'triage_tier'`
  (**WAS BROKEN 2026-08-03** — nothing wrote `cycle_metadata["triage_tier"]`, so all 30 firings
  in 21 days recorded null and per-tier block rates were unanswerable).
- [ ] When the desks disagree on direction, a `v3_dissent_*` event fires BEFORE the board runs and
  the board's context carries the `UNRESOLVED CROSS-DESK DISSENT` section. A BUY/SELL that answers
  it in `dissent_resolution` executes at its stated confidence; one that does not is blocked as
  `HOLD_POLICY_BLOCKED_UNRESOLVED_DISSENT` (**never** as LOW_CONFIDENCE — that label means the desk
  chose a low number, not that we overwrote its high one).
- [ ] No decision carries `confidence_cap_reason`. The post-hoc cap-to-60 was removed 2026-08-03;
  it always collided with the 70 floor and turned a documented "not a downgrade" into a guaranteed
  block.
- [ ] Implausible stop/target levels are dropped BEFORE `save_trade_result` and before the result is
  built, on **both** the full-panel and delta paths — `trade_results.stop_loss` must never hold a
  value that `DROPPED_IMPLAUSIBLE_LEVEL` was recorded for.
- [ ] `v3_delta` tier writes a `trade_results` row (**WAS BROKEN** — 40 of 40 delta analyses had no
  row, 5 of them with real filled orders). `v3_glance` legitimately writes none.

## 6. Trade execution
- [ ] Only runs when the cycle was started with `trade:true`. **WAS BROKEN** (always ran).
- [ ] Confidence threshold (`ANALYSIS_CONFIDENCE_THRESHOLD`) blocks low-confidence BUY/SELL.
- [ ] `orders` rows created with `signal='pipeline'`; stop-loss/take-profit triggers created when the
  decision includes them.
- [ ] Trade failures recorded on the analysis result (`trade_failed`) and counted in the summary.

## 7. Cycle summary (`cycle_run_summaries`)
- [ ] Exactly one row per cycle, status `done` (or `error`/`stopped` — a row must exist even on failure;
  previously failed/cancelled cycles left NO row and vanished).
- [ ] `analysis_results_count` / `buy_count` / `sell_count` / `hold_count` match `analysis_results` for
  the cycle. **WAS BROKEN** — `_process_ticker` returned None so all counts were 0.
- [ ] `elapsed_ms` > 0 (**WAS BROKEN** — never computed), flags match the payload,
  `trade_attempted/executed/failed` match `orders`.

## 8. Autoresearch (post-cycle audit, `autoresearch_reports`)
- [ ] AUTORESEARCH command enqueued at cycle end and processed by the eval worker.
- [ ] `performance_metrics` non-zero for a real cycle. **WAS BROKEN** — read the zeroed summary.
- [ ] `reflection.fallback` is absent/false — i.e. the LLM reflection actually ran. **WAS BROKEN** —
  imported deleted `app.services.vllm_client`, silently fell back to canned text every cycle.
- [ ] `llm_performance_score` varies with real tracker data. **WAS BROKEN** — stuck at exactly 50.0
  because `app.pipeline.subsystem_benchmarks` (deleted tree) raised ImportError → default 0.5.
- [ ] `decision_issues` does not claim "No decisions produced" when decisions exist.
- [ ] Directives (`cycle_directives`) generated when reflection produces recommendations.
- [ ] Known-dead paths (do not expect data): `critic.py`/`critic_feedback` (no caller),
  `run_eval_worker` (no scheduler), `janitor_run_log` (janitor never writes it),
  `debate_tool_cache` (no reader/writer).

## 9. Robustness / observability
- [ ] No unhandled exceptions in container logs during the window (`sudo docker logs trading-service`,
  NAS clock is **PDT**, DB timestamps are UTC — convert before `--since`).
- [ ] A container restart mid-cycle is detected on boot (`detect_and_log_crashed_cycles`) and the
  orphaned pipeline state auto-clears after 30 min.
- [ ] `agent_traces`/`llm_traces` written for eval; `v3_agent_telemetry` rows have `quality_score`.
- [ ] Swallowed-exception hotspots to keep an eye on: debate_history write, telemetry flush,
  per-pitch tournament failures, the whole autoresearch stage chain — all log-only.

## 10. Regression tests
- [ ] `venv/bin/python -m pytest tests/unit tests/regression -q` green locally.
- [ ] New guards: summary counting, trade-flag gating, ticker pinning, debate-context keys,
  autoresearch import health (see `tests/unit/test_cycle_summary_builder.py`,
  `test_shared_desk_debate_context.py`, `test_autoresearch_import_health.py`).
