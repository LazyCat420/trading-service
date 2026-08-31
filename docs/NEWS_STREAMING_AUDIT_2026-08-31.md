# News streaming commit f881a63 — what it actually does (audit, 2026-08-31)

Audit of `f881a63` ("feat(pipeline): stream news article scraping and emit
real-time feed progress") and its client twin `6fab6824`, run the same day the
commits landed. Method: full diff read + adversarial call-path trace at HEAD
(`pipeline_service.py`, `news_collector.py`). Companion plan:
`~/.claude/plans/also-can-we-run-magical-pascal.md` (benchmark ladder + audit
plan of record).

## Verdict: streaming telemetry, NOT pipeline overlap — REFUTED as concurrency

The intent was "agents process at the same time as scraping." That did not
happen. `pipeline_service.py:1270` still fully `await`s `run_scraper_sync()` →
`collect_all()` → `asyncio.gather(<all feeds>)` before trending, the
gatekeeper, or ANY agent runs. The commit added no `create_task` on the
pipeline side, so the critical path is unchanged:

    RSS sweep (blocking, ~1-2 min) -> trending -> screener -> gatekeeper
    -> per-ticker precollect -> agents

What the commit REALLY changed (all confirmed in the diff):

- Per-article results now upsert to Mongo and emit `news_scraped` events **as
  each article finishes** (previously batched after the 15-article gather).
  Streaming is real *within* collection — same wall-clock, earlier visibility.
- New `news_feed_progress` events per feed (completed/total), rendered by the
  client's news banner (`6fab6824`).
- Per-article body scrape: previously called **unbounded**
  `_scrape_article_body_via_service`; now `_scrape_with_timeout(..., 4.0)`
  (default also dropped 15s→4s). Foreign-text translation capped at 5s.
  These timeouts are the commit's only real latency levers.
- `safe_emit` grew a `data` dict payload; `discovery_emit` now also writes
  progress into `pipeline_state` (one `save_state()` per event).

Per-article work was ALREADY concurrent before the commit (15/feed gather,
`FEED_CONCURRENCY=5`), and all bodies still queue behind `scraper_client`'s
single global semaphore of 5 — measured ~105s/27-feed pass at concurrency
5/10/16 alike. That measurement comment was deleted by this commit; the number
still governs.

Note: explicit-ticker cycles (`tickers: [...]`) skip `collect_all` entirely
(`pipeline_service.py:1216`), so this commit only affects auto-discovery runs.

## Defects to fix before this deploys (riders)

1. **Fallback rate hidden**: the scrape-timeout log dropped from `warning` to
   `debug` exactly when the 4s cap will inflate the fallback rate it reports.
   Restore visibility (counter or warning).
2. **Freshness fabrication**: unparsable `published_at` now falls back to
   `datetime.now(UTC)` — a silent freshness corruption where the old code
   raised. Drop the article or mark the field, never fabricate now().
3. **Quality relaxation**: on scrape timeout the body falls back to the API
   summary, so a truncated `"..."` summary ≥150 chars can pass as an article
   body (old behavior: failed scrape ⇒ empty ⇒ quality-gated).
4. **Per-event state IO**: `discovery_emit` now runs
   `cls._state.update(...) + save_state()` on every article event.
5. **Unmeasured trade**: body-coverage rate at 4s vs 15s has not been measured.
   The benchmark plan's discovery rung (Rung D) measures it over DISTINCT urls
   before the timeout is accepted.

## Deploy state

At audit time the deployed container is `2b49241` (2026-08-30T18:05Z), 43
commits behind master — `f881a63` is pushed, NOT deployed. Keep the emit/data
plumbing; land the riders before or with the deploy.

## Open items found by the same audit (not fixed here)

1. **P1 — memory injection is dead at HEAD.** `app/services/memory/repository.py:27,49`
   imports `_ensure_schema` from `app/db/memory_repo`, deleted by `b6b29d3`
   (2026-08-18). Every `MemoryRetriever.retrieve` raises ImportError;
   `orchestrator.py:593` swallows it as "Memory retrieval failed (non-fatal)",
   so NO memory tier (canonical brief, episodic, semantic, procedural,
   prospective) has reached any agent prompt since 08-18. Writes and outcome
   grading still run. The only retriever test mocks
   `fetch_candidate_memories` — the exact seam that hides the break.
2. **P1 — `whiteboard_annotate` cannot match any entry.** The tool schema
   declares `entry_id: integer` (`app/tools/whiteboard_tools.py:165`) while
   entry ids have been `wb_<hex>` strings since the Mongo port
   (`app/agents/whiteboard.py:88`); `annotate()` looks up `{'id': entry_id}`.
   Annotations were the load-bearing cross-agent channel (ch.25/26).
3. **Docs/tree divergence — hold-wall release valves.** trading-client ch.102
   records `8dbef84` deployed 2026-08-26, but `8dbef84` is NOT an ancestor of
   master and the deployed container (`2b49241`) is master-lineage. Merge or
   retire branch `hold-wall-release`.

Fix plan and benchmark ladder: `~/.claude/plans/also-can-we-run-magical-pascal.md`.
