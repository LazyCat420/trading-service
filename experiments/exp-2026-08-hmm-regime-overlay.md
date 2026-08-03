# exp-2026-08-hmm-regime-overlay

**Status:** stopped-rejected
**Started:** 2026-08-03 · **Stopped:** 2026-08-03

**Type:** OFFLINE code experiment (not a `CHALLENGER_SPEC` prompt variant).

The champion/challenger lane in this directory drives prompt variants through
`custom_instructions`. A regime overlay is a code change, so it has no A/B lane
— and per `docs/EDGE_MEASUREMENT_2026-07-31.md` it must not get one yet: the
desk's honest MDE is **8.84pp over 329 effective decisions** with **46 fills in
all of history**, so an overlay worth 1-3pp is undetectable on live decisions
for roughly a year. This experiment is therefore settled entirely offline,
against price data, and **nothing here touches the live decision path**.
Registered before the harness was run, for the reason the README gives:
deciding what "better" means after seeing the result is how a lucky run becomes
a confirmed improvement.

## Hypothesis (one sentence, falsifiable)

Cutting market exposure to zero on days when the HMM's point-in-time posterior
puts ≥50% probability on the STRESSED state earns a **positive incremental
return net of costs** versus always-long buy-and-hold in SPY.

Why believe it: the fitted STRESSED state carries a mean of **−0.23%/day**
against CALM's **+0.11%/day**, and 35% annualized vol against 11%. If the
posterior identifies that state with any skill *before the fact*, sitting out
those days should add return and cut drawdown. If it does not, the state means
are an in-sample artifact — which is equally worth knowing.

## The change

Exposure rule, applied to the session AFTER the posterior's `as_of` date:

```
exposure(t+1) = 0.0  if P(STRESSED | data through t) >= 0.50
exposure(t+1) = 1.0  otherwise
```

Costs: 7.5 bps per side on every exposure CHANGE (the repo's standing
assumption, same as `factor_backtest.py` and `quant_edge_verifier`).

Run with:

```
python scripts/regime_overlay_backtest.py --grade
```

## What is measured

The **incremental** series `overlay_return − buy_and_hold_return`, never the
overlay's raw returns. Raw returns are dominated by market beta: in a rising
market an always-long strategy "passes" any mean-positive test, which would
measure the market, not the overlay. The difference series is the value the
conditioning actually adds.

## Pre-registered decision rule (do not edit after start)

- **Primary metric:** mean daily incremental return, net of costs.
- **Promote to a live shadow if:** `full_gate` verdict = PASS (Newey-West
  t ≥ 2.5, stationary-bootstrap CI excludes zero, IS/OOS retention ≥ 0.5)
  **AND** `deflated_sharpe_from_registry` verdict = PASS with the denominator
  taken from `research_trials`, **AND** max drawdown is not worse than
  buy-and-hold.
- **Reject if:** `full_gate` = FAIL, or DSR = FAIL, or the incremental mean is
  negative.
- **Inconclusive if:** fewer than 200 usable daily observations — report and
  stop, do not extend the window to reach significance.
- **Threshold sweep is SECONDARY, not a rescue.** 0.50 above is the primary and
  only promotable rule. Other thresholds (0.3/0.4/0.6/0.7) are reported for
  shape, and **each is recorded in `research_trials` as its own hypothesis** so
  the deflation accounts for them. Picking the best threshold after the fact
  and promoting it is precisely the failure DSR exists to catch.

## Power, stated up front

~250 daily observations (one year of backfilled point-in-time posteriors). At
that n a small daily edge is **not** reliably detectable, so the asymmetry from
`factor_backtest.py`'s survivorship contract applies here too: **a FAIL is a
trustworthy kill; a PASS is "not yet falsified", not "proven"**, and would earn
a longer backfill and a shadow, never a live weighting.

## Result (2026-08-03)

**REJECTED on the pre-registered rule.** 249 usable observations,
2025-08-05 .. 2026-07-31, from a point-in-time backfill (246 fits, 0 failures).

Primary rule, P(STRESSED) ≥ 0.50 — out of market 11/249 days, 16 switches:

| | ann. return | Sharpe | maxDD |
|---|---|---|---|
| buy & hold | **+20.68%** | **+1.61** | **−8.88%** |
| overlay | +13.88% | +1.11 | −9.78% |

- Incremental mean: **−0.0270%/day (−6.80%/yr)**
- `full_gate`: **FAIL** (IS/OOS fails; the bootstrap CI excludes zero *on the
  wrong side* — [−0.0536, −0.0055] is a confidently NEGATIVE increment)
- Deflated Sharpe: **0.0** vs a luck-implied 0.1161 over **17** recorded
  trials → FAIL
- Every rejection clause fired: gate FAIL, DSR FAIL, mean ≤ 0, drawdown worse.

Threshold sweep (secondary; each recorded in `research_trials`, which is why
the denominator climbed 14 → 17 as the sweep ran):

| threshold | days out | incremental /yr | verdict |
|---|---|---|---|
| 0.30 | 17 | −9.26% | FAIL |
| 0.40 | 14 | −8.16% | FAIL |
| **0.50 (primary)** | 11 | **−6.80%** | **FAIL** |
| 0.60 | 7 | −4.40% | FAIL |
| 0.70 | 5 | −3.61% | FAIL |

The sweep is monotone: the less the overlay acts, the less it loses. There is
no threshold at which it stops destroying value — it converges to zero harm
only by converging to doing nothing. That is the cleanest possible refutation,
and it is exactly why the sweep was pre-registered as non-promotable.

### What was actually falsified

The hypothesis rested on the fitted STRESSED state's **−0.23%/day** mean. That
mean is an **in-sample description of days already labelled stressed**, not a
forecast: by the time the posterior puts ≥50% on STRESSED, the drop has
largely happened, and the overlay sells after the fall and buys back after the
recovery. Costs are a rounding error next to that — 16 switches at 7.5bps is
1.2% against a 6.8%/yr shortfall.

Note the window is a strong bull market (buy & hold +20.7%, Sharpe 1.61, max
drawdown only −8.9%), which is close to the worst case for any de-risking
overlay. A single year containing no real crisis cannot prove the rule is
worthless in one that does. But the pre-registered rule was written to be
settled on this evidence, and on this evidence it loses.

### What survived — and it is the more useful half

The same backfill graded the HMM's **native** claim, which is volatility, not
direction (`scripts/grade_hmm_regime.py --grade --compare`):

- **Volatility coverage: CALIBRATED.** 8 breaches of the one-day 95%
  predictive band in 249 days = 3.21% observed vs 5% expected, Kupiec
  LR 1.907, **p = 0.167** → cannot reject correct coverage.
- Direction: 141/244 = 58%, against an always-FLAT baseline of **59%** — no
  directional skill, and 233 of 244 calls were FLAT anyway.
- Head-to-head: over the same 244 days the LLM regime engine produced
  **one** scoreable `forward_call` (0/1). The module's founding complaint —
  "7 of 130 desks" — is if anything understated.

So the model is honest about risk and useless about direction, and the overlay
failed because it was built on the half the model cannot do. A volatility
overlay (position SIZING against the calibrated band, rather than a binary
in/out on direction) is the version worth registering next — as a NEW
experiment, since editing this hypothesis after seeing the result is precisely
what this directory forbids.
