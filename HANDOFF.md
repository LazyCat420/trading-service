# HANDOFF — A broken tool is not an absence of information (2026-07-27)

Shipped `a4c763e` · `2bceb70` · `e5ad40b`, plus lazycat-sdk `a201770`.
**NOT DEPLOYED — deliberately.** See [Deploy](#deploy) below.

The CORAL wave's handoff (shipped `cd7f606`, deployed 09:14Z) is archived to
[`docs/HANDOFF_coral_repair_loop_2026-07-27.md`](docs/HANDOFF_coral_repair_loop_2026-07-27.md);
that work is unaffected by this one.

Full audit with every measurement:
[`../.agents/AUDIT-data-collection-cycle-v3-1785137616.md`](../.agents/AUDIT-data-collection-cycle-v3-1785137616.md).

---

## What this wave was

Audit the data collection behind `cycle-v3-1785137616` (SBUX STT AGNC ASC BOOT
NDAQ AMZN, 22m39s, **7/7 HOLD**) and fix what it found. CORAL was explicitly out
of scope.

---

## THE HEADLINE

**Three independent degradations were live, and the cycle recorded `SUCCESS` on
every phase for every ticker.**

DuckDuckGo had begun refusing our egress IP. Probed from inside the container:

```
lite.duckduckgo.com   ConnectTimeout  15.2s
html.duckduckgo.com   ConnectTimeout  15.0s
finnhub.io            200  0.2s          ← egress is fine
```

`lazy_web_search` had exactly one backend, so the junior analyst's only research
tool failed 8/8. Then STT's own desk note recorded the consequence:

```json
"data_gaps": ["DataGap: ... catalysts for STT (web search timeout)"],
"triage_recommendation": "QUANT_ONLY",
"_quality_flag": "good", "_quality_score": 81,
"_failure_patterns": ["FALLBACK_OUTPUT"]
```

The orchestrator honoured that triage and skipped the Fundamental Analyst. STT
and NDAQ each got a 526-byte stub with all five pillars `"Not analyzed"`, on the
stated grounds that no qualitative catalysts existed. **NDAQ then produced the
cycle's only BUY.** The quality scorer rated that artifact 81/"good" while it
carried its own `FALLBACK_OUTPUT` flag.

`research_degraded()` now refuses a shortening triage when the analyst's tools
failed. It can only ever *add* work, and fails open on a probe error.

---

## What measuring found that reading would not have

Three of the fixes changed shape once run against real data. This is the part
worth keeping.

**The new search was wrong on its first live run.** It worked — and returned a
**2012** Starbucks earnings-call transcript for "Q3 earnings catalyst", plus a
March article for STT. Both providers serve the archive for present-tense
queries. That is the *same* disease as the news table (ASC's "Recent News" ran
to 2024-07-24), and I would have shipped it into the fix for it. Results are now
newest-first with `age_days`, a 30-day window that widens rather than returning
nothing.

**The price bug was not the one I scoped.** "Widen the refresh past the 509
S&P names" was wrong. A live fetch showed `fetch_price_history` returning **250
rows with a stale tip**: yfinance serves the latest session as NaN OHLC with a
real Volume — Friday 07-24 was still NaN on Monday, for SBUX as much as for ASC.
`fetch_ohlcv_dataframe` drops such a bar and should. The 509 look fresh only
because the S&P post-close loop catches the bar while it is briefly complete.
The real defect was that **a partial success suppressed the fallback**: the
function returned on `count > 0`, and 250 is > 0. Compounding it, the Polygon
price fallback was gated on `POLYGON_API_KEY`, which is **empty** on the live
container — the key lives in `MASSIVE_API_KEY`, which is what the news rotator
had always read. Polygon served news all along while the price path skipped it.

Verified live: ASC, BOOT and AGNC each moved `2026-07-23 → 2026-07-24`,
`source='polygon'`.

**The AI anti-pattern was rewritten from samples, not guesses.** Phrase patterns
caught 28 of 60 rows. Sampling the misses found "physical AI", "top 10 AI
stocks", "Agentic Voice AI leader", "574% AI cloud revenue" — twelve for twelve
about the technology, none about C3.ai. So "AI" now inverts the burden: assumed
jargon unless something explicitly names the company.

---

## Ticker mis-attribution, measured

Rows stored over 7 days / rows whose TITLE contains the symbol:

| ticker | rows | title mentions | after the fix (60 sampled) |
|---|---:|---:|---|
| GOOGL | 800 | 93 | 1 survives (~the legit 12%) |
| **FCF** | **634** | **0** | **0** |
| AI | 452 | 205 | 5 |
| RH | 326 | 14 | **0** |
| BLSH | 127 | 0 | 0 |
| SBUX *(control)* | — | — | **2/2 kept** |

The extractor was handed raw HTML; Google News bodies carry base64 redirects and
uppercase runs mined out of them validate as real symbols. Sanitising happens at
the single `_detect_tickers_in_text` chokepoint — **not** the four call sites,
which any new collector could bypass.

The fan-out cap iterated a **set**, so which five rows survived was arbitrary: an
article about State Street's ETF was stored under `JPMpD/J/K/L` and `STT`, with
STT surviving on luck. `rank_tickers_for_fanout` now orders requested ticker →
headline mention → common stock → ETF → preferred/warrant. Nothing is dropped;
only the order changes.

End-to-end: the WSJ Naver story that logged
`['NVDA','035420.KS','GOOGL','000660.KS']` now yields `['035420.KS','NVDA']`.

---

## Traps for whoever is next

- **Google News RSS links are not followable.** A plain GET returns a 581 KB JS
  interstitial still on `news.google.com`. Those results carry an empty `url` on
  purpose — do not "fix" it by emitting the redirect, or `scrape_url` will burn
  a call on every one. Bing News RSS puts the real URL in a query param.
- **Bing's HTML endpoint is not usable** and is deliberately absent. A browser UA
  gets a JS shell; a text-browser UA gets navigational hits (starbucks.com,
  "Menu") behind `ck/a` wrappers that do not resolve.
- **`elapsed_ms = 0` still means "not measurable"**, not a 0 ms call. The SDK now
  times prism-internal tools from the `calling` event; if that event is missing
  it reports 0 rather than fabricating a duration.
- **`_is_missing_recent_session` fails CLOSED.** An unreachable DB must not make
  every ticker look stale and set off fallback fetches fleet-wide.
- **The old `test_precollect_outcomes` re-implemented the production rule inside
  the test file**, so it could not observe the `_ok`-after-deadline bug at all.
  The rule now lives in `classify_collector_outcome` and the test drives that.
  Watch for this shape elsewhere.
- **`fetch_price_history` counts now accumulate.** A fallback returning 0 must
  not erase yfinance's rows — callers read 0 as a total outage and
  `_EXPECT_TRUTHY` turns it into a manufactured collector error.

---

## Deploy

**Nothing here is deployed.** The working tree carried the CORAL session's
uncommitted work while this was being written, and the Dockerfile does
`COPY app/ ./app/` from the working tree — deploying would have shipped
half-finished code. Only my own files were committed.

That has since resolved: CORAL committed and pushed (`cd7f606`, `51036c8`) and
deployed at 09:14Z, and **the tree is now clean at `e5ad40b`**. A deploy now
would ship a coherent committed state. It was left to the operator by explicit
choice, not oversight.

`deploy.sh` also tars `../lazycat-sdk` to the NAS, so **the SDK timing fix
(`a201770`) ships with the next trading-service deploy** — no separate step.

**After deploying, run one cycle on the same seven tickers and check:**

1. `agent_tool_telemetry` — `lazy_web_search` failures should drop from 8/8, and
   failure rows should carry a non-zero `elapsed_ms`.
2. `news_articles` — no new `FCF`/`RH`/`BLSH` rows
   (`SELECT ticker, count(*) ... WHERE collected_at > <cycle start>`).
3. `pipeline_events` — `_late` warnings where `_ok` used to appear.
4. `data_source_status.last_success` should move off 2026-06-24.
5. `price_history` — non-S&P tickers reaching the latest session, `source='polygon'`.

---

## Still open

- **`cycle_audit_log` has written nothing since 2026-07-25** — zero rows for this
  cycle. Not diagnosed.
- `put_call_ratio` is SPY-only with 6 rows ever, and 07-25 duplicates 07-24's
  value to 16 decimal places — a weekend stale-fill stored as an observation.
- `insider_trades` covers 14 micro-caps (openinsider is a firehose, not a
  targeted feed). `build_alt_data_block` stays silent rather than fabricating.
- `social_posts` engagement counts are all null, so the block always reads
  "0 total engagements".
- `market_snapshots` appears abandoned — 819 rows, none written this cycle.
- The `congress_trades` future-dated row (2026-12-26) is **guarded at read**, not
  deleted.
- **Two pre-existing test failures** — `test_parameter_tools` and
  `test_tool_whitelists` — predate this work; confirmed against a clean stash.

1540 unit tests pass in trading-service, 112 in lazycat-sdk.
