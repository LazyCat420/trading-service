# Audit: what the desks are actually fed, and what the collectors waste

Read-only audit of trading-service (no edits, no deploys). Mongo queried via
`.venv/bin/python` against `cycle_audit_log` (`trading_bot` DB), window =
last 14 days from 2026-09-06 (`>= 2026-08-23`). Source of truth for the
scraper is `app/scraper/` in this repo (not the `scraper-service` build
artifact).

**Matching rule** (the "label = background" trap): every count below is
driven off a literal, code-verified log-message prefix
(`[scraper_client] Scrape failed for`, `[rotator][DROP] dropped`, `[V3] Data
report for ... hard-truncating`, `[V3Runner] ...: non-sheddable context`),
never off a bare English word. `cycle_audit_log` is populated by exactly one
writer — `app/services/logging/unified_logger.py:DbLoggingHandler`, attached
to the root logger at **WARNING** level — so every count in this report is a
**WARNING+ event only**; several relevant lines are logged at DEBUG/INFO in
the source (news_collector's per-article `[news][DROP]`, `failure_cache`'s
`record()`/`check()` hits) and are **invisible to this collection**. Called
out explicitly wherever it matters.

---

## 1. Failure rates by source

### 1a. Body-scrape failures (`[scraper_client] Scrape failed for <url>: <reason>`)

145,031 WARNING lines in 14 days, but only **3,214 distinct URLs** —
**97.8% of the volume is retries of URLs already known to fail.** Per the
repo's own lesson ("a retry count is not a failure rate"), the table below
is DISTINCT URLs; total lines (incl. retries) shown alongside so the waste is
visible.

| host | lines (incl. retries) | distinct URLs | retries/URL |
|---|---:|---:|---:|
| finnhub.io | 143,770 | 2,793 | 51.5 |
| www.investors.com | 275 | 24 | 11.5 |
| seekingalpha.com | 184 | 99 | 1.9 |
| thestockmarketwatch.com | 103 | (not sampled this window) | — |
| www.zacks.com | 80 | 73 | 1.1 |
| biztoc.com | 71 | 37 | 1.9 |
| www.thestreet.com | 28 | 15 | 1.9 |
| www.ft.com | 25 | 15 | 1.7 |
| www.tradingview.com | 18 | 18 | 1.0 |
| www.bloomberg.com | 17 | 13 | 1.3 |
| www.marketwatch.com | 17 | 17 | 1.0 |

`finnhub.io` (finnhub's own article-redirect URLs, `finnhub.io/api/news?id=...`,
fetched by the body-upgrade path for finnhub-sourced articles) is **98.6% of
all scrape-failure lines** and **86.9% of all distinct failed URLs.** The
worst single URL was retried **1,545 times over 2.5 days** (median gap
82-106s between retries — this is a live retry loop, not once-a-cycle
re-checks). Reason breakdown for finnhub.io: "no engine returned usable
content" 50,564, "thin content" 46,935, **"None" (no reason at all) 45,455**,
"URL is permanently unavailable" 657.

Comparison success counts (distinct URLs stored to `news_articles`,
`source=finnhub`, 14d): **4,192**. Against 2,793 distinct failed URLs, a
rough (overlapping-set, see caveat) attempt count of ~6,985 gives yield
≈0.60 — but a failed body-scrape does NOT necessarily mean the article was
lost (the API summary is used as a fallback unless it's *also* too short),
so this column undercounts true successes and the two sets are not disjoint.
Flagged UNVERIFIED-precise; the clean number is the one above it: 2,793
distinct URLs that will NEVER succeed on retry and are retried anyway.

### 1b. Rotator API-provider drops (`[rotator][DROP] dropped '<title>' from <provider> — truncated/paywalled (len=<n>)`)

2,432 WARNING lines / 14d. Same retry trap applies at the title level (no
URL in this message) — distinct titles per provider shown, with total lines
alongside. "Attempts" = distinct-titles-dropped + distinct articles that
DID land in `news_articles` for that provider in the same window (a true
success proxy, since `_persist_articles` filters `is_truncated_content`
*before* the fanout/dedup that produces those rows).

| provider | success (distinct URLs, 14d) | drops (distinct titles) | drop lines (incl. retries) | attempts | yield |
|---|---:|---:|---:|---:|---:|
| alphavantage | 1,143 | 8 | 9 | 1,151 | 0.993 |
| polygon | 447 | 1 | 1 | 448 | 0.998 |
| worldnewsapi | 466 | 13 | 19 | 479 | 0.973 |
| currentsapi | 443 | 178 | 610 | 621 | 0.713 |
| stockdata | 82 | 45 | 47 | 127 | 0.646 |
| marketaux | 134 | 78 | 85 | 212 | 0.632 |
| newsapi | 342 | 205 | 718 | 547 | 0.625 |
| gnews | 54 | 43 | 547 | 97 | 0.557 |
| **thenewsapi** | **0** | 3 | 396 | 3 | **0.0** |

Ranked by wasted attempts (drop lines, i.e. repeated fetch+reject cycles):
**newsapi (718) > currentsapi (610) > gnews (547) > thenewsapi (396) >
marketaux (85) > stockdata (47) > worldnewsapi (19) > alphavantage (9) >
polygon (1).** `thenewsapi` and `gnews` both show large retry multipliers
(132x and 12.7x respectively) — the SAME 3 headlines (thenewsapi) or 43
headlines (gnews) are being re-served and re-rejected on almost every fetch,
because the persistence-time truncation check runs *before* the dedup
check, so a provider re-serving the same rejected story every poll pays the
full drop-and-log cost every single time.

---

## 2. The hard-truncation

Cap: `_MAX_DATA_REPORT_CHARS = 4900` — `app/v3/data_report.py:459`.
Fires (and logs) at `app/v3/data_report.py:132-140`:

```python
if len(report) > cap:
    logger.warning(
        "[V3] Data report for %s exceeded %d chars (%d) — hard-truncating",
        ticker, cap, len(report),
    )
    report = report[: cap - 100] + "\n\n[... DATA REPORT TRUNCATED — full data available via tools ...]\n"
```

**98 fires / 14 days** (58 distinct tickers; NVDA 10x, JPM 7x, LULU 6x, ZS 5x
were the most frequent). Overage (`actual - cap`): min 4 chars, **median 259
chars (5.3% of the 4,900 cap)**, max 1,021 chars (20.8%). So on its own,
this specific cut is almost always a small **tail trim**, not "a third of
the report" — but that's because `assemble_report()` already runs a
**content-aware** budget pass before this fires (news section shrinks to
make room for a reserved social floor; sections 4-6 truncate in priority
order with their own `[... SECTION TRUNCATED ...]` markers when they don't
fit). The 4900-char hard cap is a **blind, non-content-aware safety net on
top of that** — a flat `report[:cap-100]` slice with no idea where a section
boundary is.

**Reconstructed on the exact cycle named in the task**
(`cycle-v3-1788682529`, EXLS, 2026-09-06 08:18:16 UTC — confirmed live in
`shared_desk`, `desk_data.cycle_metadata.data_report`, final stored length
4,866 chars against a pre-cut length of **4,942**, matching the task's
own example verbatim):

- The safety net fired mid-**Institutional Fund Holdings** list, cutting the
  text off mid-word (`"Point72 Asset Management: 540,554 shar"`).
- Sections **5 (Reddit) and 6 (YouTube) never appear in the report at all** —
  not because this cut removed them, but because the earlier
  `assemble_report` social-section loop (`for section_text, section_name in
  social_sections: if budget_remaining <= 0: break`) had already run out of
  budget by the time it reached them (Institutional consumed what was left).
  **YouTube had 32 real transcripts on file for EXLS** (checked directly
  against `youtube_transcripts`) that never reached the desk — the same
  defect the code's own comment cites as measured historically ("0/187
  recent context blobs"), still reproducing today.
- Section 3 (News) shows "(9 records, showing up to 20)" then only 2
  headlines before `[... news trimmed — full feed via tools ...]` — 7 of 9
  collected articles for this cycle never reach the model's context at all,
  even though they were successfully stored.

So: **the 4900-char cap IS content-aware in its primary mechanism**
(section priority + per-section trim markers), but the **backstop that
actually fires the WARNING is not** — it doesn't know it's mid-word,
mid-list, or that two whole sections were already dropped upstream by the
budget it's now blindly re-cutting.

---

## 3. The 2048-token embedder route

Constant: `_EMBED_TOKEN_LIMIT = 2048` — `app/v3/agent_runner.py:1134`.
Decision + log: `app/v3/agent_runner.py:1198-1221`. Comments at
`:1124-1165` and `app/agents/base_agent.py:572-573` explain the mechanism:
Prism's server-side "workflow-query" embeds the LLM call's **user message**
with embeddinggemma for its own agent-memory system; embeddinggemma has a
**hard 2048-token positional limit and crashes** if fed more. `agent_runner`
avoids the crash by relocating the whole "dynamic block" (data report,
memory context, portfolio context, whiteboard summary, etc.) from the user
prompt into the **system prompt** instead — which Prism does **not** embed.

**Cost — confirmed from the code, not a memory-retrieval skip.** Nothing in
the dynamic block is dropped; the log line itself says "shed section(s)
restored" — anything shed to try to fit the user-message budget is put back
once the decision is made to relocate instead. The stated cost is purely
**loss of KV-cache prefix reuse** (the system prompt becomes bespoke per
call instead of a shared, cacheable prefix), i.e. **a slower path, not a
retrieval fallback.** This audit did not independently re-measure the
latency delta; it is asserted in the code's own comments (measured
prefix-cache hit rate ~84%, chars/token empirically 1.88 not the ~3 the
active guard uses — the guard's author left the char/token divisor
deliberately wrong, on record, because tightening it would *increase*
relocation frequency and cost more than the embed overflow it defends
against).

**1,579 fires / 14 days.** Per-agent (fires vs. `v3_agent_telemetry` run
count `n`, same 14d window):

| agent | fires | runs (n) | fire rate | min tok | median tok | max tok |
|---|---:|---:|---:|---:|---:|---:|
| v3_regime_engine | 249 | 304 | 82% | 2,994 | 3,282 | 3,732 |
| v3_decision_synthesizer | 211 | 130 | 162%* | 6,621 | 10,555 | 13,741 |
| v3_junior_analyst | 147 | 132 | 111%* | 2,869 | 3,523 | 4,580 |
| v3_quant_analyst | 132 | 129 | 102%* | 5,337 | 6,973 | 9,649 |
| v3_bull_agent | 125 | 118 | 106%* | 5,703 | 8,841 | 11,067 |
| v3_bear_agent | 122 | 116 | 105%* | 6,604 | 9,398 | 11,613 |
| v3_bull_defense | 120 | 115 | 104%* | 7,254 | 8,857 | 11,083 |
| v3_board_of_directors | 119 | 110 | 108%* | 8,865 | 10,886 | 13,563 |
| v3_debate_judge | 116 | 114 | 102%* | 7,503 | 9,567 | 11,862 |
| v3_fundamental_analyst | 115 | 108 | 106%* | 3,937 | 4,939 | 6,225 |
| v3_valuation_analyst | 109 | 99 | 110%* | 5,508 | 7,585 | 10,776 |
| v3_delta_analyst | 14 | 14 | 100% | 2,444 | 2,656 | 3,137 |

\* >100% fire rate means the same agent-run refires the log more than once
(retries rebuild the prompt each attempt, so a retried call re-triggers the
routing decision) — this reads as OVER 100%, not as evidence of undercounting.

**Is there any agent whose context fits under 2048 tokens? No — not once,
in 1,579 observed fires across 12 named agents over 14 days.** The lowest
non-sheddable floor ever observed is 2,444 tokens (`v3_delta_analyst`), 19%
over budget, and that agent still fires on 100% of its 14 runs. Every other
named agent's floor starts at 2,869-8,865 tokens (1.4x-4.3x over budget).
`v3_regime_engine` is the only agent with headroom for the "fits, keeps
KV-cache" branch to ever matter (82% fire rate, so ~18% of its runs
presumably shed enough sheddable content to duck under 2048 — this audit
cannot confirm that from WARNING-level logs alone, since a successful shed
logs at INFO and is invisible to `cycle_audit_log`). For the other 10 named
agents, the "fits without relocation" branch (`agent_runner.py:1198-1199`)
is **de facto dead code in production** — their non-sheddable core is
routinely 3-6x the budget.

`contradiction_shadow` (198 runs/14d) never appears in the routing_full
breakdown and its telemetry shows `sys_prompt_chars=0`/`user_prompt_chars=0`
— it does not go through this prompt-building path at all (different phase,
`post_decision`), so it is out of scope for this question, not evidence of
fitting.

---

## 4. Paywalled sources (len=0)

From the rotator DROP breakdown (1a): **len=0 share of drops** —
thenewsapi 396/396 (100%), gnews 539/547 (98.5%), marketaux 69/85 (81%),
stockdata 38/47 (81%), currentsapi 349/610 (57%), newsapi 175/718 (24%),
worldnewsapi 0/19, alphavantage 3/9, polygon 0/1.

**`thenewsapi` is the dead source named per the instruction.** 396
WARNING-level drops in 14 days collapse to **3 distinct headlines**
(retried ~132x each), and **0 articles from `thenewsapi` landed in
`news_articles` in the same 14-day window** (6 rows exist all-time, none
recent). It is still being queried every cycle and paying the full
scrape+reject cost every time for content that has not once cleared the
quality gate in two weeks.

`gnews` is a close second by len=0 rate (98.5%) but is **not** dead — it
still yielded 54 distinct articles in the window (yield 0.557), just at a
worse rate than every other provider except `thenewsapi`.

**Caution, per the failure_cache module's own documented case study**: a
domain-level "0% success" reading can be wrong if it's dominated by one or
two immortal dead URLs rather than a genuinely bad domain (measured there:
`thestockmarketwatch.com` looked like a 0%-success source at the domain
level but was actually 5/5 on its *current* URLs — a single 410'd article
retried 157 times was the whole story). `www.investors.com` (257/275
scrape-failures = "domain yields no articles" skip, 24 distinct URLs) is a
similar-shaped candidate worth the same distinct-URL scrutiny before anyone
proposes dropping it — not done here, flagged instead.

---

## 5. What reaches the desk — cycle-v3-1788682529 (EXLS), 2026-09-06

Reconstructed directly from `shared_desk.desk_data` (JSON-text field) →
`cycle_metadata.data_report`, the literal string every V3 agent for this
cycle read (see full text saved alongside this report at
`scratchpad/exls_data_report.md`). Final length 4,866 of the 4,900 cap
(pre-cut 4,942 — this is the exact "(4942)" cycle from the task prompt).

| section | source | content vs. filler |
|---|---|---|
| Header + "WHY YOU WOKE UP" | watch_desk trip | real (1 sentence, insider-sell headline) |
| "PRIOR RESEARCH ON FILE" | `analysis_results` | **not a thesis** — the stored "prior research" is a pipeline-failure string: `"Pipeline ended at DeskPhase.INIT without producing a decision: Invalid transition: INIT → PM_DONE..."`. A prior cycle's crash message is being fed to this cycle as if it were research to build on. |
| PRIOR TRADE HISTORY | `decision_outcomes` | real, EXLS-specific (5 trades) |
| CONFIDENCE CALIBRATION | fleet-wide stats | real, but generic (not ticker-specific) |
| LESSONS (0.b) | `evolution_lessons` | real, but generic (3 boilerplate audit lessons, none EXLS-specific) |
| 1. Market Data & Fundamentals | yfinance | real — P/E, fundamentals row, 4 quarterly + 4 annual financials, all populated |
| 2. Technical Indicators | yfinance/technicals | real — RSI/MACD/SMAs/Bollinger/ADX all populated |
| 3. Recent News & Sentiment | finnhub + multi-API + RSS | real but **7 of 9 collected articles never shown** (news_budget trimmed to 2 headlines + "[... news trimmed ...]") — while in the SAME cycle, 8 body-scrape attempts failed (thestreet, seekingalpha x2, zacks, marketscreener, sahmcapital x3) and 4 API articles were dropped as paywalled/truncated (currentsapi, newsapi x2, stockdata) before they could even reach the "9 records" count. |
| 4. Institutional Fund Holdings | `fund_scanner` | real (9 funds, $275.7M), **cut off mid-word by the hard-truncation safety net** |
| 5. Reddit | `reddit_posts` | absent — 0 rows exist for EXLS (no loss here) |
| 6. YouTube | `youtube_transcripts` | **absent from the report despite 32 real transcripts on file** — excluded upstream by budget exhaustion, not by the final cut |

Same-cycle event count matches the task's own numbers exactly: **8**
`[scraper_client] Scrape failed` warnings, **4** `[rotator][DROP]` drops,
**1** hard-truncation (4900→4942), and **11/11** V3 agents that ran
(regime_engine, junior_analyst, fundamental_analyst, quant_analyst,
valuation_analyst, bull_agent, bear_agent, bull_defense, debate_judge,
board_of_directors, decision_synthesizer) fired "routing FULL" — literally
every agent in this cycle, confirming section 3's fleet-wide finding on the
one cycle named in the prompt.

---

## Defects (ranked)

1. **HIGH — `failure_cache` never remembers the two failure modes that
   dominate the waste.** `app/scraper/core/failure_cache.py` docstring states
   its own purpose ("a dead URL was re-walked through every engine on every
   cycle, forever") and its own scope ("permanent failures only... 404/410").
   `app/scraper/engines/auto_engine.py:153-157,207-208` confirms `record()`
   is called **only** for `PERMANENT_STATUSES` (404/410). "Thin content" and
   "no engine returned usable content" — 97,943 of 145,031 scrape-failure
   lines in 14 days (67.5%) — are **never cached**, even though for a
   fixed, ID'd `finnhub.io/api/news?id=...` page (not a paywall, not a
   rate limit — a static proxy for one article) that content is not going to
   change. Measured result: one URL retried **1,545 times over 2.5 days**
   (median 82-106s apart); 2,793 distinct finnhub.io URLs will never
   succeed and are retried anyway; 99.1% of all scrape-failure volume is
   waste by this exact mechanism the cache exists to prevent.

2. **HIGH — Playwright's short-content path returns `error=None`, and
   AutoEngine's fallback only backfills a message when `success` was still
   `True` on entry.** `app/scraper/engines/playwright_engine.py:259-268`:
   `success=bool(content and len(content) > 100)` with `error=None`
   unconditionally — so a short/empty (but non-exception) Playwright result
   is `success=False, error=None`. `app/scraper/engines/auto_engine.py:224-229`
   only sets a default error message `if res.success:` (the bot-wall case,
   where Playwright wrongly reported success on an interstitial) — it never
   fires for a result that was already `success=False`. The literal string
   `"None"` reaches `cycle_audit_log` as the "reason" **45,533 times in 14
   days (31.4% of all scrape-failure lines)** — the single largest reason
   bucket after "no engine returned usable content", and it carries **zero
   diagnostic information**: no distinction between a bot-wall, a genuinely
   empty page, a JS error, or a network hiccup.

3. **MEDIUM — the failure_cache's cross-worker SQLite store repeatedly
   disables itself.** `[failure_cache] shared store unavailable at
   /app/logs/failure_cache.db` fired **318 times in 14 days**
   (`app/scraper/core/failure_cache.py:183-191`). Each firing means the
   already-narrow 404/410 memory falls back to in-process-only, so even the
   one failure mode the cache DOES track stops being shared across the
   container's uvicorn workers for that instance's lifetime.

4. **MEDIUM — `thenewsapi` is a scheduled failure.** 396 WARNING-level
   drops / 14d on 3 distinct headlines, 0 articles reaching `news_articles`
   in the window (6 rows all-time, none recent). It is actively queried and
   immediately discarded on essentially every call.

5. **MEDIUM — the 4900-char hard-truncation safety net is not
   content-aware, and fires after the content-aware pass already ran.**
   `app/v3/data_report.py:132-140` is a blind `report[:cap-100]` slice.
   Observed on cycle-v3-1788682529: cuts mid-word inside Institutional Fund
   Holdings, on a report that had already silently dropped Reddit/YouTube
   upstream (YouTube had 32 real transcripts for EXLS that never reached the
   desk). Typical impact is small (median 5.3% of the cap) because the
   upstream per-section budgeting in `assemble_report()` already did the
   heavy trimming — but the mechanism itself has no way to know that, and no
   way to prefer cutting a less important trailing section over a more
   important one once it fires.

6. **INFORMATIONAL — the 2048-token embedder route is not a rare edge
   case, it is close to the default path for every heavy V3 agent.**
   1,579 fires/14d against 1,489 runs (excluding `contradiction_shadow`,
   which doesn't use this path) — fire rates 82-162% per named agent (>100%
   from retries re-triggering the decision). No agent's non-sheddable
   context was ever observed under 2,048 tokens; the floor for 10 of 12
   named agents starts at 2,869-8,865 tokens. The "fits in the user
   message, keeps KV-cache" branch (`agent_runner.py:1198-1199`) is de facto
   dead code for every decision/debate agent (bull/bear/bull_defense/
   debate_judge/board/decision_synthesizer/valuation/quant/fundamental/
   junior). This is a cost finding (documented in-code as a KV-cache/latency
   cost, not a data-loss one), not a correctness bug — flagged as
   informational per the task's own framing ("if none do, the branch that
   fits is dead code and the message is noise").

## Notes / caveats

- All counts are WARNING+ only (`cycle_audit_log`'s one writer is
  attached at that level). `news_collector.py`'s per-article `[news][DROP]`
  logs at DEBUG and its aggregate summary at INFO — both invisible here;
  finnhub/yfinance-path drops (`_note_drop("finnhub"/"yfinance", ...)`)
  cannot be measured from this collection at all, only the rotator's
  (`news_api_rotator.py`) WARNING-level drops could be counted directly,
  which is exactly the set the task's own example cycle showed.
- Section 1a's finnhub.io "yield" (≈0.60) is explicitly flagged
  UNVERIFIED-precise: the failed-body-scrape set and the successfully-stored
  set are not disjoint (API-summary fallback can still land a row after a
  failed body scrape), so a simple sum overstates "attempts."
- Timestamps in `cycle_audit_log` are stored as naive UTC (`datetime.now(timezone.utc)`
  written, read back tzinfo-naive by pymongo) — all queries here compare
  against naive `datetime.utcnow()`.
- Scope: this audit stayed inside `trading-service`. `scraper-service` is a
  separate sibling repo/container that build-copies `app/scraper`; its
  runtime behavior was inferred from this copy (the stated source of
  truth), not independently re-read.
