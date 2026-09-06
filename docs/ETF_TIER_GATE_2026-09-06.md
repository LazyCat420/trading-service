# An ETF is not a micro-cap company, and the row always knew it — 2026-09-06

Branch `fix/etf-tier`, commit `cc52d04` on top of `138b713`. Found on the
verification cycle `cycle-v3-1788660665`, the first live run of `e592a26`
(`ensure_ticker_metadata`). Status at time of writing: tests green, NOT yet
merged or deployed — the verification cycle is live and a trading-service deploy
kills a live cycle.

## What the cycle showed

The gatekeeper selected SCHD, ABT, NBIS. Check #12 of the pre-written
expectations ("every selected ticker has a `market_cap_tier` after selection")
failed on SCHD. The container log:

    19:17:58 [TickerMeta] SCHD: no market cap on file — left untiered
    19:17:58 [PipelineService] Mega-cap cap ran blind on 1 selected name(s)
             with no market_cap_tier: ['SCHD']

Both sentences are false about the same document. SCHD's `ticker_metadata` row:

    {ticker: SCHD, asset_class: "etf", market_cap: 112337240064, market_cap_tier: null}

`ensure_ticker_metadata` projected `ticker` + `market_cap_tier` and nothing
else, so it could not see the cap or the asset class, called yfinance, got
nothing an ETF reports as `marketCap`, and fell open exactly as designed for
"a symbol with no market cap at all". The design premise was false for 38 of
the 39 rows it would ever meet.

## Census (1,049 `ticker_metadata` rows, measured before any change)

| population | count | finding |
|---|---|---|
| untiered rows | 39 | 38 are `asset_class: etf` with a cap on the row; 1 is BK |
| BK | 1 | dead symbol (BNY Mellon moved to `BNY`); yfinance 404 — the only true fail-open |
| `asset_class: etf` rows | 85 | 38 untiered + 47 tiered |
| tiered ETFs | 47 | **all 47 say `micro`** — QQQM $104B, JEPI $46B, XLV $43B |
| all tiered rows with a cap | 795 | 31 disagree with `tier_for_market_cap(own cap)`: 28 ETFs + 3 boundary stocks (MCD $195B/mega, MRVL $211B/large, DPZ $9.96B/large) |

`tier_for_market_cap` cannot return `micro` for a $104B cap, so the 47 labels
did not come from the cap on the row. The only writer of `"micro"` in the tree
is that function; the label disagrees with its own document.

## Decision (operator, asked 2026-09-06)

Three options were put to the operator: (a) a tier of the ETF's own, (b) derive
the company tier from the stored cap (makes SPY/VTI/VOO/QQQ/IVV/BND/VUG/VTV
"mega" and subject to the one-mega-cap-per-cycle cap), (c) fix only the false
log line. **Chosen: (a).** The mega-cap cap stays a statement about
single-company concentration, which is what it was built for.

## What shipped (`cc52d04`)

* `app/services/ticker_meta.py`: `ETF_TIER = "etf"`, `ETF_ASSET_CLASSES`.
  `ensure_ticker_metadata` now projects `asset_class` + `market_cap`; tags a
  fund from its own row with **no vendor call**; **corrects** a company tier
  sitting on a fund (a category fix from the row itself — the "existing tier
  always wins" guarantee is unchanged for companies); when the vendor has
  nothing, uses the cap already on the row before giving up, so "no market cap
  on file" is now only logged when true. `tier_for_market_cap` untouched.
* `scripts/backfill_market_cap_tier.py`: a fund pass, first, through
  `ensure_ticker_metadata` (one authority), funds excluded from the vendor pass.
  Dry-run against production: **85 funds → etf, 1 company target (BK, no cap,
  left untiered)**.
* `tests/unit/test_etf_tier_gate.py`: 21 tests, every fixture a verbatim
  production row (SCHD, QQQM, BK, ABT).

## Proof

* Red first: the symbol did not exist (ImportError on collection).
* Sabotage, one property at a time, after green: drop `asset_class` from the
  projection → 1 fails; `ETF_TIER = "micro"` → 4 fail; remove the stored-cap
  fallback → 1 fails; let an existing tier block the correction → 1 fails;
  make the tier falsy → 1 fails; `ETF_TIER = "mega"` → the cap-wiring test
  fails. Each mutation killed by a distinct test.
* The cap-wiring test DERIVES the mega-cap comparison literal from
  `pipeline_service` by AST rather than transcribing `"mega"`.
* Prior `test_ticker_metadata_gate.py` (16 tests) still green. Worktree unit
  suite: **6,392 passed / 0 failed / 85 skipped**.
* Consumers of `market_cap_tier` swept: the cap (`== "mega"`), ontology_builder
  (label pass-through), portfolio (pass-through). None enumerates the buckets;
  a sixth value breaks nothing. The sp500 loader iterates `SP500_TICKERS` only
  and cannot touch an ETF row.

## Still to do (in order)

1. Cycle `cycle-v3-1788660665` reaches terminal → remaining checks.
2. Backup taken: `~/db-backups/ticker_metadata-etf-tier-2026-09-06/`
   (full 1,049 rows + the 85 ETF rows before). Then run
   `scripts/backfill_market_cap_tier.py` (no flags) once — expect 85 tagged.
3. Merge `fix/etf-tier`, deploy trading-service, confirm the next
   GATEKEEPER_SELECTED event has `tier_unknown: []` when a fund is selected.

## Open item found in the same pass — check #6, the loop pin residual

`b873016` moved the sector compute into `asyncio.to_thread`. On this boot the
compute ran 160 s (19:26:12 → 19:28:51) and the loop stayed alive for most of
it, but the container logged **nothing for 53 s** (19:26:56 → 19:27:49) and
the 5 s poller missed 11 ticks. Zero bridge timeouts and zero failed tool rows
this cycle — nothing was in flight — but the mechanism is live.

Measured mechanism (`bench` on this box, `to_thread`, heartbeat on the loop):

| work shape in the worker thread | loop max gap |
|---|---|
| Python bytecode spin, 1.0 s | 0.02 s (interpreter switches every 5 ms) |
| `sum(range(60M))`, one C call | **0.71 s = the whole call** |
| `sorted(20M)` | 1.48 s |
| `pd.DataFrame(3M tuples)` | 0.66 s |
| groupby pct_change + rolling | 0.02 s |

A thread frees the loop only for work that re-enters the bytecode loop or
releases the GIL. One long C-level call holds it for its whole duration. The
existing guard `test_boot_compute_does_not_block_the_loop.py` uses
`time.sleep` as its stand-in, which releases the GIL — it cannot see this class.

The compute itself reads `price_history` with **no date bound**: 4,365,690 rows
for 509 tickers, joined in Python (`join_rows` is deliberately not `$lookup`),
to use only the latest date with 30-period lookbacks. A 90-day bound is 33,355
rows. Candidate fixes under measurement (`bench_sector_pin.py`, real function,
real Mongo, the one write patched to a recorder): thread vs process × full vs
bounded, plus sector-by-sector equality of bounded vs full output. Operator
instruction: measure first, then fix. Result to be appended here.
