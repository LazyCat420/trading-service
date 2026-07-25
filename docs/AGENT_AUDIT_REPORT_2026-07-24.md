# V3 Agent Audit — Report

**Date:** 2026-07-24 (re-tested 2026-07-25) · **Scope:** `trading-service` V3 cycle, every agent, in pipeline order
**Status:** Phases 0–6 shipped and verified live across **three** cycles · Phases 7–8 open
**Head:** `e633377` · **Tests:** 1241 passing (from 1141), 7 skipped

> **Re-test 2026-07-25** — a third cycle (`AXP/LLY/AMD`, tickers untouched by the
> first two, mixing held and unheld) confirms Phases 0–6 on the deployed image:
> **10/11 checks pass**, the 11th being a pre-existing desk-persistence gap
> (open item 7), not an agent fault. Three earlier claims changed:
> **HRP is confirmed live** with real varying weights, unblocking the top open
> item (with a units trap — see item 1); **"the synthesizer never overrides the
> board, 0 of 53" is retracted** — 108 of 557 (19%), 8 hard flips (item 3); and
> **`technicals` staleness is far worse than reported** — 1% of tickers fresh,
> not a handful of 7–9 day laggards (item 5).

---

## 1. TL;DR for whoever picks this up

Seven agents were audited in pipeline order. Six shipped fixes; all are verified
against two real cycles run through the deployed image.

**The three findings that matter most:**

1. **A single unresolved `bot_id` was silently disabling four systems**, including
   HRP position sizing (which had *never once* run in production) and the
   portfolio drawdown breaker (which could never trip). One line at the call site.
2. **The quant analyst was inventing its risk numbers** — 56% of reported RSI
   values matched nothing on the desk, in runs that made zero tool calls.
3. **A headline conclusion of this audit was wrong and has been retracted.**
   "The decision layer destroys the research layer's value" was a measurement
   artifact. See §3 — read it before trusting any earlier number.

**Two rules the tooling now enforces**, both learned the hard way:

- Score decisions with `--executable-only`. **69% of decisions cannot change the
  book** (policy-blocked SELLs on unheld tickers, no-op HOLDs). Scoring them
  measures opinions, not trades.
- Beat the printed **always-long baseline, never zero**. An agent long in a
  rising tape looks brilliant against zero.

---

## 2. How to verify any of this yourself

```bash
# run a cycle in the DEPLOYED container, no trades placed, visible in the UI
python scripts/observe_cycle.py --tickers TSM,VZ

# check every audit claim against the resulting desks
python scripts/verify_audit_phases.py --cycle <cycle_id>

# per-agent accuracy vs the always-long null hypothesis
python scripts/agent_scorecard.py --source price --horizon 7 --executable-only
```

`observe_cycle.py` enqueues `START_V3_CYCLE` onto `v3_system_commands` — the same
queue the UI's Start Cycle button uses. **Do not** call
`PipelineService.start_cycle()` in-process to test: it runs your checkout rather
than the deployed image, and `emit` events stay in-process, so the frontend's
Live Event Logger sits empty while Market Data shows ANALYZING. That split-brain
looks like an app bug and isn't.

### Live verification results

Two cycles, `JPM/NVDA/MP` (local) and `TSM/VZ` (deployed container). **11/11 pass
on both.**

| Check | Result |
|---|---|
| `bot_id` populated on the desk | `test_bot` (was `''`) |
| HRP line in `quant_math_context` | 2/2 and 3/3 — **was 0 of 51, ever** |
| `held` resolved correctly | TSM/VZ/JPM `True`, NVDA/MP `False` |
| ONE regime per cycle | both `CONTRADICTORY` |
| regime `forward_call` emitted | 2/2 that ran |
| junior `triage_recommendation` | 2/2 (was missing in 27% of runs) |
| junior `catalyst_call` | 2/2 |
| fundamental `horizon` + `near_term_read` | `QUARTERS` on both |
| quant metrics reconciled vs `technicals` | 0 corrections needed |
| no SELL survives on an unheld ticker | none |

**Timing:** tournament **246.4s → 99.8s** (2.5×). Regime averages **0.5 loops**
across two tickers — one computed, one reused. Per-ticker **609s → ~478s**.
*Caveat: n=2 on an after-hours market. Directional, not settled.*

#### Third cycle — independent re-test (`cycle-observe-1784946884`, AXP/LLY/AMD)

Run after the report was written, on tickers none of the earlier cycles touched,
deliberately mixing **held** (AXP, LLY) and **unheld** (AMD) to exercise both
sides of the no-shorting gate. **10/11 pass; the 11th is a pre-existing
persistence gap, not an agent fault** (see below).

Decisions: `AXP SELL @60` (an executable exit on a held position — the rarest
decision class, 9 of 821 before this wave), `LLY HOLD @64`, `AMD HOLD @78`.

Three things this cycle established that the first two could not:

- **The unheld-SELL fix works by persuasion, not by patching.** AMD's tournament
  returned **SELL @80% (bear won)** with all three research desks BEARISH, and
  the board still chose HOLD. `coerce_unshortable_sell` **never fired** — no
  coercion line in the logs. The never-shed constraint changed what the board
  *concluded*, so the backstop had nothing to rewrite. That is the intended
  mechanism rather than a late correction.
- **`portfolio_context` survives real overflow pressure.** The board shed 4
  sections and the synthesizer 3 on this cycle (both still overflowed to the
  system prompt). `portfolio_context` was in neither shed list — the `_KEEP`
  fix holds under exactly the condition that originally broke it.
- **Quant reconciliation is now exact.** All 7 post-fix desks match
  `technicals.rsi_14` to the cent (AXP 65.95, LLY 50.41, JPM 60.78, NVDA 47.89,
  VZ 42.53, TSM 41.03, IP 50.80). Against 56% fabricated before, this is the
  most conclusively fixed finding in the audit.

**Timing across the full history**, not just the like-for-like pair: tournament
mean **483.3s (n=229) → 86.6s (n=4)**, ~5.6×. Per-ticker agent-time
**600–800s → 260–473s**. AXP 473.3s, LLY 448.5s end to end.

---

## 3. ⚠ Retraction — read before trusting earlier numbers

Mid-audit I reported that the decision layer destroyed the research layer's
value: board 41.5% hit / −0.49 edge against a 60% tournament. **That was wrong.**
Two stacked scoring errors:

1. **Scoring opinions, not trades.** 69% of scored decisions cannot change the
   book — 137 SELLs on unheld tickers (no shorting; policy-blocked at the very
   end) and 177 HOLDs on tickers not held (pure no-ops). Only 143 of 457 were
   consequential, and the blocked SELLs alone carried −1.02%, dragging every
   aggregate negative.
2. **HOLD on a held position was scored as predicting flatness.** It means
   *stay long*, and it is right when the position rises. Those 46 desks rose
   9.9% on average and scored 0% under the flat rule. The ±1% deadband is also
   miscalibrated: the mean absolute 7-session move is **4.24%**.

Correcting both moves the board from 39.6% / −1.75 to **68.5% / +3.17** — worst
in the pipeline to best.

**The corrected number is not evidence of skill either.** Over the same
consequential desks, always-long earns **+4.05%** and the board **+2.91%** —
i.e. **−1.14% against the null** in a 95-up/30-down window. The board is neither
destroying nor creating value; it tracks beta while rarely exiting (9 held-SELLs
of 143).

Every conclusion drawn before this correction was measured against zero.
`agent_scorecard.py` now always prints the always-long baseline and an
executability breakdown so this cannot recur silently.

---

## 4. What shipped, by phase

| Phase | Agent | Core finding | Commit |
|---|---|---|---|
| 0 | — | Built the accuracy scorecard; joined desks to realized P&L | `65a4050` |
| 1 | regime_engine | Global classifier ran **per ticker** | `65a4050` |
| 2 | junior_analyst | Triage field missing 27%; budget-starved | `c4df26e` |
| 3 | fundamental_analyst | **No horizon concept existed in V3** | `343fb37` |
| 4 | quant_analyst | **Fabricated risk numbers in 56% of reports** | `43a79fd` |
| 5 | tournament | Serialization rationale was stale → 2.5× | `cd5801d` |
| 6 | board | 29% of compute on unplaceable trades | `90e7452` |
| — | cross-cutting | **`bot_id` never passed** | `a97929e` |

### Phase 1 — regime engine

An agent whose prompt says *"classify the GLOBAL market state, never individual
tickers"* was invoked once per ticker. **35 of 64 multi-ticker cycles disagreed
with themselves** — same cycle, minutes apart, one ticker `DEEP_DISCOUNT` and
another `CONTRADICTORY` off the same snapshot. 25 of 121 cycles reported more
than one VIX level (one cited 15.03, 15.57 **and** 22.00). The label selects the
Board persona, so persona routing was partly noise.

Also: three of seven factor scores were computed from data the engine never had.
The briefing was a list of *levels*; `trend_strength`, `sector_momentum` and
`liquidity` are slope/breadth questions. Zero tool calls in 366 runs, yet
`trend_strength` averaged 0.81. `app/v3/macro_trend.py` now computes real inputs
from `asset_prices`.

Added `forward_call` (5-day SPX/VIX direction) — the engine previously predicted
nothing, so it could never be wrong. Graded by `scripts/grade_regime_calls.py`.

### Phase 2 — junior analyst

96% of runs finished at the 5-turn ceiling, so "TRACE one lead depth-first" — the
step that produces a quantified finding instead of five headlines — was
structurally unreachable, and 34% never made their mandatory whiteboard write.
Budget 5 → 7, and the mandated step-1 `whiteboard_read` removed (the whiteboard
summary is **already injected** into every agent's prompt; that call re-fetched
what the agent was holding).

`triage_recommendation` routes the pipeline and anything unrecognized becomes
FULL — it was missing in **90 of 337 runs** with nothing logged. Now required.

### Phase 3 — fundamental analyst

The obvious hypothesis was wrong: 37% of runs make zero tool calls, but those
runs score **better** (40% vs 29%). "Call more tools" would have been the wrong
fix.

The real finding: **`grep horizon` returned nothing across all of V3.** A
multi-quarter business view was consumed as a vote on a trade resolving in 7
days. BULLISH calls averaged a **−0.54%** realized move, BEARISH **+0.72%**, at
76–84 stated confidence. Added `horizon` + `near_term_read`.

### Phase 4 — quant analyst

Tracing every RSI in 305 reports back to the text the agent was given: only 134
matched a number anywhere on the desk. **171 did not, 148 from zero-tool runs.**
IP reported 58.0 against a desk value of 71.19; GOOGL 47.0 against 53.7. These
set `volatility_regime` and stop placement, and the Board reads them as fact.

Root cause is a category error, not a weak prompt — those values already sit in
the `technicals` table. `app/quant/technical_baseline.py` computes, injects, and
reconciles them, keeping the model's originals so the rate stays measurable.

### Phase 5 — tournament

40% of cycle wall-clock in 9 sequential LLM calls, serialized by a comment citing
Prism 409s. **The rationale was stale** — the juror stage already gave each
juror its own conversation to fix a different bug, which removed the collision.
Parallelized all three stages caller-side (no Prism edits). Slot acquisition goes
through the existing `AdaptiveConcurrencyController` (vLLM `/metrics`-aware) so
fan-out cannot outrun KV cache, and personas share a system prefix to preserve
prefix-cache reuse.

### Phase 6 — board / unactionable waste

Across 821 decisions / 202 agent-hours:

| decision | n | agent-hours | avg |
|---|---|---|---|
| HOLD unheld (screening "no") | 343 | 55.1 | 578s |
| BUY (actionable) | 182 | 53.3 | 1054s |
| **SELL unheld (CATEGORY ERROR)** | **167** | **57.7** | **1243s** |
| HOLD held | 120 | 32.2 | 966s |
| SELL held (exits) | 9 | 3.6 | 1443s |

The unactionable SELLs were the **most expensive decisions in the system and the
least useful** — 29% of all compute, every one blocked at the very end.

**Root cause: the constraint was being shed from the prompt.**
`portfolio_context` carries *"the bot cannot SELL what it does not hold (no
shorting)"* and sat at `shed_order 2` — among the first sections dropped when a
prompt overflowed Prism's 2048-token embedder. A hard legality constraint was
being discarded to save tokens. Now `_KEEP`, with a validator backstop and a
no-trade-available gate. Kill switch: `V3_NO_TRADE_GATE=false`.

### Cross-cutting — the `bot_id` bug

`run_v3_pipeline(bot_id: str = "")` was never passed one by its only caller, so
every desk stored `bot_id=''` and fallbacks resolved to `settings.BOT_ID`
(`lazy-trader-v4`, **zero positions**) while the active bot held 9.

| System | Was | Now |
|---|---|---|
| HRP sizing | **never ran** (needs ≥2 tickers, saw empty book) | computes |
| `held` flag | **False for everything**, incl. genuinely-held | correct |
| Drawdown breaker | 0 rows → `None` → **could never trip** | −0.45% |
| Book brief | described an empty portfolio to quant + board | real |

GARCH needs no `bot_id` and kept working, so the block looked healthy — of 51
desks carrying one, **zero** had an HRP line. The board was never "ignoring" the
covariance math; **it was never given any.**

`resolve_bot_id()` (explicit > active > settings) in `portfolio_tools` is now the
single resolver. **Never reintroduce a bare `settings.BOT_ID` fallback.**

---

## 5. Open items, highest value first

1. **Position sizing is a habit, not a calculation.** Across 181 BUYs the board
   used **10 distinct values**; 80% were exactly 3%, 4% or 5%. Now genuinely
   fixable since HRP produces real numbers for the first time. Proposed: compute
   the binding constraints in code and hand the agent a **bracket** — risk-based
   size (1% equity at the ATR stop), HRP ceiling, cash, concentration cap, and
   which one *binds*. Four lines, not a data dump.

   **✅ HRP prerequisite confirmed (2026-07-25) — this is unblocked.** The
   report asked to check "FIRST, do not assume" whether the quant was failing to
   populate `hrp_weight_suggestion` or HRP was genuinely returning ~0. **Neither.**
   Across 8 post-fix desks the field carries real, varying, correlation-aware
   weights: JPM 0.123, VZ 0.192, LLY 0.099, TSM 0.069, AXP 0.063, NVDA 0.006.
   The old "mostly 0.0" reading was the `bot_id` bug — HRP was never running at
   all, not running and returning zero.

   **But there is a units trap to design around.** On the TSM/VZ cycle the board
   sized VZ at **19.2%** — exactly HRP's `0.192 × 100`. The injected line is
   correctly worded ("target weight for VZ = 19.2% of equity"), so the agent
   copied it faithfully; it just read a *portfolio target weight* as a *single
   order size*. It was contained — `resolve_buy_size_pct` clamps at
   `MAX_POSITION_SIZE_PCT` (live value **0.10**), so it would have executed at
   10% — but that is the cap catching it, not the reasoning. Cap-binding is
   otherwise rare (4 of 182 BUYs, 2%), and 19.2% is the **only** size in the
   whole history anchored to a computed number rather than a habit. The bracket
   must therefore state units explicitly and label HRP a *ceiling*, not a size.
2. **Re-derive the Phase 6 counts.** The 69% figure comes from the `held` flag,
   which was wrong for every desk before `a97929e`. The SELL direction holds;
   the counts will move.
3. **Phases 7–8**: decision synthesizer, then whole-cycle + communication layer.

   ⚠ **Correction (2026-07-25): "the synthesizer never overrides the board
   directionally — 0 of 53" is wrong.** It was an artifact of the 53-desk
   window. Over all **557** desks carrying both a board and a synthesizer
   action, the synthesizer differs on **108 (19%)**, including **8 hard
   direction flips** (BUY↔SELL: AAPL, IP, BAC, FCF, F×2, C, CRM) plus 38
   BUY→HOLD and 25 SELL→HOLD de-escalations.

   This changes the Phase 7 question. The synthesizer is *not* a directional
   rubber stamp, and on the scorecard its overrides are the single best-scoring
   handoff in the pipeline — `synthesizer overrides board`: **n=12, +5.52 edge,
   88% hit**, against agreeing (n=74, −0.61 edge, 48%). Small n, and it is the
   only handoff of the four that pays, so treat it as a hypothesis worth
   powering rather than a settled result. **Do not open Phase 7 by assuming the
   layer is inert.**
4. **Junior's `SKIP` has never fired** in 337 runs; `QUANT_ONLY` fires 2%. The
   Triage Gate (separate, pre-existing) *does* work — it skipped MP in **0.8s vs
   ~480s**. Worth asking whether junior-level triage should exist at all.
5. **`technicals` is stale for most tickers** — and it is worse than the
   "GOOGL 7d, IP 9d" spot checks suggested. Measured across the table
   (2026-07-25): **only 5 of 503 tickers (1%) are fresher than 3 days.** The
   bulk sit at 8–10 days and the tail runs to months — VZ's RSI came from
   **2026-05-15, 71 days stale**, and still fed a live BUY.

   Phase 4 therefore replaced *fabricated* numbers with *stale* ones. That is a
   genuine improvement — they are now real, sourced and traceable, and
   `technical_baseline` handles staleness correctly (5-day threshold, an
   explicit `⚠ STALE … treat levels as indicative` line, and the conditional
   correction rule in §6) — but the reconciliation is authoritative far less
   often than the report implied, so this is a bigger lever than it looked.

   One wording nit while fixing the collector: the injected block's header says
   these are "the authoritative values; do NOT restate them from memory or
   estimate around them", which sits awkwardly above a 71-day-old RSI. The
   stale branch should soften the header, not just append a warning.
6. **The synthesizer still overflows the 2048-token embedder** every run and
   routes to the system prompt, losing KV-cache reuse. Pre-existing. The board
   overflows too (~5559 tok non-sheddable after shedding 4 sections).
7. **~2% of desks never persist their `final_decision`, though the decision was
   made.** Found 2026-07-25 on AMD: the logs show the board wrote
   `final_decision` (HOLD @65) and the synthesizer appended `trade_decision`
   (2145 bytes, HOLD @78), and `trade_results` has the row — but
   `shared_desk.desk_data->'final_decision'` is `null` and the desk's
   `updated_at` froze ~96s before the decision was produced. **9 of the last
   400 `trade_results` rows (2%)** have the same split, going back to 2026-07-06
   (GOOGL, MCD, QCOM, PAM, LLY, AMZN, AMP, TSLA, BAC) — pre-existing, not a
   regression from this wave.

   Two consequences. It is the single ❌ in the third verification cycle, so
   `verify_audit_phases.py` reports a FAIL that is a *persistence* fault rather
   than an agent one. And every desk-based measurement in this report silently
   drops these rows — **all 10 observed cases are HOLDs**, so the omission is
   not random. Reconcile `shared_desk` against `trade_results` before quoting
   any desk-derived count.

---

## 6. Traps — please read before changing this code

- **`quality_score` is not accuracy.** It grades artifact *shape*. An agent can
  score 82 and be wrong two-thirds of the time; the quant analyst did exactly
  that. Judge with the scorecard.
- **`strategy_performance` is dead.** `evaluate_pnl()` has zero callers (60/1979
  resolved) and every V3 row is stamped `agent_prompt_hash="v3_pipeline"` — one
  bucket for the whole pipeline. Use `decision_outcomes`.
- **`market_regime` table is a placeholder** — `vix_zscore` 0.0,
  `breadth_sp500` 50.0, yields NaN on all 78 rows. Don't wire it into anything.
- **Historical `held` is untrusted.** 16 tickers show `held=true` with **no BUY
  fill in any bot's history**, and agents were shown fabricated positions
  ("CURRENTLY HOLDING WFC: Entry $86.30, P&L +0.1%, Held 10 days"). Current code
  returns False for these — legacy artifact, root cause never established.
- **Quant reconciliation is conditional on purpose.** Fresh baseline → correct.
  Stale + agent used no tools → correct anyway and flag. Stale + agent used tools
  → do **not** overwrite. Simplifying this to an unconditional overwrite
  reintroduces a different wrong number.
- **Sanitize NaN where values are consumed, not only where fetched.** NaN
  survives a `NOT NULL` check and compares false against every threshold.
- **`shed_order` can drop load-bearing constraints.** That is exactly how the
  no-shorting rule vanished. Anything that changes what is *legal* belongs at
  `_KEEP`.
- **Distinguish "didn't run" from "ran and omitted".** This audit made that error
  three times, including once in its own verification tool. Hence the explicit
  `n/a` state in `verify_audit_phases.py`.
- **n is small.** The scorecard baseline is 111–143 consequential decisions in a
  single rising window. No individual cell is significant.

---

## 7. Files

**New:** `app/v3/macro_trend.py`, `app/v3/regime_cache.py`,
`app/quant/technical_baseline.py`, `scripts/agent_scorecard.py`,
`scripts/grade_regime_calls.py`, `scripts/observe_cycle.py`,
`scripts/verify_audit_phases.py`

**Notably changed:** `app/v3/orchestrator.py`, `app/v3/agent_runner.py`,
`app/v3/artifact_validators.py`, `app/tools/portfolio_tools.py`,
`app/services/pipeline_service.py`, `app/cognition/debate/tournament.py`,
all seven agent prompts under `app/v3/agents/`

**Also read:** `HANDOFF.md` (working state), `AGENTS.md` (harness rules — note
its §14 and the Layer-3 debate description are stale; the tournament, not
bull/bear, is the live debate path).
