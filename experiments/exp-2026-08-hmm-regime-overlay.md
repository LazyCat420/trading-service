# exp-2026-08-hmm-regime-overlay

**Status:** running
**Started:** 2026-08-03 · **Stopped:** —

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

## Result (fill on stop)

- Usable observations: — · incremental mean: — · full_gate: — · DSR: —
- Threshold sweep: —
- Decision & rationale: —
