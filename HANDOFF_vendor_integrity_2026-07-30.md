# HANDOFF — the one-vendor rule reached 3 call sites; the live desk was not one of them

**Shipped `ee75867`, deployed to `synology` 2026-07-30 20:31:59Z.**
Container confirmed `Up (healthy)`, restarts=0, via `docker ps` after the deploy.

Started as an audit of a proposed quant-layer plan. The audit found the plan was
arguing from three misread statistics and proposing to rebuild things that
already shipped — but it also turned up four unfixed instances of the vendor bug
the 07-29 wave was supposed to have closed, one of them feeding every desk's
technical indicators.

Companion: `HANDOFF_quant_layer_session_2026-07-30.md` (the wave this extends).
Full audit + revised plan: `~/.claude/plans/audit-and-verify-if-memoized-pond.md`.

---

## The finding

`price_history` has primary key `(ticker, date, source)`, so one ticker-date
carries several vendor prints, and they disagree by a mean **20.05%** —
yfinance publishes dividend/split-adjusted closes, polygon raw.

The 07-29 fix pinned one vendor in `app/quant/returns.py` and routed
`outcome_tracker` + `agent_scorecard` through it. **That was 3 call sites. Over
40 other reads never got the filter**, including the path that writes the
`technicals` table the Board reads.

Measured on the 500-session indicator window:

```
ticker   UNFILTERED rows/dates    PINNED rows/dates
CRH           500 / 251              255 / 255
ALLY          500 / 251              500 / 500
ASML          500 / 251              500 / 500
DRIP          500 / 251              303 / 303
CVX           500 / 251              500 / 500
AAPL          500 / 500              500 / 500   <- single-source control
```

Every dual-source ticker's "500 session" window covered **251 sessions**. A
200-day SMA spanned ~100 real ones, and consecutive rows alternated between two
adjustment conventions.

Indicator deltas: **CRH RSI +4.99**, **DRIP RSI −8.06**, ASML vol +12.22pp,
CVX vol −11.28pp, CRH vol +7.78pp. **DRIP read 350 daily moves over 15%;
pinned, it reads 1.**

**AAPL is byte-identical.** That is the point — the fix is a no-op on the
~2,700 single-source tickers, so "the numbers didn't change" proves nothing.
Verify on the subset that changes.

⚠ **There are now 56 dual-source tickers, not the 38 recorded on 07-29.** The
affected set is growing. Re-measure rather than trusting the count.

## What was fixed

Live paths first:

| module | what it corrupted |
|---|---|
| `technical_processor.compute_technicals` | the `technicals` table → `technical_baseline` → every desk. Both reads pinned; the spot close now shares the vendor of the SMAs it is compared against |
| `challenger.py:160,227` | a **second, independent outcome tracker** with the exact entry-vs-exit vendor mismatch `outcome_tracker` was already fixed for |
| `quant_edge_verifier.load_historical_data` | feeds `run_equation`'s `df`. No LIMIT, so it returned two rows per shared date and fanned them out again through the technicals merge |
| `factors.load_price_panel` | `aggfunc="last"` silently picked a vendor per date |
| `regime_hmm`, `technical_baseline` | HMM return series; min-bars gate; spot price/volume |

`dominant_source_sql()` and `keep_dominant_source()` are now **public**. The
rule belongs to the table, not to the evaluation layer — reimplementing it
privately is how the same bug shipped twice.

## Why it recurred, and what now stops it

The old guard (`test_forward_window_source.py:66`) greps `inspect.getsource` for
`_dominant_source_sql()` in **named modules**. A new unfiltered read could not
fail any test.

`tests/unit/test_price_history_one_vendor_guard.py` replaces it with a repo-wide
AST scan requiring every `SELECT` against `price_history` to pin a vendor.

Three things make it hard to fool:

1. **A ratchet, not an allow-list.** `KNOWN_UNPINNED` carries the **27 files
   still unfixed** with a per-file count. Counts may only go DOWN — fixing a
   query without lowering its budget also fails. New files get budget 0.
2. **A self-check.** `test_the_scanner_actually_finds_queries` fails if the scan
   stops matching ≥25 reads, so a broken scanner cannot go green silently.
3. **It requires a `SELECT`.** The first draft matched
   `technical_processor.py`'s own *docstring* ("compute indicators from
   price_history") — the repo's own "tests that match prose" trap, caught by
   running it.

**Verified by reverting a fix and watching it fail**, then restoring.

## Also shipped

**`scripts/power_report.py`** — the measurement ceiling is now a command.

The "MDE 2.24pp" figure that governs what is worth building was computed by hand
on 07-30 and could not be re-run. This reproduces it (`mde(5.0, 157) = 2.24`,
pinned in a test) and reports three numbers on live data:

```
resolved outcomes ......... 1785        DEGRADED_ARTIFACT ... 361 (excluded)
trade fills ............... 46          span ................ 2026-05-06..07-23
pnl_pct sd ................ 28.76pp     rho ................. 0.033
effective n (design eff) .. 191.9

MDE (naive, n=1785) ....... 3.81pp   <- treats clustered desks as independent
MDE (design-effect, n=192)  11.63pp  <-- USE THIS
MDE (blocks only, n=8) .... 56.98pp  <- discards cross-sectional breadth
```

The naive n and the blocks-only count bracket the truth; neither is right. The
Kish design-effect correction is the defensible middle, and it says a new
selection signal must move P&L by **~11.6pp** to be detectable.

⚠ **That is not a contradiction of the 2.24pp figure — it is a different
measure.** 2.24pp came from sd=5.0pp at n=157 (a horizon-matched forward move);
this is `decision_outcomes.pnl_pct` at sd=28.76pp. Say which measure you mean.

**`tests/unit/test_ticker_complete_invariant.py`** — `check_ticker_complete`
emitted three invariants (`PIPELINE_NO_DESK`, `PIPELINE_NO_TRADE_ROW`,
`PIPELINE_COMPLETE_BUT_NO_DECISION`) and had **zero tests**. 19 added, each
mutation-verified: flipping the degraded sentinel breaks 5, dropping the
`elif` guard breaks 2.

Kept **deliberately separate** from `DESK_STALLED_MID_PIPELINE`. They are two
observers and are only worth having when they can disagree.

## Corrections to the plan this came from

Anyone citing the previous handoffs should know:

1. **"the tournament was 6.5× worse on removal, p<0.0001" is a misread.** The
   free quant signal was 6.5× **better** on removal (−1.85% vs −0.29%, n=14/29,
   **no p reported**). `p<0.0001` belongs to the redundancy chi²=16.63. A third
   6.5 exists — Fisher OR=6.5, p=3.2e-09 — and measures the board *obeying* the
   verdict, which the retirement doc rules out as evidence of value.
2. **The retirement's cost premise is stale.** `HANDOFF_hooks_followup` open
   item #1: the "31% of spend" basis used a denominator missing up to 14.5%.
   Recomputing is cheap and "changes decisions". **Not done here.**
3. **A dissent is unresolved.** `HANDOFF_harness_hooks` §3 says "the tournament
   RANKS — do not delete it" (AUC 0.608, p=0.0072), measured on all desks; the
   retirement measured selection on traded desks. Both can be true.
4. `PIPELINE_COMPLETE_BUT_NO_DECISION` is **not** the stall detector; that is
   `DESK_STALLED_MID_PIPELINE`.
5. "45 fills over 5 weeks" — 45 fills is the book's **entire history** (now 46);
   5 weeks is the completed-desks span. The MDE was `sd 5.0pp, n=157`, a
   different population again.

## Open

- [ ] **27 files still read `price_history` unpinned** — the ratchet lists them
      with counts. Live-path ones remaining: `paper_trader` (3), `portfolio`,
      `scoring_engine` (2), `orchestrator`, `packet_builder`,
      `quant_processor` (8), `market_tools`, `watchlist`.
- [ ] **`technicals` is stale for the 56 dual-source tickers.** The fix corrects
      what is *computed from now on*; existing rows were written from mixed
      vendors. Run `scripts/refresh_technicals.py --ticker <T>` for them, or
      accept the drift until the daily refresh rolls through. **Back up first.**
- [ ] Recompute share-of-spend on the fixed denominator (item 2 above).
- [ ] `dgx_spark` (10.0.0.141:8000) still DOWN — every boot burns 3 min failing
      readiness 36×, service runs at ~half LLM concurrency. Also blocks the
      tournament token-saving check (a 0-token row today is the outage, not the
      change: `SKIPPED`/0ms = the change, `SUCCESS`/~260ms = the fallback).
- [ ] `.claude/worktrees/fidelity-followup` is still on disk and locked by
      another session — third handoff in a row to carry this.

## State

```
master ......... ee75867, clean, pushed
deploy ......... synology 2026-07-30T20:31:59Z, Up (healthy), restarts=0
tests .......... 2,685 pass (was 2,650); 1 pre-existing failure
                 (test_parameter_tools, VLLM-dependent) reproduces on master
DEBATE_ENGINE .. 3 (unchanged — nothing here touches decision logic)
```

No decision logic was changed. Every change either pins a vendor on a read that
was already wrong, or adds a measurement that did not exist.
