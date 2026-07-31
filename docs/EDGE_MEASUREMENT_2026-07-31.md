# Does the desk beat a free baseline? — measured 2026-07-31

**Answer: cannot be determined at this sample size, and will not be for roughly a year at the
current cadence. Do not act on the apparent edge below.**

Read this before re-deriving it. The numbers look like a strong result and they are not.

## What was measured

Forward 5-bar returns, decisions from 2026-05-06 to 2026-07-24.

| quantity | value |
|---|---|
| desk BUY decisions | **+2.74%** mean (n=870, sd 18.2) |
| baseline: hold ANY ticker in `price_history`, same dates | +1.40% (sd **96%** — penny stocks dominate) |
| baseline: restricted to the desk's own 288-ticker universe, \|move\| < 100% | **−0.20%** (n=16,803, sd 9.98) |
| apparent edge vs the matched universe | **+2.94pp**, naive SE 0.62pp → nominally ~4.7σ |
| **honest MDE**, `scripts/power_report.py --horizon 5` | **8.84pp** |

## Why the 4.7σ is not real

`power_report.py` exists because this repo has already been burned by exactly this arithmetic: its
docstring records a "−2.26pp, p=0.0032" result that turned out to rest on roughly **two independent
windows**. Forward windows inside a short decision span overlap almost completely, and the tickers
are cross-correlated on top of that (PC1 ~47% of variance).

Run against this cohort the design effect cuts effective n from **1802 → 329** (ICC 0.027 over 16
time blocks at h=5). The resulting detection threshold is **8.84pp**. The apparent edge is +2.94pp.
It is a third of what would be needed to see it.

Per the tool's own table, detecting a 3.0pp effect needs ~1,430 effective observations against the
current 329 — about 4.3× more data.

## Two traps this exercise walked into

1. **Baseline choice swung the answer.** "All tickers" gives +1.40% and makes the edge look like
   +1.34pp; the desk's own universe gives −0.20% and makes it +2.94pp. Nobody had ever pinned that
   choice deliberately. Whichever baseline is used has to be stated with the number.
2. **`n_live_tup` is not a row count.** Related but separate: `pg_stat_user_tables` reported
   `price_history` at 29,813 rows when it holds 15,165,786, because autovacuum had never run. Two
   tables it reported as *empty* held 93,553 rows. Anything sized off those statistics before
   2026-07-31 is wrong.

## The context that matters more than the edge

**46 trade fills exist in the entire history.** The 1,802 scored outcomes are almost entirely
hypothetical — decisions graded as if they had been executed. Even a validated edge would be an
edge on paper.

## What IS answerable at this sample size

Ranking and calibration questions need far less data than a difference in means, and there is a
real defect in one:

| confidence | n | realized win rate |
|---|---:|---:|
| ~74 | 793 | 63% |
| ~85 | 325 | 66% |
| ~91 | 75 | **72%** |
| ~95 | 44 | **46%** |

The most confident bucket is the worst one, and it is below a coin flip. Meanwhile mean P&L *rises*
with confidence (2.92 → 2.72 → 4.57) — so confidence is tracking **magnitude**, not **frequency**,
while carrying a probability's name. Anything that sizes positions or computes a Brier/ECE score
off it is reading the wrong axis.

Separately, the **confidence-70 floor is validated**: 44.8% realized just below it against 62.7%
just above. Keep it.

`scripts/cycle_audit.py` now asserts the monotonicity and fails on the inversion above.

## Recommendation

Stop trying to prove selection alpha; it is unfalsifiable here for about a year. `power_report.py`'s
own conclusion applies — prefer controls that are **self-validating at small n** (calibration,
risk/sizing, Kupiec-style VaR breach counts) over claims that need a sample we do not have.

## Reproduce

```bash
scripts/power_report.py --horizon 5      # excludes DEGRADED_ARTIFACT by default since 2026-07-31
scripts/cycle_audit.py --check           # includes the confidence-monotonicity check
```
