# Session handoff — the quant-layer review that became a measurement repair

**2026-07-29 → 07-30.** Started as "add a course-derived quantitative reasoning
layer (MIT 18.S096, 25 lectures)". Ended up fixing the instruments instead,
because the measurement said the capability was not the binding constraint.

Everything below was measured against the live DB or a live cycle. Where a claim
is unproven I say so.

---

## The one-paragraph version

Ranking 25 lecture topics against what this repo can actually support left **3
worth building** — 10 are hard-blocked by data that does not exist. But
measuring for that ranking turned up **five real bugs corrupting numbers the
Board already reads**, a **30%-of-spend component that could not beat a free
signal**, and a **measurement ceiling that makes most new capability
unfalsifiable**. Those got fixed instead. No decision logic was changed on
evidence I could not defend.

---

## Shipped and verified in production

| # | Change | Verification |
|---|---|---|
| 1 | **One vendor per price window** in the return loaders | CRH vol 25.18% → 32.44%; DRIP 2,660% → 232% |
| 2 | **Date-joined `book_brief` correlation** | ASML now warns **TSM +0.71** (was "ALLY +0.30, diversification available") |
| 3 | **Stale-conclusion flags** on 3 reconcilers + desk render | fired on NVDA (`fundamental=true`) |
| 4 | **One vendor across the evaluation layer** (`latest_close`, `forward_window`, `forward_move_pct`) | 108/773 scored rows changed, 35 sign flips |
| 5 | **Tournament retired** (`DEBATE_ENGINE=3`) | `SKIPPED`/0 tokens |
| 6 | **Board dispatch restored** after I broke it | NVDA `PM_DONE` with a decision |
| 7 | **bull/bear/judge restored** under engine 3 | 9-ticker production cycle, all `PM_DONE`, 1078–1212 char arguments |
| 8 | **Statistics in the equation sandbox** | `full_gate`, `deflated_sharpe_ratio`, `norm_cdf` executing in-container |
| 9 | **Arg repair moved to the live tool path** | probe: `injected NVDA` → call succeeded, repair recorded |

Commits: `ecfe530`, `6448af0`, `78e0433`, `9904dfb`, `45a46ca`, `21eb7ee`,
`52c16b7`, `9f551fa`, `00a0691` (+ `44eeb83` in lazy-agent-service).

---

## The three findings that should shape what you do next

### 1. The binding constraint is measurement, not capability

```
fills in the book's entire history .......... 45
scored decisions that touched the book ...... <=5.5%
completed desks span ........................ 5 weeks (06-24 .. 07-27)
independent windows at a 10-session horizon .. ~4
minimum detectable effect ................... 2.24pp  (sd 5.0pp, n=157)
```

Any new selection signal must move P&L by **>2.24pp** to be detectable at all.
VaR/Kalman/ADF will not, so they could not be validated even if they worked.
**This is why I did not build Wave 2.**

I proved this the hard way: my first HOLD-vs-BUY analysis produced
"−2.26pp, p=0.0032". It was an artifact of a variable-horizon bug plus
uncontrolled market drift, on ~2 independent windows. `agent_scorecard.py` now
prints an `INDEPENDENCE` line that says so out loud.

### 2. The tournament lost to a signal already on the desk

n=137 desks, degraded excluded:

| channel | tournament | free `quant.thesis_direction` |
|---|---:|---:|
| selection (traded desks) | −0.822pp (p=0.34) | **−0.771pp (p=0.35)** |
| removal (held desks) | −0.29% (n=29) | **−1.85% (n=14)** — 6.5× better |
| incremental within quant dir | **+0.33pp (p=0.84)** | — |
| redundancy vs quant | **chi²=16.63, p<0.0001** | — |

Brier 0.3090 vs a 0.2266 base rate; the jury veto has blocked **zero** decisions
ever. The often-cited "directionally discriminating at p=3.2e-09" measures
`winning_side` against the **Board's action** — that the Board *listens*, not
that it is *right*. That is the redundancy channel.

**Honest limit:** at n=137 this shows *no measurable benefit against a certain,
large cost*. It is not proof of harm. `DEBATE_ENGINE=0` restores it.

### 3. `price_history` mixes vendor conventions — assume every reader is wrong until checked

`source` is in the PK. 9,225 dual-source ticker-dates across 38 tickers, mean
absolute close difference **20.05%** (adjustment convention: yfinance adjusted,
polygon raw). Both failure directions are real:

* vendors CLOSE → same-date pairs dilute variance (CRH 25.18% vs 32.44%)
* vendors FAR → alternating conventions manufacture jumps (DRIP 2,660%, 133 fake >15% moves)

Fixed in `app/quant/returns.py` (`_keep_dominant_source`, `_dominant_source_sql`)
and routed `outcome_tracker` + `agent_scorecard` through it. **Still unaudited:**
`scripts/{confidence_audit,gate_ablation,score_tournament_ranker,score_panel,
residual_alpha_report,factor_backtest}.py` and `app/trading/scoring_engine.py`
all read `price_history` with no `source` filter. Use `forward_move_pct` /
`latest_close`.

⚠ A correct fix here is a **no-op on 2,726 of 2,764 tickers**. "The numbers
didn't change" is not evidence it worked — verify on CRH, ALLY, ASML, DRIP.

---

## Open checklist

### Live bugs
- [ ] `v3_fundamental_analyst` **`AGENT_ERROR`** — one run burned 164,625 tokens
      in `cycle-observe-1785399229`. Undiagnosed.
- [ ] `read_url` **72% failure** (13/18, 30d)
- [ ] `lazy_web_search` **16% failure** (56/350)
- [ ] `get_sec_filings` SEC EDGAR **404s** (17) — separate from the malformed-args
      half, which is now fixed
- [ ] **69 desks stalled** mid-pipeline all-time (45 `RESEARCH_DONE`, 24 `DEBATE_DONE`)
- [ ] 13 desks reached `PM_DONE` with **no decision** (1.7%)
- [ ] 21 telemetry rows with agent name `?`

### Infrastructure
- [ ] **`dgx_spark` (10.0.0.141:8000) is DOWN.** It is the *active* endpoint, so
      every boot burns 3 min failing readiness 36× before `boot_service.py:318`
      swallows it. Running at ~half LLM concurrency.
- [ ] `deploy.sh` can die silently at "Restarting container", leaving **duplicate
      `trading-service_default` networks** and a container stuck in `Created`.
      Cost ~4 min of downtime this session. Recovery: remove the duplicate
      network (verify 0 containers attached), `docker compose up -d`.

### Data gaps (hard constraints)
- [ ] **No options table at all** → Black-Scholes, greeks, IV, Ross recovery are unbuildable
- [ ] `fundamentals` is **not point-in-time** (81 snapshots since 2026-05-06) → look-ahead until ~2028
- [ ] `free_cash_flow` non-null on **1 of 5,487** rows
- [ ] 3 rate tenors only → no forward curve (HJM is rank-deficient, not sparse)
- [ ] `price_history` survivorship-biased; only 614 tickers fresh with ≥2y

### Measurement
- [ ] 362 `DEGRADED_ARTIFACT` rows scored as trades — **hypothetical, 0 fills**.
      `agent_scorecard.py` guards this via `--executable-only`; raw
      `decision_outcomes` aggregates do not.
- [ ] Confidence carries **no ordering above 70** (70-79 +2.49%, 80+ +2.60%)

---

## If you build the quant layer anyway — the ranking

**Build (new, executable, cheap):**
1. **VaR / ES / Cornish-Fisher** — 4.5 ms, self-validating via Kupiec
   walk-forward (AAPL 3.90% exceptions vs 5% expected, LR 2.72, p=0.099).
   Justify as a **risk control feeding `sizing_bracket.py`**, not as alpha —
   excess kurtosis 4.65 means Gaussian understates tails. A sizing constraint
   does not need to clear a P&L significance test.
2. **Kalman time-varying beta** — 1.4 ms; `factors.py:market_beta` is a static
   full-window OLS.
3. **`adf_tau()` in `stat_gates.py`** — as a *gate* refusing claims regressed on
   price levels.

**Do NOT build:** ARIMA on returns (measured ACF ≈ 0 — all structure is in the
second moment, where GARCH already is), cointegration (none found in three
textbook pairs, *and* the short leg is forbidden), and anything needing options
or a forward curve.

**Ship it through `context_block.py`** (precompute → inject → reconcile), not as
a tool. Prompt real estate is the scarce resource: `_EMBED_CHAR_BUDGET ≈ 4,944`
chars with 10 `_KEEP` sections resident. Budget ≤1 line and displace something.

**The exception:** tools DO get used when the input is the agent's own choice —
`execute_python` 27/27 across 4 agents, `run_equation` 18. That is why the
sandbox statistics were worth shipping. My earlier blanket "tools go uncalled"
was an average masquerading as a rule.

---

## Traps that bit me — please don't repeat them

1. **A write can also be a TRIGGER.** I verified six consumers tolerated a
   missing `tournament_result` and never asked what the write *caused*. It
   chains the Board. NVDA ran 7 agents and decided nothing; 2,050 unit tests and
   a healthy container all passed. Only a real cycle caught it.
2. **`description=` in `@registry.register` is INERT.** `tool_schemas.json` (a
   gitignored build artifact from `lazy-agent-service/tool_schemas/<owner>/`) is
   loaded over it. Editing the Python literal changes nothing the model sees.
3. **Two tool dispatch paths.** V3 agents run in prism → tools return over HTTP
   to `POST /agent-tools/execute`. A hook on the local `AgentHarness` never fires
   for them. That is how a correct, well-tested arg-repair sat inert for a day.
4. **Tests that match prose.** Two of mine grepped explanatory comments instead
   of code and passed while testing nothing. Assert on the live registry / on
   code with comments stripped.
5. **A test that always skips proves nothing.** Stub the dependency instead.
6. **Verify a deploy with `docker ps`, not the deploy log.** A missing final
   success line is a FAILED deploy, not a truncated one.

---

## State at handoff

```
master ............. 7ec0913, clean, in sync with origin
container .......... trading-service healthy, restarts=0
last cycle ......... cycle-v3-1785418200 (9 tickers, scheduler) — done, all PM_DONE
DEBATE_ENGINE ...... 3  (bull/bear debate, no tournament)
tests .............. 2,083 pass; 1 pre-existing failure (test_parameter_tools,
                     VLLM-dependent) reproduces on master
worktrees .......... only fidelity-followup (another session's, locked)
```

**Unproven and honestly so:** whether the tournament retirement saved tokens
(per-ticker cost is dominated by ticker news volume — a 2-ticker cycle cannot
measure it), and whether any of this improved *decisions*. That last one needs
months of fills, not more code.

Full topic ranking and the original plan critique:
`~/.claude/plans/please-look-at-this-quiet-nebula.md`.
Companion handoffs: `HANDOFF_return_series_integrity_2026-07-29.md`,
`HANDOFF_tournament_retired_2026-07-29.md`.
