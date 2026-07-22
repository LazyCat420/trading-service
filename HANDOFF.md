# HANDOFF — Portfolio-math wave: HRP sizing, LW shrinkage, GARCH vol, strategy-health gate

**Commit:** `52b22ba` · deployed to synology `2026-07-22T04:50:08Z` · verified live
(health OK, all 4 new tools registered in-container, `get_pipeline_health()` → NORMAL
against production telemetry).

---

## What shipped

The quant agent had solid single-ticker risk tools but **zero portfolio-level math** —
every sizing decision was made blind to how correlated the new position is to the book,
volatility_regime was backward-looking (ATR vs its 30d average), and nothing checked
whether the models themselves were degrading. This wave adds all three layers.

### New package: `app/quant/`
- **`portfolio_math.py`** — Ledoit-Wolf (2004) shrinkage to scaled identity +
  condition number; HRP weights (Lopez de Prado: corr→distance→single-linkage→
  quasi-diag→recursive bisection — never inverts Σ); diversification ratio;
  `apply_view_tilt` (simplified BL-style confidence tilt, multiplier clipped
  [0.25, 2.0], renormalized); `rebalance_drift`. Pure functions, no I/O.
- **`garch.py`** — GARCH(1,1) fit by scipy Nelder-Mead MLE, next-day vol forecast,
  prediction premium vs 20d realized, `vol_signal` EXPANSION/CONTRACTION/NEUTRAL
  (±10% band). Hand-rolled: container has no `arch`/`sklearn`/`riskfolio`, and the
  equation sandbox only allows numpy/pandas imports anyway.
- **`returns.py`** — aligned log-returns matrices straight from Postgres
  `price_history` (2,740 tickers, decades deep) — no per-ticker Polygon fan-out.
  Drops columns under 60% window coverage (a thin column poisons every pairwise
  estimate), reports them.
- **`strategy_health.py`** — "has the model degraded" separate from "is it losing
  money". Reads `quality_score` history (0-100, well-populated: healthy agents run
  ~75-85) from `v3_agent_telemetry` for the decision-critical agents (quant, board,
  synthesizer). avg < 45 → **CUT**; avg < 60 or slope < −0.25/run → **REDUCE**;
  <10 samples → NORMAL. 10-min cache, computed on read — **no new table**. Fails
  OPEN everywhere: a broken health check can never block (or unblock) trading.

### New tools (registered + whitelisted)
| Tool | Where |
|---|---|
| `get_portfolio_covariance` | quant, user_chat |
| `calculate_hrp_allocation` (candidate_ticker + JSON views) | quant, board, user_chat |
| `forecast_volatility_garch` | quant, user_chat |
| `get_strategy_health` | board, user_chat |

Quant whitelist stays under the 20-tool cap by dropping `calculate_position_size`
(the flat cash-percent sizing HRP replaces; still on user_chat) and
`save_trading_chart` (prompt already said not to call it — overlays auto-render).
Quant turn budget 12 → 14. `v3_decision_synthesizer` deliberately untouched — it has
no tools by design; portfolio math reaches it through the quant report artifact.

### Enforcement
- **Policy gate** (`_apply_policy_gates`): BUY + pipeline health CUT →
  `HOLD_POLICY_BLOCKED_DEGRADED_MODEL`. SELLs always pass — a degraded model must
  still be able to de-risk.
- **Sizing** (`pipeline_service`): health REDUCE halves every BUY size
  (`apply_health_sizing`, pure + tested), tags `result["strategy_health"]`.
- **Quant prompt**: GARCH-informed volatility_regime (EXPANSION escalates the regime
  one notch); portfolio step for BULLISH theses (`calculate_hrp_allocation` with
  candidate); new schema fields `diversification_ratio`, `vol_signal`,
  `vol_prediction_premium`, `predicted_vol_annualized_pct`, `hrp_weight_suggestion`.
- **Equation-library df**: new columns `gk_vol` (Garman-Klass) and
  `mom_21d/63d/126d/252d` (clipped at 99.5th pct) for library equations.

## Verification
- 938 unit tests pass (16 new in `test_portfolio_math_wave.py`: LW conditioning
  improvement at T≈N, HRP underweights high-vol, DR=√n for iid, GARCH recovers
  clustering on synthetic data, health trend detection, gate block/fail-open).
- Live-DB smoke: 8-ticker holdings → shrinkage 0.04, cond 56.7, HRP gives NVDA 1.1%
  (highest vol — covariance talking), DR 2.04; NVDA GARCH: 48.8% predicted vs 37.9%
  realized → EXPANSION; health NORMAL (avgs 81-84).

## Deliberately NOT done (from the Ruuj/freeCodeCamp plans)
- **Full Black-Litterman** — needs Σ⁻¹, the exact instability HRP avoids; the view
  tilt covers the use case. The claim "confidence 55 triggers the same BUY as 90"
  was false anyway (confidence floor gate + confidence-scaled fallback sizing).
- **`strategy_health` table** — computed on read from telemetry; no dual-write burden.
- **GARCH/DR as library equations** — sandbox is single-ticker df + numpy/pandas-only
  imports; they'd die on the import line. They're tools instead.
- **K-Means discovery clustering, Fama-French betas** — separate data-pipeline
  projects (FF factor source needed; Gatekeeper runs with tools disabled).
- **Engagement-ratio social signal** — no likes/shares/reach data exists anywhere
  in the DB.
- **Frontend drift alerts** (`ActiveProfileHeader`) — agents get drift from
  `calculate_hrp_allocation` (`drift_breaches_over_5pct`); a client surface needs an
  API endpoint + trading-client work. Backlog.

## Watchpoints for the next cycle run
- Quant runs should now show `forecast_volatility_garch` + `calculate_hrp_allocation`
  calls in telemetry; check the report JSON carries `diversification_ratio`/`vol_signal`.
- GARCH tool is ~0.5-1.5s of CPU per call (Python-loop MLE) — fine per ticker, watch
  if anything starts calling it in a loop.
- If health ever reads REDUCE/CUT, the driver + reason are in the gate/pipeline logs
  (`[StrategyHealth]`, `HOLD_POLICY_BLOCKED_DEGRADED_MODEL`).
