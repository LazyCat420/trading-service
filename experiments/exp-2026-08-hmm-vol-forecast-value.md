# exp-2026-08-hmm-vol-forecast-value

**Status:** stopped-rejected
**Started:** 2026-08-03 · **Stopped:** 2026-08-03

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

## Result (2026-08-03)

**The HMM is REDUNDANT on this axis.** 249 paired days, 2025-08-05 .. 2026-07-31.
Realized volatility over the window: **12.83% annualized**.

### The forecasts

| forecast | mean | min | max | cost/day |
|---|---|---|---|---|
| HMM | **14.74%** | 12.20% | 35.32% | ~22 s |
| GARCH(1,1) | 13.37% | 8.81% | 22.89% | 0.056 s |
| trailing 20-day σ | **12.36%** | 5.78% | 20.39% | ~0 |

The HMM runs **~1.9 points hot** against a 12.83% realized. Trailing σ is the
closest to the truth on average and it is the one that costs nothing.

### Primary — QLIKE, Diebold-Mariano

| comparison | t | 95% CI | verdict |
|---|---|---|---|
| HMM vs trailing | −0.89 | [−0.492, 0.092] | no significant difference |
| HMM vs GARCH | +0.67 | [−0.057, 0.100] | no significant difference |
| GARCH vs trailing | −1.37 | [−0.454, 0.009] | no significant difference |

Mean QLIKE: GARCH 0.590 < HMM 0.618 < trailing 0.764. Nominal ordering favours
the models, but **nothing clears the pre-registered bar** (t ≤ −2.0 with the CI
excluding zero). The HMM does not beat a 20-day standard deviation.

### Secondary — MSE, and it is worse than a tie

| comparison | t | verdict |
|---|---|---|
| HMM vs trailing | **+2.73** | **TRAILING better** |
| HMM vs GARCH | **+3.04** | **GARCH better** |
| GARCH vs trailing | −1.55 | no significant difference |

Mean MSE: GARCH 1.323 < trailing 1.376 < HMM 1.877. On a symmetric loss the
HMM is **significantly the worst of the three**.

### Why the two losses disagree, and why that is not a contradiction

QLIKE penalises under-forecasting risk harder than over-forecasting; MSE is
symmetric and quadratic. The HMM's error is almost entirely a **systematic
upward bias** with occasional 35% spikes — the exact error profile QLIKE
forgives and MSE punishes. Both losses are describing the same defect.

This also closes the loop on the earlier Kupiec result. `exp-2026-08-hmm-
regime-overlay` found 3.21% band breaches against 5% expected — slightly *too
few*, i.e. a band slightly **too wide**. That was not significant on its own
(p=0.167) and read as "calibrated". Three independent measurements now agree:
the HMM's band is honest but **too wide**, and being too wide is precisely what
makes it no more useful than a free estimator.

### Part 2 — vol-targeted sizing (secondary, non-promotable)

| strategy | return | Sharpe | vol | maxDD | mean exposure |
|---|---|---|---|---|---|
| buy & hold | 19.85% | **1.55** | 12.83% | −9.03% | 1.00 |
| trailing-sized | 13.92% | 1.25 | 11.12% | **−7.96%** | 0.87 |
| GARCH-sized | 10.47% | 0.99 | 10.53% | −8.45% | 0.84 |
| HMM-sized | 8.49% | 0.85 | 10.02% | −8.80% | 0.79 |

Every sized variant loses Sharpe to buy-and-hold, and the ordering matches the
forecast quality exactly — the best forecast produces the best sized portfolio.
Two honest caveats: exposure is **capped at 1.0**, so in a calm bull market the
rule can only ever remove return and never add it back, and a leveraged version
is a different (untested) strategy; and the registration already committed that
a Sharpe difference is not resolvable at n=249. **Nothing here promotes.** What
it does show is that the HMM is the *worst* input to it, which is consistent
with everything above.

## Decision

Per the pre-registered consequence for "trailing σ wins or ties": **stop
presenting the HMM's volatility number as a forecast that has earned anything.**

What the HMM still uniquely provides, and what the race does not touch:

- a regime **label** with a posterior probability,
- an **expected duration** (~32 calm days vs ~5 stressed),
- the **transition dynamics** between states.

GARCH and trailing σ produce none of those. So this is not a case for deleting
the model — it is a case for being precise about which of its outputs has been
validated. The desk line now carries the measured limits so an agent cannot
read the vol figure as a superior estimate.

**Open question for a human, not settled here:** the HMM costs ~22s inside a
per-component budget that `context_block.py` documents as already starving
GARCH, HRP and the sizing bracket when the HMM runs long. Its vol number is
matched by a 0.056s model. Whether the regime label alone justifies 22s per
cycle is a cost decision, and it is now an informed one.

**Follow-up worth registering separately (not done here):** GARCH had the best
mean loss on BOTH losses and is 400× cheaper. It did not clear the bar against
trailing σ either (t=−1.37), so the honest current state is that *no* model on
this desk has demonstrated a volatility edge over a 20-day standard deviation.
A longer window would give that comparison real power.
