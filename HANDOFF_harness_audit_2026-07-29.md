# HANDOFF — systematic cycle audit: what is actually broken (2026-07-29)

**Merged to `master`. Nothing pushed or deployed.** Suite: 2,100 passed, 19
skipped, 1 failed (`test_whitelists_grant_write_to_pm_and_board_only` — the
long-standing failure, unchanged).

---

## The one-paragraph version

The premise under test was *"the harness is over-engineered; filters conflict
and make behaviour worse than not having them."* Measured stage by stage across
the whole cycle, **that is not what is wrong.** The gates barely fire, and the
decisions the system produces are directionally **correct** at every horizon
tested. What is actually broken is that large parts of the system are
**invisible** (computed and written nowhere) or **run on nothing** (analysed
with no underlying data). Three such holes are fixed here. The "zero BUYs"
problem is *not* a malfunction to be fixed — the evidence says the bot is
declining to buy things that then fall.

---

## Part 1 — What the measurements say

### The gates are not the constraint

Full funnel since 2026-07-24 (the window where `policy_action` is fully
deployed), n=133:

| outcome | n | mean conf |
|---|---:|---:|
| `HOLD_NO_SIGNAL` — *the board itself chose HOLD* | **110** | 63.9 |
| `HOLD_POLICY_BLOCKED_LOW_CONFIDENCE` | 8 | 59.2 |
| `HOLD_NO_POSITION` | 5 | 59.4 |
| `EXECUTE_BUY` | 6 | 72.0 |
| `EXECUTE_SELL` | 4 | 74.3 |

Ten gates → **13 blocks in three weeks**. Whole-history `v3_guardrail_firings`
is 22 rows. **18 of 45** HOLDs on 07-28/29 were *above* the floor — nothing
gated them; provenance is `board_reasoned`.

### The decisions discriminate — this is the important finding

| horizon | HOLD | BUY | SELL |
|---|---|---|---|
| 1d | n=91 **+0.51%** | n=40 **+1.46%** | n=25 +1.24% |
| 3d | n=50 **+0.52%** | n=30 **+0.76%** | n=14 −0.59% |
| 5d | n=24 **−2.30%** | n=16 **+0.88%** | n=6 **−9.03%** |

BUY > HOLD at every horizon; SELL isolates the worst names at 3d/5d. The
tickers the board declined genuinely fell.

### The tournament drives the board, and it has real skill

Bear-win → board HOLD **74%**; bull-win → HOLD **30%** (Fisher **OR=6.5,
p=3.2e-09**). It is the strongest predictor of board action found.

And it is *right*: bear-flagged tickers returned **−2.85%** vs bull **−0.49%**
over ~5 sessions; up-rate 34% vs 56% (Mann-Whitney **p=0.0072**; up-rate
**OR=2.44, p=0.0056**).

**This contradicts the previous handoff's "delete the tournament" conclusion.**
That verdict came from a Brier score of 0.3090 vs 0.2266 base rate. Both can be
true — it can be *badly calibrated* (bad probabilities) while being
*directionally discriminating* (good ranking). Brier punishes the former; P&L
rewards the latter. **Do not delete the tournament on the Brier number alone.**
Re-score it as a ranker before making that call.

### Three hypotheses tested and REFUTED — do not re-run

1. **Price staleness causes the HOLD ramp** — **all 45** of the 07-28+ all-HOLD
   decisions had fresh prices.
2. **`risk_flags` count drives HOLD** — within-day, controlling for date, the
   HOLD-vs-other difference is **+0.19 flags** (8/10 days). Far too small.
3. **`winning_side` is mis-derived from the legacy seat fallback** — the mapping
   agrees with the winning thesis direction **228/228**.

### The "price collapse" was a misreading — mine

`price_history` distinct tickers fell 2,642 → 509 on 07-20. That is **not** a
collector failure: **509 is exactly the S&P-500 daily refresh set**, and 2,642
was a one-off backfill (`price_backfill_progress`, all rows stamped 07-20).
Working as designed.

The **real** defect it exposed: the analysed universe is not a subset of the
refreshed one — **36 of 82** analysed tickers sit outside it.

---

## Part 2 — The three fixes (shipped)

### 1. `tournament.py` — the survivor projection dropped `direction`

Live dicts carry it; only the *persisted* copy lost it, so every stored survivor
read back as `direction="?"` (**506/506**) and the bull/bear skew could only be
recovered by regexing the winner's persona out of free text. `filter_bypassed`
(advanced **without** clearing the backtest gate) was invisible entirely — a
bypassed bracket looked identical to a backtested one. Added `backtest_verified`
so "unbacktestable" stays distinct from "backtested flat".

### 2. `orchestrator.py` — a blind UPDATE that silently matched zero rows

```
tier=v3_deep    analysed=136  trade_row=131  policy=131
tier=v3_delta   analysed= 15  trade_row=  0  policy=  0
tier=v3_glance  analysed=  5  trade_row=  0  policy=  0
```

`save_trade_result` is the only INSERT and is gated on
`has_artifact("trade_decision")`, which delta/glance never publish. ~13% of
analysed tickers computed an enforced label written **nowhere**.

Now **logged, not repaired at that site** — inserting a synthetic row there would
invent a trade record for a path that made no trade decision, and a Triage-Gate
skip is a legitimate non-decision, not a degrade. Enforcement was never affected
(`pipeline_service` reads the label off the in-memory result).

**Trap for the next agent:** the rowcount must be read off `cur._cursor`.
`PooledCursor` returns `self` from `execute()` and defines neither `rowcount`
nor `__getattr__`, so the obvious `getattr(cur, "rowcount", -1)` always yields
−1 and the check never fires. Verified live: miss → 0, hit → 45, naive → −1.

### 3. `pipeline_service.py` — a price-history pre-flight

**2026-07-26: LUCK ran the full panel to `PM_DONE` and emitted a decision at
confidence 48 with ZERO `price_history` rows.** MSBT reached INIT with zero
rows. `has_price_history` existed but was wired **only** as the last policy
gate — so ~200s and ~220k tokens were spent before rejection, and every
technical claim in between rested on nothing.

Same probe, moved to the front. The gate stays as the backstop (mid-cycle loss,
and paths that skip this loop). Fails open, matching the gate's contract.

---

## Part 3 — What is still open

### The system-dynamics reading (the user's question)

Yes, and it names the real pathology precisely. The failures here are not bad
components — they are **broken feedback loops**:

- **Measurement that doesn't close the loop.** Delta/glance computed a label and
  wrote it nowhere; survivors lost `direction`; six gates have never fired. You
  cannot regulate what you cannot observe, and each hole makes the *next*
  diagnosis harder. This is the dominant failure mode in this codebase.
- **A stock-vs-flow confusion.** 2,642 tickers was a *stock* (one-off backfill);
  509 is the *flow* (daily refresh). Reading the stock as the flow produced a
  phantom "collapse" — I made exactly this error mid-audit and had to retract it.
- **Delay-driven overcorrection.** Resolution lag is ~45 days, so every
  correction lands on stale evidence. The 07-25→28 commit burst each added a
  caution; HOLD went 58% → 100%. Whether that is overshoot or correct caution
  is *not yet answerable*, which is itself the point.

The actionable version: **stop adding controls until the sensors work.** These
three fixes are sensor repairs, deliberately.

### Ordered next steps

1. **Do not "fix" the zero-BUY state yet.** The evidence says the bot is
   declining things that fall. Verify on resolved outcomes in ~2 weeks before
   touching the floor (calibrated: conf ≥70 → +2.54% on n=1464; <70 → −4.07% on
   n=605).
2. **Re-score the tournament as a ranker, not a forecaster.** It has p=0.007
   directional skill and a bad Brier. Deleting it on the Brier alone would throw
   away the strongest signal measured in this audit.
3. **Make the analysed universe a subset of the refreshed set** — 36 of 82 are
   outside it. Either widen the refresh or restrict selection.
4. **Gate ablation harness** (designed, not built). Replay is proven viable:
   `shared_desk` round-trips, and calling the *real* `_apply_policy_gates`
   reproduces stored labels **131/133 = 98.5%**, both mismatches explained by
   the 65→70 floor move. n=1,311 desks. Design notes in
   `~/.claude/plans/quiet-kindling-breeze.md`.
5. **74 of 195 tables (38%) are dead** — 37 empty, 37 with no write in 14 days.
   Includes `runtime_parameters` (empty, so the floor is a code default not
   recoverable from the DB) and `data_flags`.

### Standing caveats

- **n is small.** 133 fully-instrumented decisions, 10 executions. The
  horizon-5d cuts are n=24/16/6. Directional, not conclusive.
- **Power:** detecting a 1pp gate effect at 80% power needs ~500–800 changed
  decisions per gate — months for most, **never** for `JURY_VETO` at n=1.
- The system still trails always-long by ~1%; nothing here claims to change that.

---

## Method note

Three of my own hypotheses were refuted by data mid-audit (price staleness,
risk-flag density, the seat-fallback theory), and I misread the backfill stock
as a collector flow before catching it. The `rowcount` fix would have been a
silent no-op had I not tested it against the live DB rather than reading the
wrapper's source. Read the commit body — it carries the measurements.
