# Plan — net-of-cost returns and multiple-testing correction

**Date:** 2026-07-26 · **Status:** proposed
**Trigger:** "how can we improve our ground truth system to be PhD research level?"

---

## Context

Every performance number this system has produced is **gross of all trading
costs**, and the audit that established this took one query:

```
fees_nonzero | 0 | of | 44          -- every fill, zero fees
fill_price   = current_price        -- paper_trader.py:620, exactly
```

There is no spread, no slippage, no commission, and no market impact anywhere in
the execution path. Fills happen at the reference close price.

This matters more than it sounds. [Toward Reliable Evaluation of LLM-Based
Financial Multi-Agent Systems](https://arxiv.org/html/2603.27539v1) (2026)
re-evaluated FinMem's published **+23% return and obtained −22%** once
transaction costs were applied — a sign reversal from costs alone. That paper
defines five minimum standards and finds *no surveyed system meets all five*.

Audited against those five standards, this repo scores:

| Standard | Status | Evidence |
|---|---|---|
| 1. Contamination control | ❌ | Eval windows overlap model training; agents see real tickers/dates |
| 2. Point-in-time universe | ❌ | `fundamentals` = current snapshot, look-ahead until ~2028 |
| 3. Rolling-window reporting | ⚠️ | `is_oos_degradation` exists; no multi-window variance |
| 4. **Net-of-cost returns** | ❌ | **This plan** |
| 5. Regime coverage | ⚠️ | HMM shadow exists; no regime-stratified reporting |

The live measurement already reads **PIPELINE +1.53% vs BASELINE +2.14%** — the
pipeline trails the always-long null *before* any costs. Costs can only widen
that gap. The current instrumentation cannot show by how much.

---

## What this plan does NOT do

Stated up front, because the temptation is to claim more than is delivered:

- **It does not make the strategy profitable.** It measures honestly. The
  expected outcome is that the pipeline looks *worse* than it does today.
- **It does not fix contamination or point-in-time data.** Those are standards 1
  and 2, and both are larger jobs (see Phase 4 and the open items).
- **It does not produce a "true" cost number.** A paper book has no real fills.
  This models costs from observable spread proxies; it is a defensible estimate,
  not ground truth. Ground truth requires a real broker.

---

## Phase 1 — a cost model (the sign-reversal risk)

**New module `app/quant/execution_costs.py`.** Pure functions, no DB, unit
testable — the same shape as `app/quant/stat_gates.py`.

Three components, each separately defensible and separately overridable:

1. **Spread cost** — half the bid-ask spread, paid on entry and exit. Estimated
   per ticker from the Corwin-Schultz high-low estimator over `price_history`
   (no quote data is stored, so a high-low proxy is the honest option), with a
   floor for large caps and a ceiling to stop illiquid names producing absurd
   figures.
2. **Commission** — per-share or per-trade, configurable, default 0 for the
   paper book but present so a real venue can be modeled.
3. **Market impact** — square-root law, `impact = k * σ * sqrt(order_value /
   ADV)`. The standard practitioner form. Requires ADV, which is derivable from
   `price_history.volume`.

**Applied at two points, deliberately separated:**

- `paper_trader.buy/sell` — the fill price becomes
  `reference_price * (1 ± cost_bps/10_000)` and `fees` is populated, so
  **future** fills carry real friction and `trade_fills` stops being fiction.
- `scripts/residual_alpha_report.py` and `agent_scorecard.py` — a
  `--cost-bps` / `--no-costs` flag applies costs to **historical** decisions, so
  the existing measurements can be restated without waiting for new data.

**Why both:** changing only the trader leaves every existing number gross and
uncomparable. Changing only the reports leaves the book still trading
frictionlessly. The pair is what makes the measurement consistent.

⚠ **This changes live trading behavior.** Positions will be slightly smaller and
exits slightly worse. That is the correction, but it must be called out.

---

## Phase 2 — multiple-testing correction

**Extend `app/quant/stat_gates.py`** (do not create a new module — NW t-stat and
the stationary bootstrap already live there and share the callers).

- **`probabilistic_sharpe_ratio(returns, benchmark_sr=0.0)`** — Bailey & López de
  Prado. Corrects a Sharpe for skew, kurtosis and sample length:
  `PSR = Φ((SR - SR*)·√(n-1) / √(1 - γ₃·SR + (γ₄-1)/4·SR²))`
- **`deflated_sharpe_ratio(returns, n_trials, trial_sharpes=None)`** — the same,
  with the benchmark raised to the level the *best of N trials* would reach by
  chance. This is the one that matters here: this repo has tested momentum,
  low-vol, beta, reversal, HMM regimes, several sizing rules and many agent
  configurations. Each trial inflates the best observed Sharpe.
- **`min_track_record_length(returns, benchmark_sr)`** — how many observations
  are needed before a claimed Sharpe is distinguishable from the benchmark.
  Expected to return numbers far above current n, which is itself the finding.

**Wire into `scripts/factor_backtest.py`**, which currently prints a raw Sharpe
with no trial correction — the exact shape the DSR exists to catch.

---

## Phase 3 — measure realized slippage rather than assume it

Once Phase 1 is live, `trade_fills.fill_price` diverges from the decision
reference price. Record both, and the **implementation shortfall** (Perold) is
then directly measurable rather than modeled:

```
IS = (fill_price - decision_price) / decision_price   [signed by side]
```

Add `decision_price` to `trade_fills` and a `scripts/execution_quality.py`
reporting realized IS by ticker, side and order size. This is the feedback loop
that lets the Phase 1 cost *model* be checked against what the book actually
experiences — turning an assumption into a measurement.

---

## Phase 4 — blinded evaluation (highest research value, largest effort)

From [KTD-Fin](https://arxiv.org/html/2605.28359): anonymize tickers and dates
so an agent cannot lean on memorized narratives. Under blinding, their agents'
rationales shifted from *"defensive blue-chip"* to actual factor ranks, and
**nine of ten models showed negative selection alpha**.

This directly tests whether these agents reason or recall — the question the
whole harness exists to answer. Scoped here, not built:

- Alias map per evaluation episode (`AAPL → asset_0042`), applied consistently
  across prompt, tool arguments **and tool returns** (KTD-Fin's warning: an
  agent must never see a real identifier "even transiently through a tool
  result").
- A de-anonymization probe to certify the mask actually holds.

**Deferred** because it needs a harness change across every tool, and Phases 1-3
must land first to give it something honest to measure.

---

## Verification

- Unit tests for every cost component with hand-computed expected values.
- A **negative control**: with all costs set to zero, the new path must reproduce
  today's numbers exactly. If it does not, the harness is wrong, not the finding.
- Restate the headline residual-alpha measurement gross and net, and report both.
- DSR sanity check: a Sharpe from 100 random trials must deflate to
  insignificance. If it does not, the implementation is wrong.
- Full suite green (baseline: 1276 passed, 2 known pre-existing failures).

---

## Expected outcome, stated before running it

The pipeline currently trails the always-long baseline by **-0.62%** gross. With
costs applied it will trail by more. Combined with this repo's existing findings
— price factors dead over 30 years, no residual alpha at n=106 (t=-0.904) — the
likely conclusion is that **the system has no measurable edge, and costs make
that clearer**.

That is a result worth having. Writing the prediction down first is what stops it
being rationalized afterward.
