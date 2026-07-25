# HANDOFF — Quant factor wave: price factors, statistical gates, residual alpha, HMM regime shadow (2026-07-25)

Shipped `6a527d1` + `scripts/agent_scorecard.py` default fix. Previous wave's
handoff archived to [`docs/HANDOFF_agent_audit_2026-07-24.md`](docs/HANDOFF_agent_audit_2026-07-24.md).

---

## What this wave was

A plan arrived proposing a graph-engineering rewrite (11-node DAG runtime,
YAML graph specs), seven Fama-French factor nodes, an HMM regime engine, and
an RL allocation overlay. **Four ideas survived verification against the live
DB; three were cut.** The cuts matter as much as the builds:

| Proposed | Verdict | Why |
|---|---|---|
| Graph runtime (nodes/edges/YAML) | **CUT** | `orchestrator.py` already is one — scoped per-node failure (`_run_agent_with_circuit_breaker`), fan-in state (`SharedDesk`), phase gating (`DeskPhase`), budgets, telemetry, run artifacts. Rebuilding it generically costs weeks and ships zero alpha. |
| Value / profitability / investment / size factors | **CUT** | `fundamentals` = 4,782 rows, 737 tickers, **76 distinct snapshot dates, earliest 2026-05-06**. A current-snapshot table, not a panel. Backfilling today's book values across history is look-ahead bias. Earliest honest start ~2028. |
| RL regime overlay | **CUT** | 3 states × ~2-3 regime transitions in any available test window. A learned policy is indistinguishable from a lucky one at that n — and the *existing* decision layer already trails buy-and-hold. |
| Momentum / low-vol / beta / reversal factors | **BUILT** | Price-derived. `price_history` = 15.1M rows, 2,744 tickers, back to 1962, 2,072 with ≥10y. |
| Newey-West / bootstrap / IS-OOS gates | **BUILT** | Every return series here is built from *overlapping* windows. |
| Residual-alpha gate | **BUILT** | Directly answers the open question: alpha, or beta? |
| HMM regimes | **BUILT — as a shadow** | Not a replacement. See below. |

---

## THE HEADLINE MEASUREMENT

```
scripts/residual_alpha_report.py --since 2026-05-01 --executable-only
153 consequential desks, +7-session horizon

BASELINE  always-long over the same desks : +2.14%
PIPELINE  return signed to actions taken  : +1.53%
          difference vs the null          : -0.62%

Residual alpha: -0.53% per decision (t=-0.904, gate 2.5) — NOT distinguishable from zero.
  raw mean +1.12% = +1.65% explained by factor exposure + -0.53% residual.
  n=106, R²=0.4001, NW lag=6
    momentum   loading +1.976 (t=3.194)
    low_vol    loading +0.665 (t=1.198)
    beta       loading -2.884 (t=-2.486)
    reversal   loading +0.425 (t=0.681)
```

**The pipeline's return is explained by its factor exposure.** This is the
first time that is sayable with a t-stat rather than an anecdote. Note the
*negative* beta loading alongside a positive raw return — the book leans
toward low-beta names and still trails always-long, so this is not the simple
"it just bought high-beta in a rising tape" story; the momentum loading
(+1.98, t=3.19) is doing most of the explaining.

This is a **null result, not a broken script** — the report says so in its own
output so the next reader doesn't file it as a bug.

**Caveats, stated up front:** n=106 after factor-exposure joins. The window is
91-up/48-down, a rising tape that flatters always-long. R²=0.40 means 60% of
variance is still unexplained. This is one measurement, not a verdict on the
pipeline.

---

## The four modules

### `app/quant/factors.py`
Momentum 12-1, low-vol, beta, short-term reversal → **cross-sectional
z-scores** (a factor value for one ticker in isolation is meaningless).

- The **12-1 skip month is load-bearing**: the recent month carries short-term
  reversal, the *opposite* sign to momentum. 12-0 reliably destroys the effect.
- `low_vol` and `reversal` are **sign-flipped** so a high score always means
  the desirable end. Returning raw vol would invert every downstream ranking.
- **SPY is an input to beta, never a member of the ranked cross-section.**
- A cross-section under `MIN_CROSS_SECTION` (5) yields **no factor at all**,
  never zero-fill. A zero-filled factor reads as "perfectly average" and is a
  fabrication.

### `app/quant/stat_gates.py`
Newey-West t-stat, stationary bootstrap CI, chronological IS/OOS degradation.

- Every return series here is built from **overlapping** forward windows, so
  consecutive observations are mechanically correlated and the naive t-stat
  overstates significance by ~√overlap. NW lag is floored at the horizon.
- Bootstrap is **Politis-Romano stationary** (geometric blocks, wrapping). A
  fixed-block bootstrap severs dependence at every boundary and silently
  narrows the interval on exactly the autocorrelated data we care about.
- **`INSUFFICIENT_DATA` is a distinct verdict from `FAIL`.** "Couldn't check"
  must never read as "checked and fine" — that is the laundering failure mode
  in one enum.
- Guards a real trap: two negative Sharpes divide to a *positive* retention
  ratio, so `is_oos_degradation` returns `retention=None` when IS Sharpe ≤ 0.

### `app/quant/residual_alpha.py` + `scripts/residual_alpha_report.py`
OLS with Newey-West standard errors. Exposures computed **as of the decision
date** from that date's cross-section (using today's exposures for a two-
month-old decision would be look-ahead). Returns are **signed to the action
taken** — a SELL before a fall is a positive return.

**Reports only. It never gates a trade** — a residual-alpha estimate over a
few hundred decisions in one rising tape is a diagnostic, not a risk control.

### `app/quant/regime_hmm.py`
2/3-state Gaussian HMM on SPY daily returns, BIC-selected.

**Why a shadow and not a replacement:** `v3_regime_engine` is the
best-calibrated agent in the pipeline (+7.65 edge, 85.7% hit, Brier 0.146) —
but on **n=7 of 130 desks**, because 94% of its output carries no falsifiable
claim. That 85.7% cannot be distinguished from seven lucky draws. The HMM
emits a posterior **every day, unconditionally, from prices alone**, which
makes it the measurable baseline the agent must beat. It is injected as
context explicitly framed as "NOT a directive", never overrides the Regime
Engine, and never gates a trade.

- **Baum-Welch runs in LOG SPACE.** Naive forward-backward underflows to nan
  within ~200 daily observations; log-space + logsumexp is the only version
  that survives a 2-year window. There is a regression test on 2,000 obs.
- **Deterministic quantile init.** EM is only locally optimal — a random start
  would tell a different regime story every cycle.
- **States ordered by volatility ascending** → CALM / TRANSITIONAL / STRESSED
  mean the same thing every run.

Live output on 2026-07-25 (n=547, 3-state by BIC): TRANSITIONAL @ 50%
posterior; STRESSED state carries 77% annualized vol and -0.81% mean daily
return — economically sensible.

---

## Traps found (will bite again)

- **`np.float64` survives `round()`** and then breaks `json.dumps` when the
  desk artifact serializes. Cast `float()` *inside* the round, not around it.
  Caught only by serializing the real output, not by the unit tests.
- **`agent_scorecard.py --source outcomes` caps at n=40 and `--since` cannot
  widen it.** The limiter is neither the date nor the resolver: **2,023
  outcomes are resolved but only 65 JOIN to a `shared_desk` row**, so every
  `--since` from 05-01 to 07-01 returns the identical 65. That sample also
  reports a *negative* always-long baseline where the 856-desk price sample
  reports +2.16%. **Fixed: `--source price` is now the DEFAULT** and the
  outcomes path prints the resolved-vs-joinable gap. **The desk-join gap
  itself is still an open bug.**
- **HOLDs-never-resolve is NOT a bug** — I called it one and was wrong. All 78
  unresolved HOLD rows were created 2026-07-20..07-25, inside the 7-day
  window; **zero** are older. HOLD tracking was switched on recently and the
  resolver handles it correctly. *Check row ages before calling a `0 resolved`
  count a defect.*
- **`pipeline_state` can be read stale.** A query right after triggering a
  cycle returns the *previous* run's `done`, which reads as "the cycle
  finished instantly". Key progress queries on the actual `cycle_id`.
- `shared_desk`'s column is **`phase`**, not `desk_phase`. `positions` uses
  **`qty`**, not `quantity`.
- Container still has **no sklearn / hmmlearn / arch** — all of this is
  hand-rolled numpy/scipy.
- New tests take ~3 min: the 10k-resample bootstrap runs a Python loop per
  resample. Acceptable for a gate that runs offline; do not put it on the
  cycle's hot path.

---

## Verification

- **24/24** new unit tests pass (`tests/unit/test_quant_factor_wave.py`).
- **22/22** related existing tests still pass (sizing bracket, context-block
  fail-open, buy sizing) — no regression from the `context_block.py` edit.
- Factors + HMM verified on **real DB data**, not just fixtures: KO/JPM rank
  highest on low-vol, AMD highest on beta and momentum — economically sensible.
- `build_quant_math_block('UNH', 'test_bot')` renders both new lines in the
  **deployed image**, alongside the existing GARCH/HRP/sizing-bracket content.
- Deployed to NAS (145s), container healthy, all four modules import.

---

## Open / next

1. **The override matrix is the cheapest win available.** Board overriding
   fundamental = **-2.38 edge, 18% hit (n=34)**; overriding quant = -1.56;
   overriding debate = -0.52 at 25% hit. *Every* override is negative except
   the synthesizer's (+5.65, 88%, n=12). That is a tunable gate needing no new
   machinery — but n is small, so instrument before acting.
2. **Make `forward_call` mandatory on the regime engine** so the pipeline's
   best agent becomes gradeable on all 130 desks instead of 7. Then compare it
   head-to-head against the HMM shadow over the same days.
3. **Fix the `decision_outcomes` → `shared_desk` join gap** (2,023 resolved,
   65 joinable).
4. **Distribution-collapse canary** — flag any agent field whose distinct-count
   collapses or whose top value exceeds 70%. Would have caught the fabricated
   RSIs, the 3/4/5% sizing habit, and the missing-`final_decision` bug, all
   found by hand months apart.
5. Keep collecting `fundamentals` snapshots — the fundamental factors unlock
   for free once the panel is deep enough.
