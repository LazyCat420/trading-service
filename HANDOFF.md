# HANDOFF — Agent-by-agent audit of the V3 trading cycle (2026-07-24)

A systematic audit of every agent in the V3 cycle, in pipeline order, each
phase building on the last. **Phases 0-6 are done and live** (`90e7452`).
Phases 7-8 are not started.

> ## ⚠ READ THIS BEFORE TRUSTING ANY NUMBER IN THIS FILE
>
> Midway through, this audit's own measurement was found to be wrong, and it
> had inverted a headline conclusion. **"The decision layer destroys the
> research layer's value" is RETRACTED** — it was two stacked scoring errors:
> 69% of scored decisions could not change the book (policy-blocked SELLs on
> unheld tickers, no-op HOLDs), and HOLD on a *held* position was scored as
> predicting flatness when it means staying long. Correcting both moved the
> board from 39.6% hit / -1.75 edge to 68.5% / +3.17.
>
> The corrected number is **not** evidence of skill either: always-long over
> the same desks earns +4.05% vs the board's +2.91%, i.e. **-1.14% against the
> null** in a 95-up/30-down window.
>
> **Always run `--executable-only` and beat the printed always-long BASELINE,
> never zero.** Every conclusion drawn before 2026-07-24 was measured against
> zero and is suspect.

**Decisions taken with the user for the remaining phases:**
1. Tournament (5) and Board (6) are audited **together** — the board's whole
   job is consuming the debate, so auditing them apart analyses the same
   handoff twice.
2. Any board change ships **shadow-first**: log what the new behavior WOULD
   decide alongside the live decision, the way `contradiction_shadow` already
   works, and only promote it once there is evidence on live data. The board
   moves real money; n=53 is not enough to rewire it blind.
3. The tournament gets **parallelized caller-side only** — no Prism edits
   (standing rule). See the concurrency constraints below before starting.

The governing rule for this wave: **no agent gets tuned without a measurable
target.** That is why Phase 0 exists and why it came first.

---

## What is live right now

**`65a4050`** — deployed to synology 2026-07-24T20:09Z, health OK on :3031.
1156 tests pass (1141 before this wave).

### ✅ Re-tested 2026-07-25 — third live cycle, independent tickers

`cycle-observe-1784946884` (**AXP/LLY/AMD**) through the deployed container —
tickers none of the earlier verification cycles touched, deliberately mixing
**held** (AXP, LLY) and **unheld** (AMD). **10/11 checks pass.** 1241 tests
pass locally. Decisions: `AXP SELL @60` (an executable exit — the rarest class,
9 of 821 before this wave), `LLY HOLD @64`, `AMD HOLD @78`.

The strongest single result: **AMD's tournament returned SELL @80% with all
three research desks BEARISH on an unheld ticker, and the board chose HOLD
anyway — `coerce_unshortable_sell` never fired.** The never-shed constraint
changed what the board concluded, so the backstop had nothing to rewrite. The
shed logs also confirm `portfolio_context` stayed out of both the board's
4-section and the synthesizer's 3-section shed lists under real overflow —
`_KEEP` holds under exactly the pressure that originally broke it.

**Four things changed since the report was written — read these before picking
up any open item:**

1. **HRP is confirmed live — the top open item is unblocked.** Real varying
   weights across 8 desks (JPM 0.123, VZ 0.192, LLY 0.099, TSM 0.069, AXP
   0.063, NVDA 0.006). The old "mostly 0.0" was the `bot_id` bug: HRP was never
   running, not running and returning zero. **Units trap**: the board sized VZ
   at 19.2% = HRP's `0.192 × 100`, reading a portfolio *target weight* as a
   single *order size*. Contained by `MAX_POSITION_SIZE_PCT` (live **0.10**),
   but by the cap, not the reasoning. The sizing bracket must state units and
   label HRP a ceiling.
2. **"The synthesizer never overrides the board — 0 of 53" is RETRACTED.** At
   n=557 it differs on **108 (19%)** including **8 hard BUY↔SELL flips**. Its
   overrides are also the only handoff of four that pays (n=12, +5.52 edge, 88%
   hit). Do not open Phase 7 assuming the layer is inert.
3. **`technicals` staleness is far worse than "GOOGL 7d, IP 9d"** — only **5 of
   503 tickers (1%)** are fresher than 3 days; VZ's RSI was **71 days** stale
   and still fed a live BUY. Phase 4 swapped fabricated numbers for stale ones
   (a real improvement — now sourced and traceable, and the 5-day stale guard
   works), but the reconciliation is authoritative far less often than assumed.
4. **NEW — ~2% of desks never persist `final_decision` though the decision was
   made.** AMD is the 10th case: board and synthesizer both logged, the
   `trade_results` row exists, but `shared_desk.desk_data->'final_decision'` is
   `null`. 9 of the last 400 `trade_results` rows show the same split back to
   07-06 — pre-existing. **All 10 observed cases are HOLDs**, so desk-derived
   counts drop them non-randomly. It is also the lone ❌ in the verification
   run: a persistence fault, not an agent one. Reconcile `shared_desk` against
   `trade_results` before quoting any desk-based number.

**Timing across the full history** (not just the like-for-like pair): tournament
mean **483.3s (n=229) → 86.6s (n=4)**, ~5.6×. Per-ticker agent-time
**600–800s → 260–473s**. Quant RSI now matches `technicals` to the cent on
**7/7** post-fix desks, against 56% fabricated before — the most conclusively
fixed finding in the audit.

### Phase 0 — the measurement target (`scripts/agent_scorecard.py`)

Every agent had latency/loop/`quality_score` telemetry, but `quality_score`
grades the *shape* of an artifact, not whether it was right. Nothing scored an
agent against the market.

The scorecard joins the two halves that were never joined —
`decision_outcomes` (resolved P&L, 7-day horizon) and `shared_desk` (every
agent's artifact for the same cycle+ticker; **201/201 decisions since 06-18
join**) — and reports per agent: hit rate with a Wilson 95% interval, `edge%`
(mean signed move from following that agent), Brier calibration, and the
confidence gap between right and wrong calls. It also scores handoffs: does a
downstream override of an upstream agent pay?

```bash
.venv/bin/python scripts/agent_scorecard.py --since 2026-06-18
```

**Baseline — 53 resolved decisions, 17W/24L/12F.** Re-run after each phase and
compare against this:

| agent | n | decisive | hit% | 95% CI | edge% | brier | confΔ |
|---|---|---|---|---|---|---|---|
| regime_engine | 53 | 0 | — | — | — | — | — |
| junior_analyst | 53 | 0 | — | — | — | — | — |
| fundamental_analyst | 53 | 32 | 46.9 | 31–64 | -0.09 | 0.393 | -1.9 |
| quant_analyst | 53 | 29 | 41.4 | 26–59 | +0.07 | 0.354 | -0.5 |
| tournament_debate | 53 | 40 | 60.0 | 45–74 | **+0.39** | **0.262** | +3.0 |
| board_of_directors | 53 | 41 | 41.5 | 28–57 | **-0.49** | 0.366 | +1.7 |
| decision_synthesizer | 53 | 41 | 41.5 | 28–57 | -0.49 | 0.368 | +1.2 |

`debate_judge` is omitted: the orchestrator copies `tournament_result` into it,
so it is the same row twice — do not read it as independent confirmation.

### Phase 1 — regime engine (`65a4050`)

Three defects, all measured over 14 days / 366 runs:

1. **A global classifier was running per ticker.** `run_v3_pipeline` is invoked
   per ticker, so the engine answered the same question about the same market
   up to 6 times concurrently — and **disagreed with itself in 35 of 64
   multi-ticker cycles**. 25 of 121 cycles reported more than one VIX level
   (one cited 15.03, 15.57 and 22.00 at once). The label picks the Board
   persona, so that routing was partly noise. `app/v3/regime_cache.py` now
   classifies once per cycle behind an asyncio lock.
2. **Three of seven factors were scored from data the engine never had.** The
   briefing was a list of *levels*; `trend_strength`, `sector_momentum` and
   `liquidity` are slope/breadth questions. The engine made **zero** tool calls
   in 366 runs and still returned `trend_strength` averaging 0.81.
   `app/v3/macro_trend.py` computes the real inputs from `asset_prices`.
3. **It predicted nothing, so it could never be wrong** — 53/53 artifacts
   unscoreable. It now emits `forward_call` (5-day SPX direction, VIX
   direction, conviction), graded by `scripts/grade_regime_calls.py`.

### Phase 2 — junior analyst (`c4df26e`)

- **Budget starvation**: 96% of runs finished at the 5-turn ceiling, so step 3
  "TRACE one lead depth-first" was unreachable and 34% never made their
  mandatory whiteboard write. Budget 5 → 7, and the mandated step-1
  `whiteboard_read` is gone — `whiteboard.summarize()` is **already injected
  into every agent's prompt**, so that call re-fetched what the agent held.
- **The triage gate was not binding**: `triage_recommendation` routes the
  pipeline and anything unrecognized becomes FULL (the expensive path). It was
  **missing in 90 of 337 runs (27%)** with nothing logged; SKIP has never
  fired; QUANT_ONLY fires 2% of the time. Now required + normalized.
- **It could never be wrong** (0-for-53 decisive) → new `catalyst_call`.

### Phase 3 — fundamental analyst (`343fb37`)

- **The obvious hypothesis was wrong.** 37% of runs make zero tool calls, but
  those runs score *better* (40% vs 29%). "Call more tools" would have been the
  wrong fix; the prompt is unchanged on that axis.
- **No horizon concept existed anywhere in V3** (`grep horizon` → nothing). A
  multi-quarter business view was consumed as a vote on a 7-day trade — by the
  debate, by the Buffett persona, by the contradiction shadow. BULLISH calls
  averaged a **-0.54%** realized move, BEARISH **+0.72%**, at 76-84 stated
  confidence. New `horizon` + `near_term_read`; the scorecard grades the
  horizon-matched claim and falls back for old artifacts.
- moat carried no number in 70% of reports, management in 38% → quantitative
  proxies (gross margin trend, ROIC, countable capital allocation).

### Phase 4 — quant analyst (`43a79fd`)

**The most serious correctness finding of the audit.** Tracing every RSI in 305
reports back to the text the agent was given: only 134 matched a number
anywhere on the desk. **171 did not, 148 of those from zero-tool runs.** IP
reported 58.0 against a desk value of 71.19; GOOGL 47.0 against 53.7. These set
`volatility_regime` and stop placement and the Board reads them as fact.

Root cause is a category error, not a weak prompt: those values already exist
in the `technicals` table. `app/quant/technical_baseline.py` computes them,
injects them (never shed), and reconciles the artifact afterwards, keeping the
model's originals so the rate stays measurable. **Correction is conditional** —
see Gotchas. The "MANDATORY" whiteboard `signals` write (9 of 56 runs) is now
posted from the artifact in code.

### Phase 6 — board / unactionable-decision waste (`90e7452`)

The board did **not** need the override constraint I had planned (see the
retraction above). The real finding was where its compute goes. Across 821
decisions / 202 agent-hours:

| decision | n | agent-hours | avg |
|---|---|---|---|
| HOLD unheld (screening "no") | 343 | 55.1 | 578s |
| BUY (actionable) | 182 | 53.3 | 1054s |
| **SELL unheld (CATEGORY ERROR)** | **167** | **57.7** | **1243s** |
| HOLD held (actionable) | 120 | 32.2 | 966s |
| SELL held (actionable) | 9 | 3.6 | 1443s |

The unactionable SELLs were the **most expensive decisions in the system and
the least useful** — 29% of all compute, every one blocked by the policy gate
at the very end.

**Root cause: the constraint was being shed from the prompt.**
`portfolio_context` carries "the bot cannot SELL what it does not hold (no
shorting)" and sat at `shed_order 2` — one of the FIRST sections dropped when a
prompt overflowed Prism's 2048-token embedder. Now `_KEEP`.

Three layers: never-shed constraint → `coerce_unshortable_sell` (unheld SELL →
HOLD/size-0, bearish reasoning retained) → no-trade-available gate (unheld +
both research desks BEARISH → skip the ~246s tournament, board still decides).
Kill switch `V3_NO_TRADE_GATE=false`.

### bot_id resolution — one bug, four silent failures (`a97929e`)

Investigating why `hrp_weight_suggestion` was null in 516/568 quant reports
found a root cause far wider than sizing. `run_v3_pipeline(bot_id: str = "")`
was never passed a bot_id by its only caller, so every desk stored `bot_id=''`
and the fallbacks resolved to `settings.BOT_ID` — a bot with **zero
positions** — while the active bot held 9.

| System | Was | Now |
|---|---|---|
| HRP sizing | **never ran** (needs ≥2 tickers, saw an empty book) | computes |
| `held` flag | **False for everything**, incl. genuinely-held TSM/JPM/ALLY | correct |
| Drawdown breaker | 0 rows → `None` → **could never trip** | returns -0.45% |
| Book brief | described an empty portfolio to quant + board | real 9 positions |

GARCH needs no bot_id and kept working, so the quant-math block looked healthy
— of 51 desks carrying one, **zero** had an HRP line. **The board was never
"ignoring" the covariance math; it was never given any.** The +0.24 correlation
reported earlier came from 16 rows of model-invented numbers.

`resolve_bot_id()` (explicit > active > settings) in `portfolio_tools` is now
the single resolver. Never reintroduce a bare `settings.BOT_ID` fallback.
`trading_tools.py` already resolved correctly and is unchanged.

---

## Open items — act on these next

- **RE-DERIVE THE PHASE 6 NUMBERS ONCE FRESH CYCLES LAND.** The 69%
  unactionable figure and the executability split are computed from the desk
  `held` flag, which was wrong for every desk before `a97929e`. The SELL
  direction of the finding holds (those were held=False and genuinely
  unshortable), but the counts will move. Do not quote 69% as settled.
- **Historical `held` is unreliable, and worse than merely wrong.** 16 tickers
  (AMZN, GOOGL, META, WFC, XOM, DIS, CRM, ASML) carry `held=true` on desks
  despite having **no BUY fill in any bot's history**, and agents were shown
  fabricated positions — "CURRENTLY HOLDING WFC: Entry $86.30, P&L +0.1%, Held
  10 days". Current code returns held=False for these, so it is a legacy
  artifact, not a live fault. Root cause never established; a purged bot
  profile is the leading theory. Anything mining desks before 2026-07-24 for
  position state must treat `held` as untrusted.
- **POSITION SIZING IS A HABIT, NOT A CALCULATION — highest-value open item.**
  (Now genuinely buildable: HRP produces real numbers for the first time.
  Confirm HRP lands in a live block before building the sizing frame on it.)
  Sizing is already agentic (agent proposes `position_size_pct`, code haircuts
  it in `resolve_buy_size_pct`), but across 181 BUYs the board used only **10
  distinct values**, and 80% were exactly 3%, 4% or 5%. Correlation with the
  quant's covariance-aware `hrp_weight_suggestion` is **+0.24** (n=16) — and
  mean HRP suggestion is **0.75%** against a mean board size of **4.41%**,
  ~6× larger. The portfolio math is computed and then ignored.
  - ~~**Check FIRST, do not assume**: most `hrp_weight_suggestion` values are
    0.0. Either the quant is not populating the field, or HRP genuinely says
    "too correlated with the book, do not add" and the board has been
    overriding it on every trade.~~ **ANSWERED 2026-07-25 — neither.** HRP was
    never *running* (the `bot_id` bug), not running and returning zero. 8
    post-fix desks carry real varying weights (JPM 0.123, VZ 0.192, LLY 0.099,
    TSM 0.069, AXP 0.063, NVDA 0.006). This item is unblocked; see the units
    trap in "What is live right now".
  - Proposed shape (same precompute-inject pattern that worked for GARCH/HRP
    and the Phase 4 technical baseline): compute the binding constraints in
    code and hand the agent a **bracket**, not a blank field — risk-based size
    (1% equity at the ATR stop), HRP correlation ceiling, cash available,
    concentration cap, and which one BINDS. Four lines, not a data dump. The
    agent's job becomes judgment within the bracket.
- **Phases 7-8 not started**: synthesizer, then the whole-cycle audit.
- **Tournament parallelization — verify these three BEFORE fanning out.** The
  246s is 9 sequential LLM calls, serialized because concurrent turns on one
  Prism agent-conversation return 409. The KV-cache guard already exists
  (`AdaptiveConcurrencyController` reads live vLLM `/metrics`;
  `ADAPTIVE_MIN_CONCURRENCY=8` when cache pressure >80%, max 24 under 60%).
  1. **Do the tournament's `llm.chat` calls actually route through that
     controller?** If they bypass it, fan-out is genuinely unsafe — this gates
     the whole approach.
  2. **Slots must be acquired globally, not via a private semaphore.** 6
     tickers × 4 pitches = 24 in flight while the controller believes it is
     idle.
  3. **Preserve prefix-cache reuse.** `V3_PROMPT_SPLIT` keeps system prompts
     byte-identical so vLLM reuses the prefix. Giving each persona its own
     identity must NOT give each its own system prefix, or KV pressure rises
     exactly when concurrency does.
- **`technicals` is stale for most tickers** — **measured 2026-07-25: only 5 of
  503 tickers (1%) are fresher than 3 days**, far worse than the GOOGL 7d / IP
  9d / NVDA 7d spot checks implied. The bulk sit at 8–10 days, the tail runs to
  months (VZ's RSI was 71 days stale and still fed a live BUY), while
  `price_history` is current for 515. Collector gap, not an agent one —
  deliberately not fixed inside an agent phase. Fixing it makes the Phase 4
  reconciliation authoritative far more often, and it is a bigger lever than
  the original wording suggested.
- **NEW 2026-07-25 — ~2% of desks never persist `final_decision`, though the
  decision was made and executed.** On AMD the board logged `final_decision`
  (HOLD @65) and the synthesizer appended `trade_decision` (2145 bytes, HOLD
  @78), and the `trade_results` row exists — but
  `shared_desk.desk_data->'final_decision'` is `null` and the desk's
  `updated_at` froze ~96s *before* the decision was produced. **9 of the last
  400 `trade_results` rows (2%)** show the same split back to 2026-07-06
  (GOOGL, MCD, QCOM, PAM, LLY, AMZN, AMP, TSLA, BAC) — pre-existing, not from
  this wave. **All 10 observed cases are HOLDs**, so every desk-derived count in
  this file drops them non-randomly. It is also the lone ❌ in the third
  verification cycle — a persistence fault, not an agent one. Reconcile
  `shared_desk` against `trade_results` before quoting desk-based numbers.
- **The board is where the damage is.** It overrides the debate on 23/53
  decisions at a 32% hit rate (-0.96% edge); overriding the fundamental
  analyst hits 20% (-2.14%). Three independent comparisons all point the same
  way: a 60%-accurate debate goes in, a 41.5% verdict comes out. Phase 6.
- ~~**The synthesizer never overrides the board directionally — 0 of 53.** 74s
  and ~34k tokens per ticker for a directional rubber stamp.~~ **RETRACTED
  2026-07-25 — an artifact of the 53-desk window.** At n=557 the synthesizer
  differs from the board on **108 (19%)**: 8 hard BUY↔SELL flips (AAPL, IP,
  BAC, FCF, F×2, C, CRM), 38 BUY→HOLD, 25 SELL→HOLD. Its overrides are also the
  only one of the four handoffs that pays (n=12, +5.52 edge, 88% hit vs
  agreeing n=74, −0.61, 48%) — small n, so a hypothesis to power, not a result.
  **Do not open Phase 7 assuming the layer is inert.**
- **Verify Phase 1 after the next cycle** (~04:15 UTC). Two checks:
  ```bash
  .venv/bin/python scripts/grade_regime_calls.py --since 2026-07-24
  ```
  and confirm one regime per cycle:
  ```sql
  SELECT cycle_id, COUNT(DISTINCT desk_data->'regime_classification'->>'regime')
  FROM shared_desk WHERE created_at > '2026-07-24' GROUP BY 1;  -- must all be 1
  ```
  `forward_call` needs 5 trading days before it grades; expect "pending" first.
- **`strategy_performance` is dead** and should be either fixed or dropped.
  `evaluate_pnl()` has **zero callers**, so nothing ever resolves (60/1979),
  and every V3 row is stamped `agent_prompt_hash="v3_pipeline"` — one bucket
  for the whole pipeline, so it could not attribute to an agent even if it ran.
  `decision_outcomes` is the live, working table; use that.
- **`market_regime` table is a dead placeholder** — `vix_zscore` 0.0,
  `breadth_sp500` 50.0, yields NaN, `regime_label` 'Neutral' on all 78 rows.
  Do not wire it into anything. `macro_trend.py` reads `asset_prices` instead.
- **SPY/QQQ/sector ETFs in `price_history` stop at 07-17** while 515 other
  tickers are current — index/ETF symbols are not in the daily refresh set.
  `macro_trend.py` sidesteps this by using `asset_prices` (fresh to 07-24), but
  anything else reading index history from `price_history` is a week stale.

---

## Gotchas

- **`quality_score` is not accuracy.** It grades artifact shape. An agent can
  score 82 and be wrong more than half the time — the quant analyst does
  exactly that. Judge changes with the scorecard, not this.
- **`_score_data_completeness` buckets on a ratio**, so adding a 5th optional
  field leaves 4-of-5 and 5-of-5 in the same ≥0.8 bucket. `forward_call` is
  deliberately NOT in `_OPTIONAL_FIELDS` — adding it looks like enforcement
  while changing nothing, and re-cutting the buckets would move the quality
  baseline for every artifact type mid-audit.
- **Holding the regime lock across the LLM call is intentional.** Tickers 2..N
  wait for the first answer rather than computing rivals. Wall clock is
  unchanged for one wave (they used to spend that time in parallel anyway) and
  strictly better for watchlists larger than the concurrency cap.
- **A reused regime still records a telemetry row** (`reused: True`,
  `elapsed_ms: 0`). Without it the ticker's regime node vanishes from the
  replay flow graph and the regime→analyst edges break — the same "islands"
  bug the tournament had.
- **`asset_prices` carries NaN** for symbols a vendor returned empty. NaN
  compares false everywhere and survives a `NOT NULL` check; `macro_trend._finite`
  and the grader both filter it. The `market_regime` table is what happens
  when you don't.
- **n=53 on the baseline.** Every CI overlaps 50%. No single cell is
  significant — what is persuasive is the override pattern repeating across
  three different upstream agents.
- **Quant reconciliation is conditional on purpose.** `technicals` lags for
  most tickers, so overwriting a value the agent genuinely fetched live with a
  week-old stored one is its own regression. Fresh → correct. Stale + agent
  used NO tools → correct anyway (it had no source) and flag the gap. Stale +
  agent used tools → do NOT overwrite, record the discrepancy under
  `_unreconciled_metrics`. Anyone "simplifying" this to an unconditional
  overwrite will reintroduce a different wrong number.
- **Sanitize NaN where values are CONSUMED, not only where they are fetched.**
  A test caught this in `technical_baseline`: NaN survives a `NOT NULL` check
  and compares false against every threshold, so one arriving by any other path
  lands in `risk_metrics` looking like real data.
- **The `catalyst_call` / `near_term_read` / `forward_call` fields are all
  graded on exact enums.** Their validators normalize case and drop echoed
  schema literals ("BULLISH|BEARISH|NEUTRAL"). An un-normalized value scores as
  a permanent miss rather than an error, which is invisible.

## Where the reasoning lives

Per-agent measured starting state (latency, loops vs budget, tool-call
reality, whiteboard participation) is in the session task list, phases 2-8.
`AGENTS.md` remains the harness-level source of truth for budgets and limits —
note its §14 "Inactive Agents" and the Layer-3 debate description are now
partly stale: the tournament, not bull/bear, is the live debate path.
