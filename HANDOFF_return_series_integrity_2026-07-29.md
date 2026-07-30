# The return series the desk reasons from was wrong in three places

**Shipped 2026-07-29** · `ecfe530` on master, deployed `master@e722da6` → synology
2026-07-30T04:27:44Z, container healthy, restarts=0.

**Trigger:** a review of a proposed "course-derived quantitative reasoning layer"
(MIT 18.S096, 25 lectures). Ranking the topics required measuring what the data
can actually support, and the measurement found live bugs in the numbers already
being injected into agent prompts. Those were worth more than any lecture on the
list, so they shipped first. The ranked topic review is in
`~/.claude/plans/please-look-at-this-quiet-nebula.md`.

---

## 1. `price_history` mixes vendor adjustment conventions

`source` is part of the primary key, so one ticker-date can carry several vendor
prints — and **the vendors disagree**:

```
dual-source ticker-dates ............ 9,225 across 38 tickers
mean absolute close difference ...... 20.05%
pairs over 50bps .................... 2,959 of 9,225  (32%)
worst .............. DRIP 718%, AGNC 6.69%, ASC 2.44%, CVX 1.71%, ALLY 1.11%
row counts ......... yfinance 15,144,255 · polygon 9,458 · world_simulator 58 (1 ticker)
```

It is an adjustment-convention gap (yfinance dividend/split-adjusted, polygon
raw), so it is systematic, not noise.

`load_close_returns` applied `LIMIT` **before** any de-duplication, so on a
dual-source ticker "253 bars" spanned ~127 distinct dates. **The bug had no
single sign**, which is why it survived review for so long:

| ticker | vendor gap | as-written | correct | mode |
|---|---|---:|---:|---|
| CRH | ~1% | 25.18% | 32.44% | **understated 23%** — same-date pairs inject near-zero returns and dilute variance |
| ALLY | ~1% | 23.92% | 29.85% | understated |
| CVX | 1.7% | 23.06% | 22.87% | marginal |
| DRIP | 718% | **2,660.95%**, 133 daily moves >15% | 232.39%, 1 | **overstated ~10x** — alternating conventions manufacture jumps |
| AAPL | single-source | 24.73% | 24.73% | unaffected |

`load_returns_matrix` was **not** safe either, contrary to first reading:
`pivot_table(aggfunc="last")` collapses duplicates by row order while the query
sorts only by `date`, so the vendor chosen per date was undefined — a single
column could switch convention between dates.

**Fix:** collapsing to one row per date is insufficient. Both loaders now pin
**one vendor per ticker for the whole window** — `_keep_dominant_source()` (pandas
path) and `_dominant_source_sql()` (single-ticker path, filter inside the
subquery so it precedes `LIMIT`). Preference is row count in window, ties broken
by source name for determinism.

⚠ **A correct fix here is a no-op almost everywhere.** 2,726 of 2,764 tickers are
single-source. "The numbers didn't change" is not evidence it worked — verify on
CRH, ALLY, ASML, DRIP, CVX.

Live consumers: `context_block.py:108` (**the GARCH vol forecast in every quant
prompt**), `technical_baseline.py:366,390`, `book_brief.py`, `quant_tools.py:379`.
The cycle running when this was found used HOOD, EXLS, **CRH**.

## 2. `book_brief` correlated by array position, not by date

`book_brief.py:110-121` loaded each series independently and correlated them with
`n = min(sizes)` then `[-n:]`. No shared date index, so any coverage difference
silently misaligns. Reproduced directly: unaligned NVDA~SPY OLS beta read
**−0.01**; date-joined it is **+2.06**.

Measured over 43 candidate×holding pairs against the live 9-position book —
these are the values that were rendering into the Board's prompt:

| candidate | worst holding, as-written | worst holding, date-joined | avg (aw) | avg (dj) |
|---|---|---|---:|---:|
| CRH | ALLY +0.566 | ALLY +0.444 | 0.095 | 0.254 |
| **ASML** | **ALLY +0.298** | **TSM +0.712** | 0.039 | 0.221 |
| **NVDA** | **AXP +0.101** | **TSM +0.659** | 0.019 | 0.123 |
| JPM | COF +0.590 | COF +0.590 | 0.206 | 0.284 |
| **XOM** | **TSM −0.211** | **ALLY −0.215** | −0.071 | −0.091 |

`mean |Δ| 0.152`, `max |Δ| 0.679`. Two things matter more than the spread:

1. **For 3 of 5 candidates it named the wrong holding.** ASML's largest overlap
   was reported as a *bank* at +0.30; it is its *foundry* at **+0.712** — over the
   ±0.70 "concentrates existing risk" threshold, so the Board was told
   "diversification available" on a name that concentrates.
2. **The bias is directional** — correlation was understated in *every* case. On a
   long-only 9-position book that structurally encourages adding correlated
   exposure. Pairs sharing a vendor and a calendar came out at `Δ = +0.000`, which
   is why it never looked broken.

**Fix:** route through `load_returns_matrix([ticker, *held], 250)` — the
date-indexed, coverage-filtered (60%), ffill-capped (5d) path already written 30
lines above in the same module. Also removes a redundant per-ticker query.

## 3. Reconciling a fact did not reconcile the inference

Closes the open item from `HANDOFF_fidelity_audit_2026-07-29.md` §"Still open" #5.
The technical, fundamental and valuation passes corrected the metric and preserved
the model's original, but the **conclusion built on the wrong number kept
travelling as founded** — a verdict of UNDERVALUED derived from a since-overwritten
P/E read to the Board exactly like one derived from the real figure.

**Fix:** `mark_conclusion_stale()` in `technical_baseline.py` (already the shared
module — the other two import `_finite` from it), called from all three
reconcilers, plus `render_stale_conclusion()` in `shared_desk.py` so it reaches
the Board. Follows `alt_data_block.py:269-288`'s `stance_is_stale` precedent.

Flagged conclusions: quant → `thesis_direction`; fundamental →
`thesis_direction`, `near_term_read`; valuation → `verdict`,
`fair_value_estimate`.

Two invariants held deliberately:
- **Judgment is never rewritten.** A module that corrects numbers has no opinion
  on what they mean; the flag only stops a stale call presenting as sound.
- **Keys are underscore-prefixed and written only by code.** A model-authored
  `assumptions`/`method` field would be guaranteed-populated and never-verified —
  the same failure as the 171-of-305 fabricated RSIs. Provenance must come from
  the verifier, not the claimant.

---

## Verification

- 1,955 unit tests pass. **2 failures reproduce identically on unmodified master**
  (`test_parameter_tools`, `test_prism_prompt_injection` — VLLM endpoint offline).
- 13 new tests in `tests/unit/test_returns_vendor_consistency.py` pin **both**
  failure directions (dilution and manufactured jumps) plus the no-op-on-
  single-source property and the stale-conclusion render.
- Post-fix values verified against the live DB: CRH 32.44%, ALLY 29.85%,
  DRIP 232.39%, AAPL 24.73% (unchanged); ASML now names TSM +0.71 and crosses
  into "concentrates existing risk".
- Loader latency **5–16 ms** post-warmup — the new subquery costs nothing. (A
  first-call 7.4 s reading was connection-pool warmup, not the query.)

## Also measured, and it refutes a plausible hypothesis

`decision_outcomes` is **almost entirely disconnected from the book**, so its
aggregates are hypothetical, not realized:

```
total scored (pnl_pct not null) .......... 2,117
of which DEGRADED_ARTIFACT ............... 358  at -5.75% avg
fills ever ............................... 45, across 28 cycles
degraded decisions -> fills .............. 0
ALL SELL rows (1,062) -> fills ........... 0     (the system cannot short)
upper bound, ticker-only join ............ <=116 of 2,117  (5.5%)
```

The 362 degraded rows at −5.75% are **not** book drag — they are 356 SELLs on a
long-only book, marked to market against a trade that could not have happened.
The hypothesis that their drag exceeds the 0.61pp pipeline-vs-null gap is
**wrong**. `scripts/agent_scorecard.py` already guards this with
`--executable-only` / `--include-degraded`.

The live consequence is a measurement one: **the evidence used to tune this system
is computed on decisions that overwhelmingly never traded**, including the
confidence-floor-70 justification at `parameter_store.py:84-85`. Still valid as a
*ranking* signal; not realized P&L. Related: confidence is monotone up to the
floor and flat above it (<60 → −5.29%, 60-69 → −1.09%, 70-79 → **+2.49%**,
80+ → **+2.60%**), so above 70 it carries no ordering information — any future
capability whose output is a *finer* conviction number is dead on arrival.

## Next (unstarted)

`tests/unit/test_returns_vendor_consistency.py` is the template. From the ranked
review, only three of 25 lecture topics are new, executable and cheap:

1. **VaR / ES / Cornish-Fisher** → new `app/quant/tail_risk.py`, wired into
   `context_block.py` (4.5 ms) and `sizing_bracket.py` as a 5th binding
   constraint. **Replace** `quant_report.risk_metrics.max_drawdown_est`
   (`artifacts.py:308`) rather than adding a field — prompt real estate is the
   scarce resource (`_EMBED_CHAR_BUDGET ≈ 4,944` chars, 10 `_KEEP` sections).
   Acceptance gate: Kupiec LR p > 0.05 on ≥20 of 30 held names (226 ms each;
   AAPL measured 3.90% exceptions vs 5% expected, LR 2.72, p=0.099).
2. **Kalman time-varying beta** → extend `factors.py:market_beta` (1.4 ms); the
   current beta is a static full-window OLS.
3. **`adf_tau()`** → `stat_gates.py`, as a *gate* that refuses claims regressed on
   price levels. **Not** as a forecaster: measured ACF of AAPL returns is
   [−0.004, +0.005, −0.034, −0.032, −0.015] ≈ 0 while ACF of squared returns is
   [+0.182, +0.117, +0.155, +0.228, +0.099]. There is no linear return
   autocorrelation to model — ARIMA on returns fits noise, and all the structure
   is in the second moment, where GARCH already is.

Hard-blocked, do not attempt: Black-Scholes/greeks/Ross recovery (**no options
table exists at all**), HJM (3 rate tenors is insufficient rank), cointegration
(none found in XOM~CVX/KO~PEP/AAPL~NVDA, *and* the short leg is forbidden).
