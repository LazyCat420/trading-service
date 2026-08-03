# exp-2026-08-hmm-vol-forecast-value

**Status:** running
**Started:** 2026-08-03 · **Stopped:** —

**Type:** OFFLINE code experiment (not a `CHALLENGER_SPEC` prompt variant).
Nothing here touches the live decision path.

## The question this actually asks

`exp-2026-08-hmm-regime-overlay` established two things: the HMM's one-day
volatility band is **calibrated** (Kupiec p=0.167, 249 obs) and its directional
call has **no skill** (58% against a 59% always-FLAT baseline). Calibration is
necessary but nowhere near sufficient — *a constant band equal to SPY's average
volatility would also be calibrated*, and it would forecast nothing.

So the question is not "is the HMM right on average" (answered) and not "does
volatility targeting work" (well established in the literature, and not a fact
about this desk). It is:

> **Does the HMM's volatility forecast beat the free alternatives — and is it
> worth 22 seconds of every cycle?**

That framing is deliberate. The HMM costs **~22s per fit**. GARCH(1,1), already
in this repo at `app/quant/garch.py`, costs **0.056s** — 400× less. A trailing
20-day realized standard deviation costs essentially nothing. If either matches
the HMM, the HMM is not earning its slot in a per-component budget that already
starves GARCH, HRP and the sizing bracket when it runs long.

## Feasibility checks run BEFORE registering (not results)

Confirming the design is testable at all, deliberately stopping short of any
performance comparison:

- The HMM band **varies**: 1.51% to 4.36% (2.9×, coefficient of variation
  0.307) across 250 stored posteriors; P(STRESSED) spans 0.001–0.989 with 20
  days above 0.25. A predictor that never moved would make this experiment
  vacuous, which is the `a-gate-that-emits-two-values-is-not-a-gate` failure.
- `garch_forecast` takes **raw log returns** (it scales internally); fed those
  it returns 16.52% annualized against a 16.42% sample realized — correct
  units, converged.

No accuracy or P&L comparison has been looked at.

## Hypothesis (one sentence, falsifiable)

The HMM's one-day-ahead volatility forecast is **more accurate than a trailing
20-day realized-volatility forecast**, measured by QLIKE loss against realized
squared returns, over the 249 stored point-in-time posteriors.

## Design

**Part 1 — forecast accuracy (PRIMARY, decisive).**

Three competitors, each producing a one-day-ahead volatility forecast for
session t+1 using only data through t:

| forecast | cost/day | source |
|---|---|---|
| HMM predictive band (gamma_T @ A mixture) | ~22 s | `regime_hmm_posteriors` |
| GARCH(1,1), refit point-in-time | 0.056 s | `app/quant/garch.py` |
| trailing 20-day realized σ | ~0 | `price_history` |

Truth proxy: the realized squared return on t+1. Loss functions:

- **QLIKE** (primary): `log(σ²) + r²/σ²`. Chosen over MSE because the squared
  return is an extremely noisy proxy for the true variance; QLIKE is one of the
  loss families that still ranks forecasts consistently under that noise
  (Patton 2011), and it penalises UNDER-forecasting risk harder, which is the
  asymmetry a trading desk actually cares about.
- **MSE** on variance (secondary), reported so a QLIKE-specific artefact is
  visible rather than hidden.

Test: **Diebold-Mariano** — a Newey-West t-test on the paired per-day loss
differential `d_t = L(HMM)_t − L(competitor)_t`, using the existing
`newey_west_tstat`, plus a stationary-bootstrap CI on the same series. Negative
mean ⇒ HMM better. Paired daily differentials are far less noisy than returns,
which is exactly why this is answerable at n≈249 while P&L is not.

**Part 2 — economic value (SECONDARY, reported only).**

Volatility-targeted sizing on each forecast:

```
exposure_t+1 = min(1.0, target_vol / forecast_vol_t+1)      target = 11% annualized
```

Target is the CALM state's own reported volatility — a number the model emits,
not one derived from this window's outcomes, so there is no look-ahead. Capped
at 1.0: no leverage. Costs 7.5 bps per side on every exposure change. Compared
against buy-and-hold and against each other on Sharpe, max drawdown, and how
tightly realized volatility tracks the target.

## Pre-registered decision rule (do not edit after start)

- **The HMM earns its cost** if QLIKE loss is lower than **trailing σ** with
  DM t ≤ −2.0 **and** the bootstrap CI on the differential excludes zero.
- **The HMM is redundant** if it does not beat trailing σ, or if GARCH beats
  it — GARCH is 400× cheaper and already wired into the same context block.
- **Inconclusive** if fewer than 200 usable paired days.
- **Part 2 cannot promote anything on its own.** At n=249 a Sharpe difference
  of the size volatility targeting produces is not resolvable; it is reported
  for direction and sanity, not for a decision. Reading a Sharpe improvement
  here as evidence would repeat the error the overlay experiment already made.
- Every forecast × use is recorded in `research_trials`, so the deflation
  accounts for this experiment too.

## What each outcome would mean

- **HMM wins:** keep it, and its 22s buys something GARCH cannot. Sizing on it
  becomes worth a separate registration.
- **GARCH wins or ties:** move the desk's volatility context to GARCH, keep the
  HMM only for its regime LABEL and duration (which GARCH does not produce),
  and stop paying 22s for a number a 0.056s model matches.
- **Trailing σ wins or ties:** neither model is earning anything on this axis,
  and the honest response is to say so in the desk block rather than keep
  rendering a forecast that a 20-day standard deviation matches.

## Result (fill on stop)

- Usable paired days: — · QLIKE DM (HMM vs trailing): — · (HMM vs GARCH): —
- Part 2 sizing: —
- Decision & rationale: —
