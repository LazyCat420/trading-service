# HANDOFF — the fix wave: sensors repaired, gate hypothesis retired (2026-07-29)

Follows `HANDOFF_harness_audit_2026-07-29.md`. That one diagnosed; this one
fixes. **Six defects closed, two measurement instruments built, one deletion
prevented.**

---

## The one-paragraph version

The brief was "the harness is over-engineered, filters conflict, clean it up."
Measured end to end, the filters are **not** the problem — the gates fire ~13
times in three weeks and the decisions they produce are directionally
**correct** at every horizon. The actual problem is a class of defect:
**the system could not see itself.** Paths computed values written nowhere,
artifacts lost fields on persistence, a full panel ran on tickers with no data,
and the set the bot analysed was not the set it kept fresh. All six are now
closed, and the two instruments needed to keep them closed are built.

---

## What shipped

### 1. `tournament.py` — survivors lost `direction` on persistence

Live dicts carried it; the persisted projection dropped it, so **506/506**
stored survivors read back as `direction="?"`. The bull/bear skew — the
strongest predictor of board action in the system — could only be recovered by
regexing the winner's persona out of free text. `filter_bypassed` (advanced
**without** clearing the backtest gate) was invisible entirely, so a bypassed
bracket looked identical to a backtested one. Added `backtest_verified` so
"unbacktestable" stays distinct from "backtested flat".

### 2. `orchestrator.py` — a blind UPDATE matching zero rows, silently

```
tier=v3_deep    analysed=136  trade_row=131  policy=131
tier=v3_delta   analysed= 15  trade_row=  0  policy=  0
tier=v3_glance  analysed=  5  trade_row=  0  policy=  0
```

~13% of analysed tickers computed an enforced policy label written **nowhere**.
Now logged, not repaired at that site — inserting a synthetic row there would
invent a trade record for a path that made no trade decision, and a Triage-Gate
skip is a legitimate non-decision, not a degrade.

> **Trap:** the rowcount must be read off `cur._cursor`. `PooledCursor` returns
> `self` from `execute()` and defines neither `rowcount` nor `__getattr__`, so
> the obvious `getattr(cur, "rowcount", -1)` always yields −1 and the check
> **never fires**. I nearly shipped exactly that. Verified live: miss → 0,
> hit → 45, naive → −1.

### 3. `pipeline_service.py` — a full panel on tickers with no data

**2026-07-26: LUCK ran the complete panel to `PM_DONE` and emitted a decision at
confidence 48 with ZERO `price_history` rows.** MSBT reached INIT the same way.
`has_price_history` existed but was wired **only** as the last policy gate — so
~200s and ~220k tokens were spent before rejection, and every technical claim in
between rested on nothing. Same probe, moved to the front; the gate stays as the
backstop. Fails open, matching the gate's contract.

### 4. `sp500_price_collector.py` / `boot_service.py` — refresh what we analyse

The daily loop refreshed **sp500 only**, but the cycle analyses the
**watchlist**: **127 of 199** watchlist tickers are outside the S&P 500, 60 with
prices older than 7 days. Now `sp500 UNION watchlist UNION positions` — 636
tickers, 7 chunks instead of 6. Positions are unconditional (marking a holding
to market is not optional). The `today_count < 400` top-up threshold was a
hardcoded literal against an sp500-only universe; a stale run covering only the
index would clear 400 and look healthy. Now derived from the actual set at 75%.

### 5. `scripts/gate_ablation.py` (new) — makes the gate question answerable

Replays the **real** `_apply_policy_gates` over persisted desks with one gate's
predicate falsified at a time. Never a reimplementation — that would measure the
copy, and the copy can drift.

```
FIDELITY: 148/149 = 99.3%
```

The era-pinned floor earned the last 0.8%: `_apply_policy_gates` imports
`get_param` **inside the function body**, so patching the orchestrator attribute
never intercepts it — patch `parameter_store` instead. `runtime_parameters` is
empty **by design** (falls through to registry defaults), so the historical 65
floor is only recoverable from git.

**It immediately found the effect it was built to catch:**

```
HOLD_NO_POSITION   fired=168  changed_action=168  scored=136
    blocked_good n=62  mean -3.29%   (stopped losers)
    blocked_bad  n=74  mean +4.42%   (cost winners)
    net +0.91%   CI [-0.37, 2.46]  p=0.19  holm=False
    exposed to: EXECUTE_SELL 145, LOW_CONFIDENCE 18, MISSING_REGIME 2,
                JURY_VETO 2, DATA_QUALITY 1
```

A gate simultaneously helping and hurting, invisible in the mean — the
arxiv 2604.07236 cancellation pattern, live in this codebase. And that last line
is *why* counterfactual replay is required: disabling one gate exposed 23
decisions to later gates, including **DATA_QUALITY — a gate that has never fired
in production — firing once when unmasked.** Counting firings cannot see this.

Four enforced brakes: `stationary_bootstrap_ci` refuses below 20 observations,
`MIN_N_ACTIONABLE=100` for a verdict, Holm-Bonferroni across gates, and a power
statement. Read-only, verified: `v3_guardrail_firings` 22 before and after.

### 6. `scripts/score_tournament_ranker.py` (new) — prevented a bad deletion

The previous handoff had the tournament queued for deletion on Brier 0.3090 vs
0.2266. **That conclusion does not follow.** Brier scores *calibration*; the
board consumes `winning_side` — a **label**, not a probability.

```
bull  n= 75  mean -0.49%  up=56%
bear  n=102  mean -2.85%  up=34%
separation +2.36pp   AUC 0.608
Mann-Whitney p=0.0072   up-rate Fisher OR=2.44 p=0.0056
```

**The tournament ranks.** It is badly calibrated *and* directionally
discriminating — different properties. Deleting it on Brier would have thrown
away the strongest single predictor of board action (bear-win → HOLD 74% vs
bull-win 30%, OR=6.5, p=3.2e-09). The right move is recalibrating `confidence`
(isotonic/Platt) while leaving the ranking untouched.

---

## Deliberately NOT done

**The 74 dead tables (37 empty + 37 stale) were triaged, not dropped.** The
dangerous class would be an empty table code still *reads* — a silent
fail-open. Checked: `data_flags` has 12 references but all 12 are inside
`data_flag_service.py` itself, a self-contained user-flagging feature with no
users. `cycle_checkpoints` (9) is the same shape. These are **dormant features,
not live failures**, and `runtime_parameters` being empty is correct by design.
Dropping tables is destructive and was not requested — the inventory is here
when you want it.

**The floor was not lowered.** It is calibrated: conf ≥70 → +2.54% (n=1464),
<70 → −4.07% (n=605).

---

## What is still open

1. **Do not "fix" the zero-BUY state.** The evidence says the bot declines
   things that fall: at h=5, HOLD −2.30% vs BUY +0.88% vs SELL −9.03%. Verify on
   resolved outcomes in ~2 weeks.
2. **Recalibrate tournament `confidence`** — isotonic on the same data. The
   ranking is real; only the numbers are wrong.
3. **Add point-in-time logging** for the three unreplayable gates
   (`DEGRADED_MODEL`, `NO_PRICE_DATA`, `DROPPED_IMPLAUSIBLE_LEVEL`) so the
   ablation harness can score them. It currently says so honestly rather than
   scoring them as "no effect".
4. **Power:** ~564 changed decisions per gate to detect a 1pp effect; the
   largest gate has 168. Most verdicts stay *needs-more-data* **by
   construction**. Re-run `gate_ablation.py` monthly as n grows.

## System-dynamics reading

The user asked whether this framing applies. It does, and it names the
pathology: these were not bad components but **broken feedback loops** —
measurement that never closed (write-nowhere paths, fields lost on persist), a
**stock-vs-flow confusion** (2,642 was a one-off backfill; 509 is the daily
flow — I misread this mid-audit and had to retract a "price collapse" finding),
and ~45-day resolution lag so every correction lands on stale evidence.

The operative rule: **stop adding controls until the sensors work.** Every fix
in this wave is a sensor repair or a coverage fix. Not one new gate was added.

---

## Method note

Four of my own hypotheses were refuted by data during this work: price staleness
as the HOLD cause (all 45 all-HOLD decisions had fresh prices), risk-flag density
(+0.19 flags, too small), the seat-fallback theory (228/228 correct), and the
"price collapse" (a stock/flow error). The `rowcount` fix would have been a
silent no-op had I trusted the wrapper's source instead of testing it live.
Read the commit bodies — they carry the measurements.
