# Telemetry, Deduplication, and Outcome Resolution Hardening — September 15, 2026

## Executive Summary
Audited and resolved 4 critical defects across telemetry, event-loop non-blocking concurrency, outcome contract resolution, and MongoDB dataset integrity in `trading-service`. All fixes were verified via unit tests, merged into `master` (`da926d9a`), pushed to GitHub, and deployed to the Synology NAS container.

---

## 1. Resolved Defects

### 1.1 Completion Tokens Telemetry Loss in Audit Trail
- **Symptom**: `completion_tokens` was logged as `0` and `tokens_per_second` was `None` in `llm_audit_logs`, despite model endpoints returning complete completion tokens.
- **Root Cause**: In `app/v3/orchestrator.py:_persist_trade_verdict()`, the call to `log_rlm_audit_trail()` forwarded `prompt_tokens` but omitted `completion_tokens`.
- **Fix**: Added `completion_tokens=sum(int(e.get("completion_tokens") or 0) for e in _telemetry)` to `log_rlm_audit_trail()`.
- **Verification**: Unit test `test_completion_tokens_and_tok_per_sec_in_rlm_audit` confirmed `completion_tokens` and `tokens_per_second` are accurately computed and persisted.

### 1.2 Scraper Service Queue Deadlines & Non-Blocking Async Ticker Extraction
- **Symptom**: `_scrape_with_timeout()` was waiting in semaphore queues while `CompanyRegistry` iteration in `ticker_extractor.extract_tickers` blocked the asyncio event loop for up to 177 seconds during news sweeps.
- **Root Cause**: `CompanyRegistry` was unsynchronized and lacked thread-safety, preventing `asyncio.to_thread` offloading. Additionally, `scraper_client.py` ignored caller-provided `options["timeout"]`.
- **Fix**:
  - Added `threading.RLock()` and safe snapshot methods `name_items()` and `alias_items()` to `CompanyRegistry` in `app/processors/ticker_extractor.py`.
  - Offloaded CPU-heavy extraction via `matches = await asyncio.to_thread(extract_tickers, text, title=title, source=source)` in `extract_and_validate()`.
  - Updated `scraper_client.py` to forward `options["timeout"]` to `httpx.AsyncClient`.
- **Verification**: Unit test `test_company_registry_thread_safety_and_snapshots` confirmed thread safety under concurrent mutation and reading.

### 1.3 Outcome Resolution Contract BF1 Bug & Stranded Decisions Backfill
- **Symptom**: 76 decisions were permanently `STRANDED` in `outcome_resolution_health.py` and zero outcomes had resolved in >10 days.
- **Root Cause**: In `scripts/backfill_outcome_contract.py`, `decision_as_of` was set to the bar date `when` (at `00:00:00 UTC`). Because `closed_bar_cutoff(dt)` returns prior midnight when `dt` is before 16:15 NY time, `closed_bar_cutoff(when)` was always strictly less than `when`, permanently failing `verified_pair()`.
- **Fix**:
  - Repaired `scripts/backfill_outcome_contract.py` to set `decision_as_of = ca` (created_at timestamp).
  - Executed backfill with on-disk undo log (`outcome_contract_undo.json`).
  - Repaired 73 decisions, dropping `STRANDED` from 76 to 3.
  - Resolved 21 mature decisions against historical price data with 0 errors.

### 1.4 Database Safety Dumps & Massive Deduplication
- **Symptom**: 372k duplicate rows in `asset_prices` and 2k duplicate rows in `insider_trades` inflated database sizes and caused duplicate index errors on startup.
- **Pre-Execution Safety**:
  - Dumped full safety archives directly on Synology NAS host:
    - `/volume1/docker/backups/asset_prices_backup_20260914.archive` (63 MB, 379,446 docs)
    - `/volume1/docker/backups/insider_trades_backup_20260914.archive` (866 KB, 2,451 docs)
  - Generated JSON undo logs locally before deletion.
- **Execution**:
  - `scripts/dedupe_asset_prices.py --apply --build-index`: Deleted 372,138 redundant docs, reduced collection from 379,446 to 7,308 docs, and built `natural_key_unique` index.
  - `scripts/dedupe_insider_trades.py --apply --build-index`: Deleted 2,090 redundant docs, reduced collection from 2,451 to 361 docs, and built `natural_key_unique` index.
  - Declared `natural_key_unique` in `ensure_indexes()` in `app/db/mongo_store.py`.

---

## 2. Production Deployment & Verification
- **Commit**: `da926d9a` fast-forward merged to `master` and pushed to `LazyCat420/trading-service`.
- **Container Deployment**: Deployed via `npm run deploy` to Synology NAS (`10.0.0.16:3031`).
- **Container Health**: Inspected Docker container status: `healthy` (`HTTP 200` on `/health`).
- **Index Verification**: Confirmed unique constraints on `asset_prices` and `insider_trades`.

---

## 3. Remaining Focus Areas from System Documentation
1. **Decision Synthesizer Context Efficiency & Redundant Prefill Reprocessing**:
   - Deliver unified, versioned evidence packet to `v3_decision_synthesizer` to prevent 18 whiteboard read loops.
2. **Headless / Proxy Fallback for Bot-Walled News Sites (`scrape_url`)**:
   - Provide headless rendering or proxy fallback when news publishers enforce Cloudflare bot protection.
3. **Long-Term Learning System Revamp (Releases 1–5)**:
   - Quarantine invalid autonomous skills, implement durable outbox and idempotent lesson writer.
