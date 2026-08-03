# HANDOFF — Component efficacy monitor: is the HMM earning its keep? (2026-08-03)

## What is live right now

The trading cycle now has a scheduled answer to "is this expensive component
helping?", starting with the HMM regime shadow — the full audit and design
rationale is in
[`docs/AUDIT_COMPONENT_EFFICACY_2026-08-03.md`](docs/AUDIT_COMPONENT_EFFICACY_2026-08-03.md).

- `app/autoresearch/component_health.py` grades the HMM's stored daily
  posteriors every weekday at **5:45 PM PT** (after the 5:30 snapshot):
  Kupiec band coverage, Diebold-Mariano vs the FREE trailing 20-day σ
  (QLIKE + MSE), and operational health (snapshot gaps, stale-tape runs).
  Verdicts: `insufficient_data` / `healthy` / `redundant` / `failing`.
- **3 consecutive daily `failing` verdicts → auto-disable**: the monitor
  proposes `HMM_REGIME_MODE` 0→1 through the parameter governor and writes a
  ⚠️ agent note. Mode 1 = desk path skips the fit (frees ~22–32s of the
  45s quant budget), prompt line withheld, **daily snapshot + grading
  continue**. Mode 2 (fully off) is human-only. The monitor never re-enables.
- `GET /api/v1/component-health` (+ `/history`) serves the latest verdict,
  the real thresholds, and the mode semantics. Reports persist in
  `component_health_reports` (table auto-created).
- Grading math is shared: `app/quant/regime_grading.py` is imported by the
  monitor, `scripts/grade_hmm_regime.py`, and `scripts/vol_forecast_race.py`.

First live reading (read-only dry run): **redundant** — calibrated band
(Kupiec p=0.384, n=119), loses to free on MSE only (t=+2.38), ops healthy.
Matches the pre-registered 08-03 experiments; no auto-disable fires today.

## Open items

1. **The human call the monitor will keep surfacing:** the HMM is `redundant`
   — not harmful, not better than free; its unique outputs are the state
   label/duration/switch odds at ~22–32s/cycle. To take the cost off the desk:
   propose `HMM_REGIME_MODE=1` via chat. Grading continues either way.
2. **trading-client has no panel for `/api/v1/component-health` yet.** The
   endpoint serves everything needed (verdict definitions + thresholds
   included, eval-trust convention). Until then the JSON is readable directly.
3. First scheduled run is the next weekday 5:45 PM PT — check
   `component_health_reports` has a row after it.

## Gotchas

- **`redundant` does NOT auto-disable — by design.** Only demonstrated harm
  (band too NARROW, worse than free on BOTH losses, snapshot gap ≥4 trading
  days, stale run ≥3) counts toward the 3-strike disable. Don't "fix" this
  by making redundant count; that call was deliberately left to the user.
- **Fail-open direction is ACTIVE (mode 0)** — a store failure resurrects the
  prompt line rather than silently retiring the component. Commented in the
  registry entry; don't invert it.
- `component_health_monitor` is in `STANDARD_TIER_AGENTS`
  (parameter_validator.py) — it is not an LLM; it's the monitor's audited
  path to its one proposal. Removing it from the set silently breaks
  auto-disable (the governor would reject with "not authorized").
- The old grading names (`_load_posteriors`, `predictive_band`, …) are
  re-exports in `scripts/grade_hmm_regime.py`; `vol_forecast_race.py` and
  `test_hmm_grading.py` import them from there. Keep the aliases if you move
  things again.

## Where the reasoning lives

`docs/AUDIT_COMPONENT_EFFICACY_2026-08-03.md` (this wave) ·
`experiments/exp-2026-08-hmm-regime-overlay.md` and
`exp-2026-08-hmm-vol-forecast-value.md` (the measured record the thresholds
encode) · `scripts/power_report.py` (why P&L is not a verdict input) ·
parameter registry comments on `HMM_REGIME_MODE` / `TOURNAMENT_DEBATE_MODE` /
`DEBATE_ENGINE` (the retire-by-measurement pattern this follows).

Previous handoff (portfolio import, 2026-07-27) archived to
[`docs/HANDOFF_portfolio_import_2026-07-27.md`](docs/HANDOFF_portfolio_import_2026-07-27.md);
its unrun post-deploy checklist in
[`docs/HANDOFF_skill_gate_2026-07-27.md`](docs/HANDOFF_skill_gate_2026-07-27.md)
§"Verify next cycle" is still unrun.
