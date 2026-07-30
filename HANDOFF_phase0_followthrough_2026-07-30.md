# HANDOFF — technicals repaired, the tournament premise re-checked, and where its 28% went

**2026-07-30, after `a0dc8dc`.** No code shipped. This is the operational
follow-through on `HANDOFF_vendor_integrity_2026-07-30.md`: repairing the data
that fix could not reach, and running two checks that were blocked.

Everything below is measured against the live DB.

---

## 1. The stale technicals are repaired

The vendor fix corrects what is *computed from now on*. The rows already in
`technicals` were written from vendor-mixed windows and stayed wrong.

**`refresh_technicals.py --stale-days` would never have found them.** Its
staleness test is date lag — "is the newest technical row behind the newest
price row" — and these rows were *current*. Only the VALUES were wrong. Forced a
per-ticker recompute across all 56 dual-source tickers instead.

```
backed up ............. 28,941 rows (10.5 MB CSV, scratchpad/backup/)
recomputed ............ 56 tickers
changed an indicator .. 31 / 56
of which HELD ......... ALLY, HOOD, LLY, NVDA  (all 4 held dual-source names)
```

Largest moves: PNC RSI **+8.25**, SPMO RSI **−8.81**, RH RSI **+7.69** and
SMA-200 **+19.00**, PNC SMA-50 **−11.65**, RGA SMA-50 **−10.66**.

**Two crossed the label the desk actually reads:**

```
GM     73.44 OVERBOUGHT -> 67.51 NEUTRAL
HOOD   23.59 OVERSOLD   -> 36.00 NEUTRAL     <-- HELD POSITION
```

HOOD was being described to the Board as OVERSOLD — a buy signal — on a
12.4-point RSI error, while the bot held it. That is the concrete cost of the
bug, and it is why "the indicator moved a bit" was the wrong way to think about
it: the numbers feed a *threshold*.

⚠ **25 of 56 did not change.** Those are dual-source tickers whose vendors
happen to agree closely, not evidence the repair failed. The negative control is
that single-source tickers were never recomputed at all.

## 2. The tournament's cost premise survives recomputation — at 27.8%, not 31%

`HANDOFF_hooks_followup` open item #1: every share-of-spend figure before
2026-07-30 used a denominator missing up to 14.5% of tokens, and "it changes
decisions — the tournament's 31% share was the basis for retiring it."

**The flush works.** Cost-record coverage, desks created since telemetry began:

```
before the flush deploy ... 638 desks   70.4% have cost rows
after  the flush deploy ....  25 desks   96.0% have cost rows
```

**Recomputed share of spend, trailing 10 days, 167.4M tokens:**

```
v3_tournament_debate ....  46,615,322   27.8%   <- was quoted as 30.1% / 31%
v3_junior_analyst .......  44,383,506   26.5%
v3_fundamental_analyst ..  40,773,433   24.4%
v3_quant_analyst ........  10,846,657    6.5%
```

**The conclusion holds.** The tournament was ~28%, not ~31% — smaller than
claimed, still the largest single cost centre by a nose. Correcting the
denominator can only *shrink* a component's share (it adds tokens elsewhere), so
this direction was predictable; the point was that it did not shrink enough to
matter. **Retiring it was the right call on the corrected numbers.**

⚠ Caveat: the trailing-10d window is still 638-before vs 25-after the flush, so
the denominator remains partly incomplete. 27.8% is an upper bound.

## 3. The retirement is working. The pre-registered prediction still failed.

Both halves matter.

**It took.** The two 0-token states are distinguishable and this is the right one:

```
2026-07-30    1 run   SKIPPED   0 tokens    0 ms     <- engine 3 (the change)
2026-07-29   26 runs  SUCCESS   7,039,948   187,027 ms
```

`runtime_parameters` is **EMPTY**, so the registry default (3) governs — no
stale override resurrecting the cost. The tournament does not appear in the top
nine agents by spend on 07-30 at all: **0.0%**.

**And yet the prediction missed by 2×:**

```
PREDICTED after retirement ... 446,360 tokens/ticker
2026-07-30 actual ............ 905,378 tokens/ticker
```

The pre-registered falsification criterion was *"if tokens per ticker does not
land near 446k, the retirement did not take."* **That criterion is wrong.** The
retirement demonstrably took; the prediction failed because it assumed every
other agent's spend held constant. It did not. This is worth recording as a
lesson: a prediction about a *total* cannot falsify a claim about a *component*
unless the rest is pinned.

## 4. What the freed 28% went to — a bigger lever than the tournament

Runs per (cycle, ticker). 1.00 means each agent ran once, as intended:

```
v3_valuation_analyst .... 1.41   <-- 
v3_fundamental_analyst .. 1.39   <--  the four most expensive agents
v3_junior_analyst ....... 1.37   <--  all run ~1.4x per ticker
v3_quant_analyst ........ 1.35   <-- 
v3_debate_judge ......... 1.00
v3_decision_synthesizer . 1.00
contradiction_shadow .... 1.00
v3_regime_engine ........ 1.00
```

**The amplification is confined to the four analyst agents** — every downstream
agent is exactly 1.00, so this is not a cycle-level re-run. Roughly **28-40% of
analyst spend is duplicate invocations**, and analysts are ~72% of the budget.

It is **not** the known `AGENT_ERROR`: only 1 error in 3 days (164,625 tokens).
109 of 110 fundamental runs returned SUCCESS. These are *successful* duplicates.

**Undiagnosed, and it is now the largest identified waste in the pipeline** —
comparable to or larger than the tournament that was just retired, and unlike
the tournament it buys nothing at all. Start at whatever dispatches the analyst
phase; the 1.00 downstream agents say the fan-out is upstream of them.

## Open

- [ ] **Diagnose the ~1.4 analyst runs/ticker.** Highest-value open item.
- [ ] 27 files still read `price_history` unpinned (ratcheted in
      `tests/unit/test_price_history_one_vendor_guard.py`); live-path ones:
      `paper_trader`, `portfolio`, `scoring_engine`, `orchestrator`,
      `packet_builder`, `quant_processor`, `market_tools`, `watchlist`.
- [ ] Re-run the share-of-spend check once the post-flush window is wide enough
      to stand on its own (currently 25 desks).
- [ ] The `technicals` repair is a point-in-time fix. Any ticker that becomes
      dual-source later gets silently corrupted again until the daily refresh
      recomputes it — and the staleness heuristic cannot see value corruption.
      Consider a periodic forced recompute for dual-source tickers.
- [ ] `.claude/worktrees/fidelity-followup` still on disk, locked by another
      session — fourth handoff carrying this.

## State

```
master ......... a0dc8dc, clean
deploy ......... synology, Up (healthy), restarts=0
dgx_spark ...... UP (was down; unblocked the checks above)
DEBATE_ENGINE .. 3, runtime_parameters empty, tournament at 0.0% of spend
technicals ..... 56 dual-source tickers recomputed; backup in scratchpad
```

Backup: `scratchpad/backup/technicals_dual_source_backup.csv` (28,941 rows, all
22 columns) + `tickers.txt`. Restore is a straight upsert if needed.
