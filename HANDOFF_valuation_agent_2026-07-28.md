# HANDOFF — Valuation Analyst, mined doctrine, opinion cards (2026-07-28)

**Live on synology at `master@105016b`.** Container healthy,
`CUSTOM_V3_VALUATION_ANALYST` registered with 7 tools, doctrine loading at 6531
chars (12 structural + 8 mined rules), 76 opinion cards over 62 tickers.

---

## What this is

The V3 cycle had **no valuation math at all** — no DCF, no computed EV/EBITDA.
`ev_to_ebitda` existed only as a string scraped off Finviz and
`intrinsic_value_estimate` was free text a board persona was asked to guess. An
agent asked "is this overvalued" with no computed multiple in front of it
invents one the way the quant invented RSI in 171 of 305 reports.

Three pieces now fill that seam:

| Piece | File | Role |
|---|---|---|
| Computed multiples | `app/quant/valuation_block.py` | EV, EV/EBIT, EV/Sales, FCF yield, PEG, leverage, per-field CAGRs, reverse DCF — plus `reconcile_valuation_metrics` |
| The agent | `app/v3/agents/valuation_analyst.py` | non-blocking Layer-2 desk, `valuation_report` artifact |
| Its method | `app/v3/doctrine/` | pinned doctrine: hand-written skeleton + mined rules |
| Per-ticker views | `app/v3/opinion_block.py` + `shkreli_opinions` | what he said about THIS company, dated |
| The mine | `scripts/mine_shkreli_doctrine.py` | offline, stage-gated, resumable |

---

## Data findings — read these before touching valuation

**`financial_history.free_cash_flow` is empty across the entire database.**
Non-null in **1 of 3060** quarterly rows, **0 of 2412** annual. Not a coverage
gap: `yfinance_collector.py:462` hardcodes `None`, and its upsert carries
`free_cash_flow = EXCLUDED.free_cash_flow`, so every run **clobbers** what
`fmp_collector` writes. yfinance owns 955 of 1073 latest snapshots.

> The reverse DCF therefore runs on **NOPAT** (EBIT × 0.79, 422/487 coverage)
> and says so on every line it prints. **The collector is NOT fixed** — that is
> a separate job, and fixing it lights the FCF path up automatically.

**EBITDA cannot be computed.** No D&A column exists anywhere
(`grep -rni "depreciation\|amortization" app/` → zero). The block emits
**EV/EBIT** and labels it. Validated against the independently scraped vendor
figure: COST 37.1x vs 29.9x, GE 40.5x vs 32.9x — both ~1.24x, exactly the D&A
wedge. Calling ours EV/EBITDA would have overstated COST by 24%.

**`financial_history` carries all-null placeholder rows** below real coverage
(COST and GE both have a null 2021 row under four clean years). CAGR endpoints
are therefore chosen **by data and per-field**, never by position — see
`_series_cagr`. Only 5 of 510 tickers have a null NEWEST row, so every real
failure was at the far end, on data that was there all along.

**`eps_growth_next_5y` exists for 16 of 1073 tickers**, so PEG is mostly
`NOT COMPUTABLE`. That is honest output, not a bug.

---

## Design decisions that look like oversights

**The agent is NON-BLOCKING and that is deliberate.** It is queued with FA/QA
off `desk_note` but is *not* in the AND-gate releasing the debate. The scheduler
is FIFO (`_queue_agent` appends, the loop does `tasks_to_run.pop(0)`), so it
lands ahead of bull/bear anyway. A third gate term would need a synthetic stub
artifact in every skip path, and the two that already exist for `fa_skipped` are
the evidence of how easily one gets missed. Its scheduler branch omits
`_check_abort` — a failed valuation must not kill the desk.

**`v3_valuation_analyst` is absent from `skill_optimizer.TARGET_AGENTS`, on
purpose.** The optimizer issues REPLACE actions and rolls back on outcome
scores; the board's doc travelled 1146→1812 chars across 20 accepted rewrites in
five days. A mined doctrine is a SOURCE DOCUMENT — that treatment would leave
nobody able to say which sentences came from the corpus. A comment at
`TARGET_AGENTS` says so.

**Opinion cards go to the valuation desk ONLY, never the Board.** The Board
authorises trades and makes ~1.0 tool calls, so it can verify nothing it is
told; a named investor's opinion there invites deference to a personality.

**`build_opinion_block` returns `""` when there is no coverage** — the one place
in this codebase where an empty block is right. A missing multiple is a gap in
evidence; a missing opinion is not, and announcing it on every desk would teach
the agent to read one commentator's silence as information.

---

## The doctrine

Shipped file `app/v3/doctrine/shkreli_valuation.md` is **generated**. Edit
`app/v3/doctrine/parts/valuation_base.md` (hand-written half) or re-run the
mine; a direct edit is overwritten by the next promote.

```
python scripts/mine_shkreli_doctrine.py --promote --merge
```

Idempotent by construction — it renders both halves rather than appending, and
a missing base REFUSES rather than shipping the mined half alone. `--promote`
refuses while any rule is `UNREVIEWED`.

Rule ids use two namespaces: `N.` structural, `MN.` mined. So
`doctrine_rules_applied` on the artifact reveals not just which rule fired but
which KIND — and that is what makes the mined half measurable against the
generic baseline rather than merely present.

### Mining pipeline, and four defects worth not repeating

```
--index   869 streams (2843h)          # review before fetching
--fetch --analysis-only  SHORTEST-first
--extract    679 candidate rules
--reduce     493 clusters -> 10 drafted
--promote --merge
--opinions   76 cards / 62 tickers
```

1. **The extractor mined the rarest thing in the corpus.** It asked for rules he
   *states* and returned `{"rules": []}` on the Microsoft Full Excel Valuation
   video — correctly, since "margins are really good, 8% revenue growth" is a
   fact about one company. Of 679 mined rules, **653 are inferred and 26
   stated**: he applies his method, he does not narrate it. The prompt now
   recovers method from BEHAVIOUR and tags `inferred=true|false`.
2. **The chunk gate was tuned for the wrong corpus** — built for 23,000 chunks,
   applied to 328. At ≥3 terms it passed 2% and extracted nothing.
3. **The vocabulary was written finance, not spoken.** Across 328 deep-dive
   chunks: `wacc`, `ebitda`, `intrinsic value`, `p/e`, `earnings power` — **zero
   occurrences**; `cash flow` 112.
4. **Duplicate uploads inflated the evidence floor.** 20 titles in the index
   appear more than once. A rule scored `n_distinct_videos: 2` on two
   byte-identical quotes from two uploads of one Microsoft video. Support is now
   counted over normalised titles.

**Corpus facts:** caption availability is duration-dependent (~1h videos 6/6,
~9h streams 4/6) and the longest streams have NO auto-captions, only a
`live_chat` track — hence shortest-first, which took the fetch success rate from
33% to 92%. `--flat-playlist` returns `upload_date: None`, so the fetch makes a
separate `--print "%(upload_date)s"` call; without it every analysis card is
dropped by the no-date rule.

The shared scraper **cannot** serve this corpus: it hardcodes `/@handle/videos`
(livestreams are under `/streams`), its RSS path caps at ~15 entries with
`duration: 0`, and its yt-dlp fallback has a 30s timeout. Do not "fix" it for
this — it serves the live cycle.

---

## Verify it works

```bash
# 1. Read a real block. Cross-check EV/EBIT against the scraped ev_to_ebitda —
#    ours must be HIGHER (no D&A add-back) by roughly the D&A wedge.
python -c "from app.quant.valuation_block import build_valuation_block; print(build_valuation_block('COST'))"

# 2. A ticker with opinion coverage
python -c "from app.v3.opinion_block import build_opinion_block; print(build_opinion_block('INTC'))"

# 3. One live ticker through the cycle
#    Confirm: reaches final_decision (the debate gate was deliberately untouched),
#    valuation_report lands AHEAD of bull/bear in the iteration log,
#    the reconcile log line appears, doctrine_rules_applied is non-empty.
```

**`doctrine_rules_applied` empty across every ticker = the doctrine is in the
prompt but unused** — the "shipped the mechanism, not the capability" failure.
That is the number to watch first.

Log `report["corrected"]` from the reconcile on every run: in 30 days it gives
an actual fabrication rate for valuation multiples, the way 171/305 was
countable for RSI.

---

## Open / next

- **13 supported clusters were dropped by the `TOP_CLUSTERS = 40` cap** and 440
  fell below the 2-recording floor. More corpus (only 253 of 869 indexed videos
  are fetched) would surface more repeated rules.
- **The FCF collector bug is unfixed.** Highest-value follow-up: it would light
  up FCF yield, EV/FCF, fcf_cagr and a cash-based reverse DCF at once.
- **Opinion coverage is sparse on the watchlist** — his coverage skews to
  small-cap biotech the desk does not track. 38 of 110 transcripts produced no
  card; all drops were correct (unknown ticker, delisted, or not one company).
- Canonicalisation is LLM-driven and varies between `--reduce` runs; a good rule
  can be absorbed into a generic neighbour. Re-run and diff before promoting.

**Pre-existing test failures, NOT from this work** (verified identical on a
stashed clean tree): `test_parameter_tools.py::test_whitelists_grant_write_to_pm_and_board_only`
and `test_tool_whitelists.py::test_quant_analyst_has_calculator_tools` — both
assert tools deliberately dropped in earlier audits.

1716 tests pass.
