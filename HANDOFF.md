# HANDOFF — AI Analysis Overlays fixed (cycles stopped charting)

**Commit:** `f5f7bb7` · deployed to synology `2026-07-20T22:43:56Z` · verified live.

---
## Follow-up (same session): ticker-concurrency cap — commit `344dadc`
A full-watchlist (35-ticker) `trade=true` cycle DEADLOCKED: `_run_all_v3` fanned out
every ticker via `asyncio.gather` with no bound; each ticker pipeline borrows several DB
connections, so 35 at once exhausted the pool (max_size=50) — the STOP_CYCLE poller
couldn't even get a connection, and cycle_main's loop hung (:3031 → 000). Recovered with
`deploy.sh --restart-only`. Fix: `MAX_CONCURRENT_TICKERS` (default 6, env-overridable) +
an `asyncio.Semaphore` gating `_process_ticker`; large watchlists now run in waves. The
5-ticker `trade=true` cycle before this fix DID complete (charts on all 5, TSM+NVDA BUYs)
— 5 was under the implicit safe limit. See memory `cycle-ticker-concurrency-deadlock`.
Liveness note: debate/board agents emit ~no tool telemetry — judge progress by
`shared_desk.phase`/`updated_at`, and read executed trades from `trade_results`.

---

## Symptom
Ticker-detail "AI Analysis Overlays" chart showed no agent-drawn support/resistance
lines. TSM (and most tickers) 404'd on `/charts/{ticker}.json`.

## Root cause
The chart depended on the quant analyst tool-calling `save_trading_chart` mid-loop
(step 5 of its execution loop). Commit **af940bc** (2026-07-18 17:06, "execution-loop
prompt rewrite -33% tokens") compressed that loop into a terse numbered list. After it
deployed, the model began emitting its final JSON after ~6 tool turns and **skipping
steps 5-7** (`save_trading_chart`, `whiteboard_write`, `whiteboard_annotate`) — even
though the quant turn budget is 12.

Evidence (agent_tool_telemetry):
- Last cycle-generated chart: **2026-07-18 21:52** (AAPL). Zero `save_trading_chart`
  calls on 07-19/07-20.
- 07-20 quant runs stop at 6 tools: `discover_and_enable_tools, whiteboard_read×2,
  get_technical_indicators, get_market_data, run_equation` → final JSON. Never reaches
  the chart or whiteboard.

The migration (schemas → lazy-tool-service, MCP bridge) was NOT the cause — other MCP
tools work, and charts write to the shared `/app/data/charts` volume that
`chart_router` (:3031) serves. The old charts landed there fine.

## Fix — decouple chart persistence from the mid-loop tool call
- `quant_report` artifact gains an `overlays` field (`artifacts.py`). Models reliably
  fill an output field even when they skip a tool call.
- `quant_analyst.py` prompt routes S/R zones + trendlines into `overlays` and drops the
  per-cycle `save_trading_chart` mandate; re-flags `whiteboard_write/annotate` MANDATORY.
- `run_v3_agent` (`agent_runner.py`) persists the chart itself after parsing the
  artifact via `_persist_quant_chart()` → `save_trading_chart(...)`. Writes to the
  shared charts volume regardless of whether the tool was called.
- `_fallback_overlays_from_metrics()` synthesizes a stop-loss line if `overlays` is
  empty, so the chart is never blank.
- `save_trading_chart` stays whitelisted; the on-demand "Run AI Analysis" button
  (chart_router `_run_quant_analysis`, its own MANDATORY-tool user prompt) is unchanged.

## Verification
Triggered a no-trade cycle for TSM (START_CYCLE row in `v3_system_commands`).
`/charts/TSM.json`: 404 → 200 with **4 quant-authored overlays** (SMA-200 support,
immediate support, SMA-50/20 resistance, 52-wk-high supply) — and the quant analyst
made **zero** `save_trading_chart` tool calls that run. The deterministic hook did it.

## Open / follow-up
- The same 07-18 compression regressed `whiteboard_write/annotate` adherence — the
  Board debates over the quant's posted numbers. Re-emphasized in the prompt this wave,
  but confirm whiteboard writes recover on the next few cycles (query
  `agent_tool_telemetry` for `whiteboard_write` by v3_quant_analyst).
- `lazy-agent-service/python/app/...` holds a stale COPY of these files (charting_tools,
  agent_runner, quant_analyst, artifacts). The live cycle runs in trading-service, so it
  was NOT edited. If anything ever runs agents from that mirror, hand-sync this fix.
