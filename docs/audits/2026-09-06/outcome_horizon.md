# Is the "7-day outcome" measuring a 7-day horizon?

**Read-only audit — 2026-09-06.** Repo: `/home/lazycat/github/projects/sun/trading-service` @ `aa5a6832`.
Store: `trading_bot` (Mongo), `decision_outcomes` n=2765 (2694 resolved, 71 pending).

## Verdict

**DRIFTING — and the fix is committed but has never run.**

The open item was correct, and it was correct as an understatement. Three separate things
are wrong, and only one of them has been fixed:

| | status |
|---|---|
| **Exit price** was `latest_close` — "whatever the price was when the sweep reached the row" | **FIXED IN CODE** at commit `1bfd700f` (2026-09-05 15:07 PT), **zero rows produced** |
| **The 2,694 historical rows** are still graded on the drifted basis, with nothing on the row to say so | **OPEN** — no backfill, no quarantine, no score-version bump |
| **`app/v3/challenger.py:139`** — the peer resolver that drives champion/challenger promotion — still uses latest-close, and with **no vendor pin at all** | **OPEN, unnoticed** |
| **Entry price** is `latest_close` at record time, with **no `entry_date` stored** — the left edge of the return is unspecified in code and unrecoverable from the row | **OPEN** |
| `resolve_outcome_for_exit` resolves at *any* age on a stop-out and stamps **no** `exit_date`/`horizon_days` | **OPEN** — 37 rows pooled into the same cohort |

---

## 1. The resolver — the exact price lookup

**`app/autoresearch/outcome_tracker.py`**

Recording (`record_cycle_decisions`, line 273):

```
335:        from app.quant.returns import latest_close
338:            (ticker, confidence, latest_close(ticker), result_json)
385:                'created_at': now_utc,
```

`latest_close` (`app/quant/returns.py:261`) is `price_history` sorted `date DESC limit 1`, vendor-pinned
by `dominant_source_for` (freshest-then-deepest). **It is the newest bar that exists at the moment
the cycle runs** — not the close on the decision date. No `entry_date` is written.

Resolution (`resolve_pending_outcomes`, line 405) — **as of `1bfd700f`**:

```
459:                horizon = created_at + timedelta(days=RESOLVE_AFTER_DAYS)
460:                exit_price, exit_date = close_on_or_after(ticker, horizon)
500:  ... {'$set': {..., 'exit_date': exit_date, 'horizon_days': RESOLVE_AFTER_DAYS}}
```

`close_on_or_after` (`app/quant/returns.py:311`) is `price_history` `date >= horizon`,
`date <= horizon + 5d`, sorted ASC limit 1, same vendor pin. **This is entry+7d, correctly.**

**Before `1bfd700f` it was `latest_close(ticker)` — the latest close, full stop.** Every one of
the 2,694 resolved rows on the store was priced that way.

Third path, `resolve_outcome_for_exit` (line 527), called from `app/trading/paper_trader.py:1146`
and `:1243` on a stop-loss / take-profit exit:

```
550:  ... {'$set': {'exit_price': ..., 'pnl_pct': ..., 'outcome': ..., 'resolved_at': ...}}
```

No horizon at all; no `exit_date`, no `horizon_days`. It grades a realized trade exit and files
it in the same collection as the 7-day paper claims. 37 rows (31 BUY, 6 SELL) resolved this way.

**Fourth path, unfixed — `app/v3/challenger.py:121 resolve_challenger_outcomes`:**

```
139:            price_row = mongo_query.find_row(
                     'price_history', {'ticker': ticker}, ['close'], sort=[('date', -1)])
147:            exit_price = float(price_row[0])
```

This is the *original* defect verbatim, plus a second one: no `source` filter, on a collection whose
key is `(ticker, date, source)` and where **188 tickers carry more than one vendor**. It writes
`challenger_pnl_pct` / `challenger_outcome`, which feed `sequential.paired_disagreement_test`
(the e-value promotion gate) and `/api/v1/challenger/stats`. 498 rows resolved. Its sweep happens
to be timely (median lag 7.1d, max 8.7d), so the horizon damage is small — but the price is still
"newest bar at sweep time", not the bar at entry+7d, and the vendor is whatever Mongo emits first.

## 2. The stored contract — actual key set

Scanned all 2,765 documents. There is **no** `exit_date`, **no** `horizon_days`, **no** `entry_date`.

```
_id 2765   id 2765   cycle_id 2765   ticker 2765   action 2765   confidence 2765
entry_price 2765   created_at 2765   skill_versions 2765   overridden_from 2765
models_used 2765   exit_price 2694   pnl_pct 2694   outcome 2694   resolved_at 2694
lesson_stored 2637
exit_date      0
horizon_days   0
```

**A row cannot distinguish an on-contract resolution from a late one.** The only timestamps are
`created_at` (the decision) and `resolved_at` (when the sweep ran). `resolved_at` is not the price
date — under the old code the price was the newest bar at sweep time, which lags `resolved_at` by
0-2 sessions; under the exit path it is a live fill price at an arbitrary age.

The fix writes both new fields, but **`exit_date` is present on 0 of 2,765 rows**: the last
resolution was `2026-09-04 14:43`, before the fix committed, and every currently pending row is
younger than 7 days (0 eligible). The first live row lands ~2026-09-07. **The fix is committed at
HEAD and unproven in production.**

## 3. Measured drift

Horizon derived from stored dates, never from the field name. `exit_date` does not exist, so the
best available proxy is **`resolved_at − created_at`** — labelled a **PROXY**, and a *lower bound*
on the error: it dates the sweep, not the bar, and the bar is 0-2 sessions older still.

All 2,694 resolved rows:

| bucket | n | share |
|---|---|---|
| < 7 days | 37 | 1.4% |
| 7.0 – 7.9 days | 699 | 25.9% |
| 8 – 30 days | 26 | 1.0% |
| **> 30 days** | **1932** | **71.7%** |

- min 0.01 d · p25 **7.50** · **median 42.98** · p75 46.62 · **p90 52.41** · p99 64.81 · max 68.57
- **share within 7 calendar days: 0.0137**; within 8 days (7 + one grace day): 0.273
- **share beyond 30 days: 0.717**

This reproduces the number in the fix commit exactly (median 43.0, 71.7% >30d, 25.9% at 7.0-7.9d).
Mechanism: `resolve_pending_outcomes` takes `limit=50` per autoresearch cycle, so a backlog built up
and the eligibility cutoff never bounded how late the price could be.

**The backlog has since been worked off.** The cohort `decision_audit` actually scores today
(`resolved_at` within 30d, limit 100) has a proxy median of **7.2 days**, p90 7.9, 0% beyond 30 days.
So the *wall-clock* drift is currently small — but see §4: the grades on that same cohort still move
by 13-19 points, because "latest close at a 7.2-day sweep" is still not "the close at entry+7d".

## 4. Honest recompute

Method: for each resolved row, take the stored `entry_price` and `created_at`, and recompute the exit
as `close_on_or_after(ticker, created_at + 7d)` — the repo's own new accessor, same vendor pin, same
5-day grace — then re-grade through the repo's own `outcome_tracker._classify`. Entry is held fixed so
the horizon defect is isolated from the entry defect. Deviations are reported against the row's own
stored `pnl_pct`.

### 4a. 20-decision sample (seed 7, chronological)

19 of 20 recomputable (CRSR had no bar in the grace window). **3 grades change, 1 sign flip.**
abs deviation: median 4.31 pp, p90 27.97 pp, max 33.87 pp.

| id | tkr | act | entry | stored exit | honest exit | stored % | honest % | stored grade | honest grade | lag |
|---|---|---|---|---|---|---|---|---|---|---|
| a2f89845 | FDS | BUY | 223.68 | 247.11 | 270.85 | +10.47 | +21.09 | WIN | WIN | 53 d |
| 1b86a576 | WULF | SELL | 22.82 | 24.00 | 25.55 | −5.17 | −11.94 | LOSS | LOSS | 52 d |
| f6534132 | TX | BUY | 46.99 | 48.01 | 49.08 | +2.17 | +4.45 | WIN | WIN | 52 d |
| f55ad079 | MRVL | SELL | 196.33 | 235.81 | 290.72 | −20.11 | −48.08 | LOSS | LOSS | 50 d |
| **c1872dd5** | **SAIA** | BUY | 456.23 | 459.27 | 471.15 | +0.67 | +3.27 | **FLAT** | **WIN** | 50 d |
| 357fa47c | TDIC | BUY | 0.44 | 0.22 | 0.37 | −48.74 | −14.87 | LOSS | LOSS | 50 d |
| 7e12444d | WWII | SELL | 0.00 | 0.00 | 0.00 | 0.00 | 0.00 | FLAT | FLAT | 49 d |
| f8f749ed | IBKR | BUY | 80.95 | 94.09 | 87.06 | +16.23 | +7.55 | WIN | WIN | 48 d |
| 1651cc90 | AGI | BUY | 40.80 | 35.52 | 35.83 | −12.94 | −12.19 | LOSS | LOSS | 46 d |
| 0b6053a3 | SANM | SELL | 264.88 | 256.02 | 231.32 | +3.34 | +12.67 | WIN | WIN | 44 d |
| b6f6d6f0 | ARQQ | SELL | 16.27 | 12.14 | 13.69 | +25.38 | +15.86 | WIN | WIN | 43 d |
| ece36f77 | ORCL | SELL | 213.68 | 124.21 | 191.97 | +41.87 | +10.16 | WIN | WIN | 41 d |
| **3f0e4146** | **SANM** | SELL | 256.02 | 256.02 | 249.05 | 0.00 | +2.72 | **FLAT** | **WIN** | 39 d |
| **do-11b51f67** | **TSM** | BUY | 421.58 | 402.30 | 421.21 | −4.57 | **−0.09** | **LOSS** | **FLAT** | 7 d |
| do-3d4e1e0a | AXP | BUY | 351.93 | 335.28 | 336.63 | −4.73 | −4.35 | LOSS | LOSS | 7 d |
| do-f4e96d91 | INTC | SELL | 102.59 | 86.30 | 81.88 | +15.88 | +20.19 | WIN | WIN | 7 d |
| do-d3638ce9 | LMT | BUY | 514.36 | 574.11 | 582.74 | +11.62 | +13.29 | WIN | WIN | 7 d |
| do-5d0de634 | CRSR | HOLD | 12.10 | 12.10 | — | 0.00 | — | HOLD_CORRECT | *unresolvable* | 7 d |
| do-cc208b47 | META | HOLD | 599.12 | 546.03 | 545.83 | −8.86 | −8.89 | HOLD_AVOIDED_DECLINE | same | 8 d |
| do-d50ed761 | STX | HOLD | 984.65 | 795.10 | 821.67 | −19.25 | −16.55 | HOLD_AVOIDED_DECLINE | same | 7 d |

ORCL is the cleanest illustration: a single-vendor ticker (yfinance only, so no vendor confound),
graded WIN at +41.87% off a price 41 days late. At the actual horizon the move was +10.16% — still a
WIN, but the desk was credited with four times the edge it earned. TSM shows the reverse at a
*7-day* lag: −4.57% → −0.09%, LOSS → FLAT, purely because "latest close at the sweep" was two bars
short of entry+7d.

### 4b. Whole cohort

2,336 resolved non-degraded rows, 436 tickers. 2,253 recomputable; **83 (3.6%) have no bar in the
5-day grace window** and under the new code would stay pending indefinitely.

| | stored | honest (entry+7d) |
|---|---|---|
| directional win rate (ex-flat, ex-hold) | **0.609** (908 W / 582 L) | **0.576** (894 W / 658 L) |
| hold accuracy (direction-aware) | **0.549** (n=490) | **0.518** (n=490) |
| **grade changes** | — | **863 / 2253 = 38.3%** |
| **pnl sign flips** | — | **624 / 2253 = 27.7%** |
| abs pnl deviation | — | median **3.76 pp**, p90 14.86, p99 52.19 |
| median pnl | +1.050% | +1.028% |
| 1%-trimmed mean pnl | **+2.342%** | **+1.403%** |

The **raw** mean moves +1.79% → −6.67%, but that number is not defensible and I am not reporting it
as the headline: it is dominated by reverse-split basis breaks (TDIC entry $0.393 against a
re-adjusted series at $12.53 fabricates a −3087% "move"; HUBC the same).

Re-run with those breaks excluded — any row where today's vendor-pinned series disagrees with the
stored snapshot by more than 2× at the entry or 5× at the exit, **12 rows, 0.5% of the population**:

| | stored | honest (entry+7d) |
|---|---|---|
| n | 2241 clean (83 unresolvable, 12 basis breaks) | |
| directional win rate | **0.6086** (902 W / 580 L) | **0.5786** (891 W / 649 L) |
| hold accuracy | **0.549** | **0.518** |
| **grade changes** | — | **852 / 2241 = 38.0%** |
| **pnl sign flips** | — | **617 / 2241 = 27.5%** |
| abs pnl deviation | — | median **3.70 pp**, p75 8.34, p90 14.68, p99 37.10 |
| **mean pnl** | **+2.254%** | **+0.681%** |
| median pnl | +1.050% | +1.032% |

So the level damage is **+1.57 pp of mean return per decision overstated**, and the win rate is
**3.0 pp too high** — but the *ordering* damage is far larger: **38% of every stored grade changes**
and **27.5% change sign**. The median return barely moves, which is exactly what a 43-day drift on a
roughly symmetric distribution should do to an order statistic; it is the per-decision grades, not
the central tendency, that the drift destroys.

Confidence-bucket calibration, the input to `honesty_score` and the Kendall-tau discrimination term:

| stated conf | realized win% (stored) | realized win% (honest) |
|---|---|---|
| 55 | 36.4 (n=22) | 52.2 (n=23) |
| 60 | 52.5 (n=40) | 61.9 (n=42) |
| 65 | 45.7 (n=164) | 57.3 (n=164) |
| 70 | **62.7** (n=316) | **53.4** (n=322) |
| 75 | 63.1 (n=488) | 59.8 (n=498) |
| 80 | 65.6 (n=32) | 55.9 (n=34) |
| 85 | 67.1 (n=301) | 58.8 (n=318) |
| 90 | 67.7 (n=62) | 61.1 (n=72) |
| 95 | 45.5 (n=44) | 52.0 (n=50) |

The stored curve rises cleanly 45.7 → 67.7 across 65-90 and reads as a well-discriminating desk.
The honest curve is nearly flat (57.3 → 61.1) and **inverts at 70**. The isotonic map in
`app/autoresearch/confidence_calibration.py:92` — which reads the whole collection with no time
window — is fitted on the first column.

### 4c. The live panel number

The exact cohort `decision_audit._audit_decisions` scores (`resolved_at` within 30d, limit 100 —
n=100, proxy median lag 7.2 d, **nothing beyond 30 days**), run through the real formula both ways:

| | stored | honest exit | honest exit + re-anchored entry |
|---|---|---|---|
| **hold accuracy** | **0.633** | **0.506** | **0.443** |
| ECE | 0.085 | 0.151 | — |
| honesty_score | 0.830 | 0.697 | — |
| calibration_score | 0.881 | 0.788 | — |
| decision_quality_score | 90.0 | 93.6 | — |

`decision_quality_score` barely moves because the score is a blend and the directional cohort in the
last 30 days is only 5 rows (3 W / 2 L) — but **hold accuracy moves 19 points and crosses the
`hold_accuracy < 0.50` issue threshold at `decision_audit.py:370`.** The panel currently reports a
healthy 63%; the honest number on the *same 100 decisions* is 44%.

This is the important result: **the horizon defect is not only historical.** Even on a cohort whose
wall-clock lag is a compliant 7.2 days, "the newest bar at sweep time" is not "the bar at entry+7d",
and HOLD grading — which turns on a ±1% band — is exquisitely sensitive to a one-bar difference.

### 4d. The entry price is the other half, and it is not fixed

`entry_price` is `latest_close` at cycle time and no `entry_date` is stored. Measured against the
vendor-pinned series:

- which bar is it? bar −0 **48.0%**, bar −1 **42.7%**, bar −2 2.7%, bar −3 0.7%, bar −4 0.7%,
  no match in 15 bars 5.3% (n=150). **The left edge of every return moves with when the price
  loader last ran.**
- how far off is it? On the live 100-row cohort, stored `entry_price` vs the last close ≤ `created_at`:
  median **0.59%**, p90 **3.17%**, max **30.68%**; **54.8% differ by more than 0.5%**, 19.0% by more
  than 2% — against a ±1.0% WIN/FLAT/HOLD_CORRECT band. **The entry noise alone is the same order as
  the classification threshold.**
- and it compounds: hold accuracy on that cohort goes 0.633 → 0.506 (exit fixed) → **0.443**
  (entry also anchored to the close on the decision date).

## 5. Downstream readers — blast radius

Every file:line below reads an outcome, a grade, or a number derived from one.

**(a) Metrics / calibration**
- `app/autoresearch/auditors/decision_audit.py:138` count; **`:170` the primary 30-day read**;
  `:216` win_rate; `:226` `win_rate_score = win_rate/0.60`; `:232` `_bucket_win_rate`;
  `:250-256` **ECE**; `:258` honesty; `:280` Kendall-tau discrimination; `:283` calibration_score;
  `:298-305` risk_score and the final `0.4·win + 0.3·calib + 0.3·risk`; **`:312` hold_accuracy**;
  `:361/:365/:370` the issue thresholds (win<40%, ECE>0.15, **hold<50%**)
- `app/autoresearch/confidence_calibration.py:92` isotonic map over the **whole collection, no time
  window**; `:148-163` `calibrated_confidence`
- `app/autoresearch/core.py:189, :209, :223, :230-237, :274` — copies the stats into
  `perf_metrics` and persists `autoresearch_reports.decision_quality_score`; `:143` challenger resolve
- `app/quant/confidence_calibration.py:42-44` frozen Brier constants; `:63` `empirical_win_rate`;
  `:91-106` `shadow_record`
- `app/autoresearch/eval_engine.py:264/:266` (**dead — no production caller**); `:352` trace scoring
- `app/quant/residual_alpha.py:105, :213-225`
- `app/autoresearch/janitor.py:122-127` degenerate-score detector

**(b) Agent / skill scoring + champion selection**
- `app/autoresearch/scorecard.py:181` `_governed_outcomes`; `:135-163` weighted score; `:215`
  `_incomplete_rate`; `:245` CONTAMINATED/IMMATURE/HEALTHY; **`:323` `regression_verdict`**
- `app/autoresearch/skill_optimizer.py:240` baseline score; `:281/:300` `_decisions_governed`;
  **`:507-519` rollback / abstain decision**
- `app/autoresearch/sequential.py:66-93` **e-value promotion gate**
- **`app/v3/challenger.py:121-165` — itself defective (see §1)**
- `app/trading/strategy_tracker.py:188-212` rankings; `:232-237` confidence bonus at >55% win rate;
  `:266-276` bench at <40%
- `app/cognition/evaluation/strategy_auditor.py:38/:44/:356-415/:521`

**(c) LLM prompts and memory — the score is quoted back to the agents**
- **`app/agents/base_agent.py:376`** reads the last 5 resolved outcomes for the ticker;
  **`:398-406`** renders `## PRIOR TRADE HISTORY FOR {ticker}` with entry/exit/pnl and
  *"Use this history to calibrate your confidence — do not repeat past mistakes."*
- **`app/agents/base_agent.py:442`** 90-day WIN/LOSS read; **`:468-479`** renders
  `## CONFIDENCE CALIBRATION (fleet track record, last 90 days) … won X% of N trades` and
  *"you are overconfident"*; `:552-553`/`:575` splices it into the **system prompt** for 7 agents
- `app/v3/data_report.py:485-486, :494-497, :543` — into the report every V3 agent reads
- `app/autoresearch/reflection.py:56-73` — the `=== PREDICTION ACCURACY (last 30 days) ===` block
  (win rate, hold accuracy, calibration) handed to the reflection LLM; `:165`, `:185`
- `app/services/memory/episodic_memory.py:34-58` write, `:62-75` ranks episodes by `outcome_score`
- `app/services/memory/working_memory.py:86-93` renders `Outcome Score:` into the agent prompt
- `app/services/memory/consolidator.py:147-155` `RESOLVED OUTCOME: {label} ({move}%)` into the
  consolidation prompt
- `app/services/retrieval_context.py:30-40, :248`
- `app/autoresearch/outcome_tracker.py:73-149` `write_outcome_to_memory` — the write side of this loop
- `app/v3/orchestrator.py:2228-2232` confidence shadow

**(d) HTTP / UI**
- **`app/routers/eval_trust_router.py:90`** `outcome_contract.horizon_days = RESOLVE_AFTER_DAYS`
  and **`:178`** `contract: {horizon_days: 7}` — **a constant asserted over a cohort whose measured
  median horizon is 43 days.** `:125-127` the reads; `:163` win_rate; `:172` hold accuracy; `:155` ETA
- `app/routers/challenger_router.py:89, :113, :117, :169-179`; `:35-39` regressing sectors
  (the promotion blocker); `:60-74`
- `trading-client/app/routers/model_stats.py:155, :170-190, :250-269` — the **model leaderboard**
- `trading-client/app/routers/autoresearch.py:63`; `trading-client/app/services/subsystem_benchmarks.py:65-74`
- `trading-client/app/routers/archive.py:27` (generic browser)
- Frontend: `AutoResearchPanel.jsx:1001-1003` Decision Quality card, `:1050-1052` Directional Win Rate,
  `:1058-1059` HOLD accuracy, `:1075` ECE; `EvalTrustSection.jsx:281-282, :330-332, :389`;
  `DecisionRenderers.jsx:25-26`; `ModelStatsPanel.jsx:6`; `api.ts:606-664`

**(e) Scripts / reports** — `scripts/agent_scorecard.py:176/:440-446/:563/:709-740`;
`scripts/skill_version_scorecard.py:143/:222`; `scripts/calibration_report.py:181-190`;
`scripts/calibrate_confidence_floor.py:105-136`; `scripts/decision_score_report.py:215/:221`;
`scripts/power_report.py:148/:157`; `scripts/self_consistency_bench.py:128/:244/:355-361`;
`scripts/cycle_audit.py:139/:227/:305`; `scripts/verify_fidelity_fixes.py:253`;
`scripts/quality_census.py:337-344`

**One independent, on-contract oracle exists.** `scripts/agent_scorecard.py:260 fetch_rows_from_prices`
— the **default** `--source price` path — never touches `decision_outcomes`: it takes the desk date
and asks `app.quant.returns.forward_move_pct` for a fixed forward *session* window. `score_panel.py`,
`residual_alpha_report.py`, `override_matrix.py` and `override_diagnosis.py` all take that path and
are **unaffected** by this defect. They are the natural control for any remediation.

## 6. The contract that should be written — and what the code does today

| element | today | verdict |
|---|---|---|
| **entry timestamp** | none stored. `entry_price = latest_close()` at cycle time = the newest bar in `price_history` when the cycle ran. Measured: bar −0 48%, bar −1 43%, up to bar −4. | **UNSPECIFIED** |
| **entry price basis** | a frozen snapshot; `price_history` is re-adjusted afterwards. 54.8% of live-cohort entries differ from today's close-on-decision-date by >0.5%, max 30.7%. | **UNSPECIFIED / drifts** |
| **calendar convention** | `created_at + timedelta(days=7)` — **7 CALENDAR days**, then walk forward to the first bar. Measured on 221 rows: the span is **6 trading sessions in 100% of cases**. | **calendar, not trading** — and it disagrees with `agent_scorecard`'s 7 *sessions* by one bar |
| **target timestamp** | `created_at + 7 calendar days`, midnight-relative to a datetime. No market-close alignment. | partially specified |
| **price source** | `price_history`, vendor pinned by `dominant_source_for` (fresh-then-deep). The pin is applied at *read* time, so entry and exit can come from different vendors if dominance changed. `challenger.py:139` applies **no pin at all** (188 dual-vendor tickers). | **specified for outcomes, ABSENT for challenger** |
| **adjusted vs unadjusted** | never stated. yfinance = adjusted, polygon = raw; the dominant-source rule picks per ticker, so the basis varies by ticker and over time. | **UNSPECIFIED** |
| **weekends / holidays** | handled — `close_on_or_after` walks forward, grace 5 days. Measured: 65% land on the horizon date, 25% walk 1 day, 10% walk 2; none beyond 2. | **SPECIFIED (new)** |
| **delistings / missing data** | `(None, None)` → `continue` with a `logger.debug`. The row stays pending forever; **there is no reaper and no alarm**, and `resolve_pending_outcomes` retries it every sweep. 83 of 2,336 (3.6%) historical rows are in this state. | **UNSPECIFIED — fails silently** |
| **splits / corporate actions** | nothing. A frozen entry snapshot against a re-adjusted series fabricates the move (TDIC −3087%, HUBC −780%). Not fixed by `1bfd700f`. | **UNSPECIFIED** |
| **late fills** | `trade_fills` carries `fill_price` / `filled_at` / `decision_price` (71 rows) and `decision_outcomes` never reads it. The outcome is a paper close, not a fill. | **UNSPECIFIED** |
| **early exits** | `resolve_outcome_for_exit` resolves at any age against the live exit price, stamping no horizon marker; 37 such rows sit in the same cohort as the 7-day claims. | **conflated** |
| **row-level provenance** | `exit_date` + `horizon_days` are written by the new code, on 0 rows so far. `SCORE_VERSION` is still `"v6"` — unchanged by the fix, so a report computed on the drifted cohort and one computed on the honest cohort are indistinguishable in `autoresearch_reports`. | **not yet effective** |

## 7. What is *not* wrong

- `close_on_or_after` is correct and well-tested (`tests/unit/test_outcome_writeback.py:237, :323,
  :340, :354, :362-363`, proven red by reverting to `latest_close`).
- The fix commit explicitly declines to rewrite history and says so: *"the 2,694 already-resolved
  rows are left as they are … they need versioning or quarantine, which is an operator call."*
  That is the right call; the open item is that nobody has made the call.
- `decision_audit.py:163-167` already reasons about the cohort-age gap and surfaces
  `median_decision_age_days`. The instrument to notice this existed; nothing read it.
- `decision_evaluations` is an LLM-judge quality score with no price in it — it is untouched by this
  defect. `decision_scores` is effectively write-only in the service.

## 8. Recommended order

1. **Quarantine, do not rewrite.** Stamp the 2,694 pre-fix rows with `horizon_basis: "latest_close"`
   (and the post-fix ones implicitly by `exit_date`). Every consumer in §5 then gets a filter it can
   apply, and the evidence survives. Bump `SCORE_VERSION`.
2. **Fix `app/v3/challenger.py:139`** with `close_on_or_after` + the vendor pin. It is the same bug in
   the same repo and it gates promotion.
3. **Store `entry_date`, and anchor the entry** to `close_on_or_after(ticker, created_at)` rather than
   `latest_close`. Until then the left edge of every return is unspecified and hold grading is noise
   at the ±1% band.
4. **Decide calendar vs trading days** and make the two in-repo oracles agree — 7 calendar days is
   6 sessions; `agent_scorecard` uses 7 sessions.
5. **Give the unresolvable rows a terminal state** (`outcome: "NO_PRICE_AT_HORIZON"`) so 3.6% of the
   population stops being invisible.
6. **Re-derive the calibration map and any frozen Brier constants** (`app/quant/confidence_calibration.py:42-44`)
   once a clean cohort exists — and validate against `agent_scorecard --source price`, which is the
   independent on-contract oracle already in the repo.

---
*Evidence scripts: `/tmp/claude-1000/-home-lazycat-github-projects-sun/ef745d5b-cfc3-4282-a7b5-4f7c165fc03d/scratchpad/{q1..q8,recompute,recompute_big,full_recompute,robust,score30,entryvar,sessions,clean}.py`.
No file in the repo was modified; no write reached any database.*
