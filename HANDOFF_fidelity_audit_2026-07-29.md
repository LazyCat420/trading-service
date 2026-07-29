# HANDOFF — agent fidelity, accounting, and tool-selection audit (2026-07-29)

**Live on synology at `master@0ddd747`.** Container healthy. 1857 unit tests
pass; the single failure (`test_parameter_tools::test_whitelists_grant_write_to_pm_and_board_only`)
is long-standing and verified identical on a stashed clean tree.

19 commits. Everything below was verified by running a real cycle in the
deployed container, not by reading code — see "Method" at the end for why that
distinction earned its place.

---

## Measured result across 8 verification cycles

```
cycle              tools  calls   positioning  quality
C1 baseline            8     37       0/5        71.3
C2 alt-data           11     40       0/5        70.8
C3 prompt-fix         11     64       0/5        73.1
C4 positioning        10     54       4/4        71.0
C5 stale+guard        11     98       5/5        74.1
C6 FRESH tickers      11     71       5/5        73.4   <- never tuned against
C7 post-removals      12     57       4/4          —
C8 whiteboard fix     11     —        —            —
```

C6 is the regression test that matters: MSFT/JNJ/XOM/BAC/PFE, tickers never
used during development. The fixes generalise.

---

## What was broken, and what fixed it

### Fabrication (agents inventing numbers)

| Defect | Evidence | Fix |
|---|---|---|
| Fundamental desk emitted **zero** numeric fields across 163 artifacts | 4 of 7 stated P/Es wrong; CARS 4.83 vs 27.99 (its *forward* P/E) | `app/quant/fundamental_block.py` — 23 verified fields + reconcile |
| `max_drawdown_est` was the prompt's placeholder | `12.5` recurred 15x across different tickers | computed from stored returns |
| Alt-data injected but never cited | block reached 6 agents, cited by **0** | `positioning_read` required field + reconcile → **0/5 → 5/5** |

**The pattern that works is three things, never one: precomputed block +
REQUIRED schema field + reconcile pass.** Injection alone was measured
insufficient (C2). This is the single most transferable finding here.

### Accounting (the arithmetic was right; the inputs were not)

Universe test over 286 tickers carrying both our EV/EBIT and an independent
vendor EV/EBITDA:

```
before   IMPOSSIBLE 11 (3.8%)   DISTORTED 21 (7.3%)
after    IMPOSSIBLE  0          DISTORTED  0     (239 emitted, 47 withheld)
```

- **One loss quarter poisons the TTM sum.** GM: `1459+2926+(-3647)+1076 = 1814`
  → EV/EBIT 103.4x, which passed a `> 0` gate clean and fed "36.7% implied NOPAT
  growth" straight into an override rationale. 34% of tickers carry a negative
  EBIT quarter.
- **Currency mismatch** — TSM: USD market cap over TWD filings.
- **The vendor cross-check is now an assertion**, not a one-off hand check on
  two mega-caps. `ratio < 1.0` is structurally impossible.
- All three EBIT-derived ratios fail together (multiple, reverse DCF,
  net-debt/EBIT).

### Order safety

`stop_loss` / `take_profit` / `position_size_pct` are emitted by BOTH the Board
and the synthesizer, and **nothing validated any of them**. 3 of 358 decisions
carried a decimal error: **LMT stop $0.92, target $1.25 against a $581.33
close**; ALLY $11.04 vs $43.97. Now dropped (not clamped) and recorded as
`DROPPED_IMPLAUSIBLE_LEVEL`.

### Tool selection — why only ~10 of 54 tools ever fired

Two causes, both structural, neither a whitelist problem:

1. **Prompts hardcoded the opening.** `get_finnhub_news` +
   `get_institutional_holdings` + `whiteboard_write` fired on **7 of 7**
   tickers; the fundamental analyst used an identical six-tool set every time.
   The fixed core ate 50–75% of a 7-turn budget, so a new tool had no slot.
   After the fix: **tools firing on every ticker 3 → 1**, and
   `request_peer_analysis` fired for the first time in 30 days.
2. **Hedged framing reads as "skip."** Same prompt, same agent, 30 days:
   `get_institutional_holdings` "(are top funds adding?)" → **310 calls**;
   `get_reddit_trending_stocks` "only if retail buzz is plausibly a factor" →
   **0 calls**.

### The whiteboard stub (found by the user, from a screenshot)

`{'confidence': 65}` was being written into the quant's `signals` section —
53 of 326 writes (16%), and the quant was the **only** agent doing it.

Root cause was **not** the quant. `_persist_quant_chart` and
`_persist_quant_signals` sat outside any `agent_name` guard and ran after
*every* agent; the VALUATION analyst's artifact (no `risk_metrics`) collapsed to
bare `confidence` and got posted under the quant's name. Container logs settled
it in seconds after four wrong static-reading diagnoses.

### Evolution loop

CORAL had been dead since 07-27: a killed runner left `status='running'` and
`claim_next_job` only looked at `queued`, so the job was invisible forever. Now
reclaims stale claims after 3h. **Confirmed alive** — it ran a full graded cycle
(attempts 12 → 16) and correctly refused to push, capping at 0.25 because its
own generated repro test passes on unmodified code.

---

## Removals

| Removed | Evidence |
|---|---|
| Tournament debate → **shadow mode** (default OFF) | 239k tokens + 191s/ticker = **31% of all pipeline spend**; predictive power over P&L `t = -0.17` |
| 7 superseded tool grants | each returns what a precomputed block already carries, reconciled |
| `pending_evolution_fixes` → archived | 96 rows, last deploy 2026-06-01, replaced by CORAL |

**The tournament debate is NOT deleted.** It feeds a veto that fired 12 times
in 14 days. Shadow keeps the artifact and the veto and stops only the verdict
reaching the Board, stamping `shadow_mode` so the experiment is falsifiable.

---

## What I REFUSED to ship

**The `consensus_strength` floor.** I recommended it twice as "the cheapest
real win", then tested the threshold: blocks 12 trades at −1.33%, keeps 44 at
+0.21%, **t = 1.17**. Not significant at n=56. The correlation is real (+0.248
vs +0.048 for `confidence`) but the threshold effect is inside the noise.
Revisit at n≈150.

---

## Corrections to my own claims (all population mismatches)

1. **"53 tools used, not 10"** — 14-day window vs the user's 1-hour dashboard.
   Per cycle it is 8–14. The user was right.
2. **"51% veto rate"** — a 7-day n=41 slice. Over 14 days (n=333) it is 24%,
   with 88% Board/synthesizer agreement.
3. **"The autoresearch expectancy is a sign error"** — WRONG, retracted. Its
   window holds only 2 DEGRADED_ARTIFACT rows and they land in neither wins nor
   losses. Its numbers are correct; expectancy there is +0.964%.
4. **"Every GARCH vol disagrees"** — my regex captured *realized* not
   *predicted*. Actually 127/127 exact match.
5. **"The debate signal is inverted"** — read off a small slice. On the full
   population bull-won → 65% BUY, bear-won → 21% BUY. Correctly wired.

---

## Still open (ranked)

1. **The Board holds 64% of the time** (312 HOLD / 134 BUY / 38 SELL). Biggest
   remaining lever on trade flow, entirely unexamined. I spent this session on
   the synthesizer's 24% veto — the smaller effect.
2. **Flip `TOURNAMENT_DEBATE_MODE=1`** and let it run ~2 weeks. The 31% saving
   is *available*, not *taken*. Nothing is measured until you flip it.
3. **`pending_review.py` is byte-identical in trading-client**, and the UI
   calls the CLIENT copy. Only the service copy was retired, so the dashboard
   still shows those rows as pending.
4. **CORAL cannot certify a fix** — its repro generator produces tests that
   pass on unmodified code. That is the next real target for the loop.
5. **Inference staleness beyond positioning.** `positioning_read` now flags a
   stance built on corrected counts; the quant regime read, valuation verdict
   and fundamental thesis all sit on reconciled numbers and none flag it.
6. **`v3_bull_defense` has an empty whitelist**, which grants the FULL catalog.
   Pre-existing, verified on a clean tree.
7. `consensus_strength` floor at n≈150.

## Do NOT change

- **Confidence floor 70** — calibrated on real evidence (conf <70: n=130, mean
  −1.91%; ≥70: n=698, mean +3.76%), even though it is the weakest of the four
  conviction scores.
- **The quant prompt** — highest quality of the four research desks (82.2) at
  the FEWEST loops (1.08), because its numbers are already in the prompt.
  Fewer calls is the invented-RSI fix working, not a bug. Pinned by a test.
- **The delta/glance triage tiers** — 26% of desks skip full research and that
  saves real money. Surface the tier on the decision instead.

---

## New tooling

```
scripts/agent_fidelity_audit.py      per-agent numeric fidelity; UNGUARDED fields
scripts/verify_fidelity_fixes.py     acceptance harness (7/7); FAILS 3/7 on the
                                     pre-fix cycle — validated as a negative control
scripts/cycle_healthcheck.py         the 7-phase checklist, executable, in triage
                                     order (18 pre-cycle / 24 post-cycle, 0 FAIL)
.claude/hooks/                       block deploys during a live cycle;
                                     warn on leaked idle-in-transaction sessions
```

## Method — the one thing worth carrying forward

**Every finding in this document came from running something, not from reading
code.** Four times I diagnosed the whiteboard stub by reading, confidently, and
was wrong each time; one grep of the container logs settled it. The hooks
themselves no-opped silently through four "passing" tests because `jq` is not
installed on this box — only `bash -x` exposed it.

State the window and the population with every number. Three of my five
retractions above were the same error: comparing populations without matching
them.
