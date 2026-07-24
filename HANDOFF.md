# HANDOFF — Cycle-audit fix wave (2026-07-24): datetime P1 + silent-failure surfacing + Mongo-layer repairs

Full-ecosystem audit of cycle `cycle-v3-1784876033` (last night's run) across
trading-service / scraper-service / lazy-tool-service / trading-client /
prism-service, plus a MongoDB consolidation audit. All 5 service links PASS and
all 4 deployments matched git. The cycle itself completed (LMT+VZ BUYs executed,
2 unheld SELLs correctly policy-blocked, 0 trade errors) — but the audit found
one P1 that degraded it and several silent-failure holes. All fixed below;
1141 tests pass.

## P1 — `datetime` shadowing destroyed data reports for NEW tickers

`app/v3/data_report.py` had a function-local
`from datetime import datetime, timedelta, timezone` inside the Mongo-thesis
branch (introduced by the read-flip commit 346c544). For any ticker with **no
prior analysis_results doc** (exactly the FreshnessGate "NEW" tickers) the
branch didn't run, so line ~362 `datetime.now(timezone.utc)` raised
UnboundLocalError and the **entire ~10k-char data report was replaced with an
error string** — the agents analyzed those tickers data-blind. In last night's
cycle that hit **VZ, BLK, DOG (3 of 6), including the executed $2.1k VZ BUY**;
11 tickers were hit over 24h, and agents stored the error string into their
memory stores. Fix: module-level import now carries `timedelta`; the shadowing
local import is deleted.

## Silent-failure surfacing (the "green but broken" holes)

- **Report-assembly failures now count**: `orchestrator.py` records a
  `data_report` failure into `collector_stats` and emits
  `v3_precollect_err_<ticker>` (status=error). Previously `collector_failures`
  read `[]` while 3/6 tickers ran data-blind.
- **Twitter sweep 0-posts is now a WARNING**: root cause is
  **`TWITTER_ACCOUNTS` env in scraper-service is empty** (twscrape has no
  credentials) — every sweep "succeeds" with 0 posts. NEEDS USER: real X
  account credentials in scraper-service env to actually fix.
- **Vision deep-read fallback gated off** (`VISION_DEEP_READ_ENABLED=False`,
  config.py): the standalone scraper image has no vLLM OCR stack, so every
  `engine="vision"` request was a guaranteed failure (~100 errors/24h). This
  path was already dead — reviving it means porting the OCR stack into
  scraper-service (open item, not attempted).

## Self-heal watchdog + evaluator repairs

- `scripts/self_healing_watchdog.py` fetched cycle JSONL logs via **ssh — but
  the container has no ssh binary** and `logs/` IS the NAS volume mount. Now
  reads `log_manager.CYCLE_DIR/<cycle>.jsonl` locally (ssh only as a dev-box
  fallback via `shutil.which`).
- Watchdog no longer classifies **policy outcomes as crashes**
  (`trade_rejected*/SELL_NO_POSITION/HOLD_NO_POSITION/HOLD_NO_SIGNAL/
  policy_blocked` filtered from error events).
- `judge_agent.py`: a Mongo **miss** on llm_audit_logs no longer counts as a
  hit — falls through to PG. The mirror only keeps 14 days (TTL), PG has full
  history; this was the "Log not found" ×8.

## Mongo layer

- `rlm_audit.py` context_blobs mirror docs now carry `created_at`, written via
  new `upsert_doc(..., insert_only=True)` (`$setOnInsert` — matches PG's
  ON CONFLICT DO NOTHING; `$set` would have clobbered timestamps on dedup).
- `mongo_store.ensure_indexes`: added `trade_results (cycle_id, ticker)` index
  (every write/read uses that key; was collection-scanning). Corrected the
  **false TTL comment** on llm_audit_logs: PG does NOT age that table
  (AUDIT_LOG_TTL_DAYS only rotates files). DECISION NEEDED before full mongo
  cutover of llm_audit_logs: drop the 14d TTL or accept losing >14d history.
- **Backfill top-up run 2026-07-24** for the fixed-window gap (07-22 22:41 →
  07-23 02:23 UTC, rows written between last backfill and the dual-flag
  redeploy) across execution_errors, cycle_audit_log, agent_audit_log,
  agent_traces, agent_tool_telemetry, v3_agent_telemetry, trade_results,
  ticker_reports, analysis_results, context_blobs. Backups first:
  `/volume1/docker/trading-service/backups/2026-07-24-audit/` (pg_dump +
  mongodump of analysis_results + row-count snapshot).
- **analysis_results legacy dupes** (44 (cycle_id,ticker) groups, newest
  2026-05-29, present in BOTH stores) deduped keeping newest — see backups.

## Scheduler

- **YouTube nightly channel sweep never ran**: the in-memory scheduler
  registered the 1:30 AM PT cron AFTER the slot on restart nights (observed:
  container up 01:16, scheduler up 01:43). Added missed-slot catch-up: if boot
  lands within 6h after 1:30 PT, a one-shot run fires 3 min after startup
  (transcripts dedup on video_id, double-run is a no-op).

## Tests

- `tests/test_scraper_client.py` rewritten to mock the **httpx seam** — the old
  tests patched `app.scraper.service` (no longer imported) and
  `test_scrape_success` was doing a LIVE scrape of example.com through the
  production scraper; the other tests passed only because connection failures
  coincidentally matched failure-path assertions.

## Cross-service notes (no code change)

- **lupos-bot is DOWN 31h+** — crash-looping on missing `LUPOS_TOKEN` (vault
  has no such secret). NEEDS USER: provide the token.
- **API_SERVER_KEY three-way drift**: deployed trading-service =
  `lazycat-secure-…-2026` (from vault master, synced by deploy.sh — redeploys
  are safe); lazy-agent-service compose hardcodes a DIFFERENT
  `API_SERVER_KEY` in `environment:` (unused by the bridge, which
  authenticates via `TRADING_SERVICE_API_KEY` from deploy-kit/.env.deploy —
  currently matching). Local `.env` was stale (`diagnostics_key_123`) → synced.
  Rotation of any of these keys must touch vault + deploy-kit together.
- **prism E11000 requestId dupes** (32×/24h): lazycat-sdk retry path re-sends
  `requestId-N` suffixes; caller-side fix belongs in lazycat-sdk retry
  telemetry. Not fixed this wave.
- trading-client got clickable-tickers-everywhere (see its HANDOFF; deployed +
  click-tested live).
