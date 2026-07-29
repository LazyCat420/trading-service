# Board HOLD decomposition — and the finding that displaced it

**Measured 2026-07-29 against the live DB (10.0.0.16:5433/trading_bot).**
Every number below carries its window and population. Read the window before the number.

---

## Headline

The investigation was scoped to "the Board holds 64% of the time — decompose it before
touching it." The decomposition says the HOLD rate is **not** the finding. This is:

> **Board confidence collapsed from a ~78 mean to ~61, and executable BUYs went to zero.**
> On 2026-07-28 and 07-29 the pipeline produced **44 decisions and 0 BUYs**. Every one of
> the 22 Board BUYs in the late window scored **below the confidence floor of 70** — the
> highest was 68.

The floor is doing exactly what it was calibrated to do. The Board stopped clearing it.

Two controls (matched tickers; artifact presence) point at the *cause*: the agent-fidelity
fixes of 07-20..07-26 removed fabricated support for high confidence. **The most likely
reading is that the system is now honestly reporting low conviction** — not that it broke.
The floor separates a +3.69% population from a −1.78% one at n=859, so the one change that
would restore trade flow immediately is the one change that would knowingly lose money.

---

## Phase 0 — the analyzable window (blocking, and it binds hard)

`policy_action` landed 07-23, `decision_provenance` 07-25. Coverage per day:

```
day          n   policy_action  provenance   fully instrumented
2026-07-23   36      16              0
2026-07-24   16      16              0
2026-07-25   23      23             14
2026-07-26   19      19             19        YES
2026-07-27   30      30             30        YES
2026-07-28   33      33             33        YES
2026-07-29   11      11             11        YES
```

**W_instr = 2026-07-26 .. 07-29, n = 93.** That is below the n≥150 bar this analysis set for
cost tests, so **no per-population P&L significance test is run on W_instr**. Where a longer
window is used below it is stated explicitly and the provenance filter is dropped.

**Negative control (ran before believing anything):** the same classifier over the pre-07-23
window reports n=530, `policy_action`=0, `provenance`=0 → **100.0% uninstrumented**. It does
not invent labels on rows that predate the instrumentation.

---

## Phase 1 — the decomposition

Window 2026-07-26..07-29, population `trade_results`, n=93.

```
action histogram          HOLD 86 (92.5%)   BUY 7 (7.5%)   SELL 0
```

HOLD decomposition, n(HOLD)=86 — populations sum exactly, unclassified = 0:

| population | n | % of HOLDs |
|---|---:|---:|
| `board_reasoned` + `HOLD_NO_SIGNAL` | 82 | 95.3% |
| `coerced_unshortable` + `HOLD_NO_SIGNAL` | 4 | 4.7% |

**No policy gate fired at all in this window.** No `HOLD_POLICY_BLOCKED_LOW_CONFIDENCE`,
no `HOLD_NO_POSITION`, no degrade/triage rows. The five-population model this analysis was
built around collapses to two, and one of them is 95% of the mass.

---

## Phase 2 — reconciliation against the prior 52% claim

`docs/DECISION_INTEGRITY_PLAN.md:31-42` measured 209 HOLD / 101 BUY / 90 SELL (52% HOLD,
n=400) and warned "do not fix the HOLD rate."

**The leading hypothesis was that the 52% → 64% delta was the unshortable-SELL rename.**
It is not, and the pre-coercion histogram kills it: only **4** coerced rows exist in W_instr,
4.7% of HOLDs. Substituting them back to SELL moves 92.5% → 88.2%. It explains nothing.

The rename hypothesis was a reasonable read of 90 → 38 SELLs across the two published
windows, but it does not survive contact with the instrumented data. **Recording it as
refuted matters more than the fact that it was mine.**

What actually happened is larger than either figure: the HOLD rate in W_instr is **92.5%**,
not 64% and not 52%. All three are different windows of a *moving* quantity, which is why
none of them should be quoted without a date.

---

## Phase 3 — where the HOLDs actually come from

Joining the pre-veto Board verdict (`shared_desk.desk_data->'final_decision'->>'action'`) to
the final action, W_instr, n=93:

| Board | synthesizer | final | n |
|---|---|---|---:|
| HOLD | HOLD | HOLD | 62 |
| BUY | HOLD | HOLD | **24** |
| BUY | BUY | BUY | 7 |

So the Board proposed BUY 31 times and 24 were downgraded — a 77% downgrade rate on BUYs,
against the handoff's stated 24% over a 14-day window. That looked like the synthesizer
turning aggressive. **It is not.** Splitting the same 22 late-window Board BUYs by the
Board's own confidence:

```
Board conf >= 70 :  0 decisions
Board conf <  70 : 22 decisions      (max 68, mode 62)
```

**Every Board BUY in the late window was already below the floor.** The downgrade and the
floor are not two effects; they are one, and the cause sits upstream of both.

---

## The actual finding — a confidence distribution shift

Board confidence on all decisions, by day (16-day window, desk join, no provenance filter —
population stated because it is wider than W_instr):

```
day          n   min  max   avg   n(>=70)
2026-07-14  15   60   85   72.7    9
2026-07-15  25   62   85   78.2   23
2026-07-16  61   50   90   78.6   54
2026-07-17  12   65   85   77.9   10
2026-07-18  22   58   88   75.5   17
2026-07-19  12   65   85   74.3   10
2026-07-20  42   55   82   69.9   23
2026-07-21  19   55   85   68.0    9
2026-07-22   5   55   78   65.6    2
2026-07-23  36    0   85   60.8   11
2026-07-24  16   10   75   61.8    4
2026-07-25  23   55   75   64.1    3
2026-07-26  19   45   68   61.0    0
2026-07-27  30   55   80   61.9    1
2026-07-28  33   55   85   61.8    2
2026-07-29  11   55   75   61.0    1
```

Mean falls ~78 → ~61 across 07-20..07-26 and stays there. `max` still reaches 80–85, so this
is **not a cap or a clamp** — the whole distribution moved down.

Consequence for trade flow (`final action = BUY`):

```
07-17  66.7%     07-23  16.7%     07-27   6.7%
07-18  59.1%     07-24  18.8%     07-28   0.0%   (33 decisions, 0 BUYs)
07-20  42.9%     07-25   8.7%     07-29   0.0%   (11 decisions, 0 BUYs)
```

Significance of the shift, early (07-14..07-25) vs late (07-27..07-29), on Board BUYs that
end as HOLD: **8/91 (8.8%) → 20/22 (90.9%), Fisher exact p = 7.6e-14, odds ratio 104.**
Far past the t≥2 / p<0.05 bar this analysis committed to in advance.

---

## Is the floor wrong? No — it is the best-evidenced thing here

Resolved BUY outcomes, `decision_outcomes`, 30-day window:

```
conf <  70 : n=143  mean pnl -1.782%   W/L  49/74
conf >= 70 : n=716  mean pnl +3.687%   W/L 389/221
```

The floor separates a losing population from a winning one at n=859. **Lowering it to restore
trade flow would buy volume by knowingly taking −1.78% trades.** That is the change this
analysis explicitly refuses to recommend, and it is the same error the `consensus_strength`
floor was correctly refused for: acting on trade-count rather than expectancy.

---

## Two controls that discriminate "honest confidence" from "scoring regression"

These have opposite remedies, so they were tested rather than argued.

**Control 1 — matched tickers.** Same names scored both before (07-14..07-19) and after
(07-26..), so the universe cannot explain the shift:

```
MCD  81.3 -> 52.0  (-29.3)      XOM  76.0 -> 68.0   (-8.0)
GOOGL77.5 -> 52.0  (-25.5)      WMT  74.0 -> 67.0   (-7.0)
COST 85.0 -> 64.5  (-20.5)      AMZN 70.0 -> 64.0   (-6.0)
JPM  81.9 -> 65.0  (-16.9)      TSLA 86.0 -> 81.0   (-5.0)
NVDA 70.7 -> 59.2  (-11.5)      MSFT 66.0 -> 62.0   (-4.0)
JNJ  80.0 -> 69.0  (-11.0)      AAPL 65.8 -> 75.4   (+9.6)
```

**11 of 12 fell; mean −11.3 points on identical tickers.** The shift is in the scoring, not
the ticker mix.

**Control 2 — did a fidelity fix mechanically remove a confidence input?** If confidence fell
because an input went missing, artifact presence should have *dropped*. It rose across the
board:

```
artifact                before (n=301)   after (n=112)
fundamental_report          56.5%           85.7%
quant_report                55.8%           85.7%
valuation_report             0.0%           39.3%   (new)
desk_note                   57.1%           86.6%
tournament_result           50.2%           84.8%
final_decision              85.0%           97.3%
trade_decision              48.8%           83.0%
regime_classification       63.5%           86.6%
```

**Nothing was removed — the desks became markedly more complete while confidence fell.** A
regression that deletes an input is ruled out. The remaining consistent explanation is that
the agent-fidelity fixes landing in that same window (`43a79fd` quant stop inventing risk
numbers, `343fb37` fundamental separate business view from trade horizon, `ddf0b95`
fundamental emitted no numbers, `9c450ef` positioning_read) removed *fabricated* support for
high confidence. On fuller evidence the desks are **less** sure — which is what honest scoring
looks like.

**This is the good outcome, and it is uncomfortable:** the system is now accurately reporting
that it does not have high-conviction ideas. The trade flow did not break; the confidence that
justified it was partly manufactured.

Caveat worth stating: n=12 matched tickers, and both controls are consistent with the honest
reading rather than proving it. A cheap confirmation is to re-run the matched set once more
names accumulate post-07-26.

## What to do next — in priority order

1. **Accept the confidence shift as most likely correct, and confirm it once.** Re-run the
   matched-ticker control when the post-07-26 sample is larger. If it holds, the HOLD rate is
   the truth rather than a defect, and the correct response is patience, not a looser gate.
2. **Do not touch the floor of 70.** Calibrated at n=859 above, and independently at
   n=130/n=698 in the prior handoff.
3. **Watch expectancy, not volume.** Weekly BUY expectancy is already soft (week of 07-13:
   −0.667%, n=51; week of 07-20: −0.055%, n=26) — a reason to be *more* cautious about
   restoring flow by loosening a gate, not less.
4. **The HOLD-rate question is closed as a target.** It is a symptom of the confidence
   distribution, not an independent lever. A decomposition of a downstream count cannot
   diagnose an upstream distribution, which is why this document stops here rather than
   proposing a HOLD-rate change.
5. **If trade flow must be restored, do it upstream.** The legitimate route is better
   evidence raising genuine conviction — not lowering the bar that separates a +3.69%
   population from a −1.78% one.

## Method notes

- Every query ran against the live DB, not against source reading.
- The negative control ran **before** any decomposition number was believed.
- The prior's 52%, the handoff's 64% and this 92.5% are three windows of a moving quantity.
  None of them is "the HOLD rate."
- The rename hypothesis this analysis started from was **refuted by its own primary test**
  and is recorded as refuted rather than quietly dropped.
