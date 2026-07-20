# HANDOFF — Grounded news extraction, CriticGate, honest collector stats

**Date:** 2026-07-20 (follows the self-healing wave below)
**Deployed:** trading-service `6e6c5f9` + lazy-agent-service `5d738e0`
**Tests:** 857 unit passing (trading-service), 491 (lazy-agent-service)
**Verified by full live cycle** `cycle-v3-1784528463`: 5 tickers, 2 BUYs executed,
0 collector errors, 0 CriticGate errors, salvage pass recovered ticker V.

## What shipped

1. **Grounded news extraction** (`app/services/news_extraction.py`) — google/langextract's
   METHOD (few-shot + char-offset quote grounding, drop-unaligned facts), not the library.
   Agents were getting raw scrape (avg 2,324 chars × 15 articles ≈ 9k tokens/ticker; 0 of
   4,923 recent articles had llm_summary). Facts cached in `news_articles.grounded_facts`;
   `[] = no substance (cached)` vs `None = retry later`. Kill switch:
   `NEWS_GROUNDED_EXTRACTION=false`. Wired into `get_finnhub_news` (finance_tools.py).
   WATCH ITEM: with 5 tickers extracting concurrently the 22s batch budget stretched to
   40s (GPU queue + loop contention); deferral handled it and cycle time was unchanged
   (1,225s vs 1,224s prior), but consider `NEWS_EXTRACT_BATCH_BUDGET_S=15` if precollect
   tightens.

2. **CriticGate fix** (lazy-agent-service `VllmModelSyncService`) — prism's CriticGate
   ignores `criticProvider` and runs the critic on the CONVERSATION's provider, so any
   pinned model 404s on the other vLLM host (869 errors/24h, 8-12s wasted per DANGER
   tool call, then blind approve). Only correct pin is EMPTY (gate falls back to the
   conversation's own model); the sync daemon now enforces that and no longer manages
   the critic role. Verified: 0 errors during the audit cycle.

3. **Collector honesty** (`app/v3/collector_stats.py`) — "timed out at 45s but still
   collecting" now rides in `collector_late`/`collector_late_names`, NOT
   `collector_error`/`collector_failures`. The audit cycle read
   `ok=15 error=0 late=15` instead of the old "15/15 failed".

## Cycle audit notes (cycle-v3-1784528463)

- META HOLD_NO_SIGNAL · JPM BUY $3,087 ✓ · AXP BUY $3,087 ✓ · V BUY policy-blocked
  (low confidence — gate working) · RH SELL skipped (no open position; agents sold an
  unheld ticker — counted as trade_failed:1 + no_position_blocked:1, correctly guarded).
- Salvage pass (from `7a1bf1b`) fired in production for V's junior_analyst (475-char
  unparseable output) and REPAIRED it — first live save.
- Discovery now pulls MarketWatch (was 401-blocked before the header fix in `0677ae9`).

---

# HANDOFF — Self-healing repair loop: bounds, budget, and code evidence

**Date:** 2026-07-19
**Deployed:** `b099238` → synology, container healthy, boot sequence clean
**Commits:** `b50611a` (bounds + context gate) → `2cfed77` (code evidence) → `b099238` (schema hotfix)
**Tests:** 817 unit passing, 5 skipped (pre-existing, DB-gated)

---

## Why this wave happened

The ask was to add LSP-based code intelligence to the trading cycle for efficiency.
Exploration changed the shape of that substantially:

1. **The LSP engine already exists.** `tools-service` has ~2,450 lines of production
   LSP (pyright/tsserver/rust-analyzer/gopls). It must not be rebuilt, and per house
   rule it is read-only.
2. **It cannot see this tree.** `tools-service/docker-compose.yml` mounts only a
   timezone file — no repo volumes. Fixing that means editing a read-only repo.
3. **The real bottleneck was elsewhere.** The repair path had no budgeting, no
   scheduling, no failure taxonomy, and a byte-truncating source reader.

So the work landed caller-side in `trading-service`, and LSP was deferred (see
"Phase 3 deferred" below for the measurements that justify deferring it).

---

## 🔴 The most important thing in this wave

**The repair loop could destroy the file it was asked to fix.**

`deploy_fix_to_disk()` wrote the model's output straight over real source with
`file_path_obj.write_text(proposed_fix)` — no guard. The proposer is told to output
a **COMPLETE rewrite**, but `resolve_target()` capped what it was shown at
`raw[:8000]` (and the debate capped again at 4000). For any target above that cap,
the model never saw the whole file.

Measured: **17 of 30 mapped repair targets exceed the cap.**

| target | size | would lose on rewrite |
|---|---:|---:|
| `app/services/pipeline_service.py` | 76,065 | 68,065 (~89%) |
| `app/cognition/debate/debate_coordinator.py` | 61,823 | 53,823 |
| `app/collectors/news_collector.py` | 39,549 | 31,549 |

A backup was taken first, so it was recoverable — but the live cycle would have been
broken until a human noticed.

**Fix:** `_check_size_regression()` in `deployer.py`, run *before* the backup so a
destructive proposal never reaches the write. Refuses rewrites below 60% of original
size, and refuses any output still carrying a truncation marker (proof the model was
looking at a shortened view).

---

## What changed

### 1. Blast radius of the self-healing watchdog

`run_healing_cycle()` was a fully autonomous deploy pipeline: LLM debate → patch to
disk → `git add -A` → commit → push → **rebuild and redeploy the NAS container** —
gated only by `python -m py_compile`.

- **`app/cognition/evolution/repair_scope.py` (new).** `is_patchable()` permits only
  trading-cycle source. Denied: the repair machinery itself, `app/db/`, `app/config/`,
  `scripts/`, Dockerfile/compose/requirements, `.github/`, and **tests** (a fixer that
  can edit tests can "pass" by deleting the assertion). Deny beats allow; unknown
  paths are refused, not permitted.
- **`SELF_HEAL_MODE`** (`diagnose` default | `apply`). **There is no mode that
  redeploys.** `push_git_changes()` and `deploy_container_nas()` were *deleted*, not
  just unwired. An unrecognised value — including a stale `SELF_HEAL_MODE=full` — 
  degrades to `diagnose`, i.e. it fails safe.
- Recovery never needed a redeploy: accepted fixes are re-applied on boot from
  `stable_harnesses`, and `check_probation_fixes` rolls back degradations.

### 2. The watchdog is now actually scheduled

It had **no scheduler wiring and no importer** — engineering failures sat in
`pipeline_state.error` until a human noticed. Now hourly, `max_instances=1`.

`heal_once()` was split out of `run_healing_cycle()`: the latter tears the service
down in a `finally`, which would have killed the live DB pool and scheduler when
called in-process.

Verified live: `[SCHEDULER] Registered self-healing watchdog (interval: 1h, mode=diagnose)`

### 3. Engineering vs market failure taxonomy

`classify_failure()` put harness defects and bad market calls in the same
`failure_buckets` table with no discriminator. `hold_bias` reads `trace.pnl_pct` — 
patching source cannot fix a losing trade.

`error_class` is now `engineering` | `market` | `unclassified`. `wrong_tool_selected`
is deliberately neither: it is the catch-all for "score < 70 and nothing matched",
too noisy to justify an automated code change.

### 4. Code evidence replaces the hardcoded map + byte truncation

`app/cognition/evolution/code_evidence.py` (new) resolves a traceback to the symbol
that raised and returns its real line range, line-numbered, with content hash.

Removes two structural failures:
- `target_map` resolved via hardcoded dicts (**`STRATEGY_MAP` was literally `{}`**),
  so anything unregistered dead-ended. Evidence needs no entry.
- Source was cut at a byte offset unrelated to the fault.

Verified live in-container: `persist_telemetry` → `app/v3/telemetry.py:71-115`,
**2,301 chars vs the old 12,000-char ceiling (81% smaller)**, with no `target_map`
entry.

Also fixed `_extract_relevant_context` scanning only `tree.body`, which made every
**method** invisible — a failure inside a class fell through to a blind
`content[:4000]` slice.

### 5. The context gate was dead code — now wired

`app/services/context_gate.py` is a complete, tested tiktoken budgeter whose docstring
says *"This is the ONLY function callers need."* Its only callers were its own unit
tests. Production passed a flat `max_tokens=8192` that **never counted tool schemas** — 
the largest fixed input cost on a tool-enabled agent.

Also fixed the embedder-overflow fallback, which appended an oversized dynamic block
to the **system prompt**. That relocated tokens rather than shedding them (the model
still received every one) *and* silently defeated prefix caching. Sections now carry
a shed order and are dropped lowest-priority-first.

---

## Phase 3 (LSP) deferred — with the measurements

Ran pyright as a real LSP client against all 625 files (scripts in the session
scratchpad: `lsp_spike.py`, `ast_vs_lsp.py`).

**Pyright fidelity: excellent.** 100% effective recall. The documented `didOpen`-all
under-reporting ([pyright#10086](https://github.com/microsoft/pyright/issues/10086))
**did not reproduce** — identical results across three didOpen regimes. The one
apparent miss was a **docstring**: LSP was right, grep was wrong.

**But a stdlib `ast` walk matched it exactly on distinctive names:**

| symbol | defs | AST | LSP | AST precision |
|---|---:|---:|---:|---:|
| `validate_artifact` | 1 | 15 | 15 | **100%** |
| `PhaseOutcome` | 1 | 61 | 61 | **100%** |
| `close` | 3 | 108 | 7 | 6.5% |
| `execute` | 12 | 1390 | 1 | **0.1%** |

AST recall is always 100% (it returns a superset) but it matches on *name* while LSP
resolves *scope*. So AST is exactly as good for the typical repair target and
catastrophically worse for a generic method name.

**Design consequence:** rather than pick one, `SymbolEvidence.is_ambiguous` flags
>1 definition or refs over threshold, and `render_evidence()` **withholds the
reference list** when ambiguous instead of handing a model 1,390 name-matched
"callers" as fact.

Revisit LSP only if the ambiguity gate proves to fire often in practice.

---

## ⚠️ Incident during this wave (read this)

I briefly broke production. `CREATE INDEX ... ON failure_buckets(error_class)` was
placed in `schema_pg.sql` next to the table. On an existing DB the `CREATE TABLE IF
NOT EXISTS` above it is a no-op, so the column didn't exist at schema-init time and
the index **aborted the whole init transaction**. Every `get_db()` raised
`UndefinedColumn`, taking down the command poller, schedule drain, and pipeline state.

Caught in post-deploy log verification, fixed in `b099238` (index moved into
`migrations.py`, after `_safe_add_column`). Verified: column and both indexes present,
zero `UndefinedColumn` in logs.

**Lesson for this repo:** `schema_pg.sql` runs *before* `migrations.py`. Any index on
a migration-added column must live in `migrations.py`. Do not put them together.

---

## Verified live

- ✅ Container `Up (healthy)`, boot sequence completed successfully
- ✅ `[SCHEDULER] Registered self-healing watchdog (interval: 1h, mode=diagnose)`
- ✅ `failure_buckets.error_class` column + `idx_failure_buckets_error_class` present
- ✅ Zero `UndefinedColumn` errors after hotfix
- ✅ `code_evidence` resolves in-container; `repo_sha` degrades to `unknown` (no `.git`
      in the image, as designed) while `content_hash` matches the host run exactly — 
      confirming the hash is the real staleness signal
- ✅ `tiktoken 0.13.0` present in image (accurate counting, not the heuristic fallback)

**Not yet observed live:** `[CONTEXT_GATE]` log lines. It fires per agent call and no
cycle has run since deploy. Wired and unit-tested, but unproven in a live cycle — 
confirm on the next cycle.

---

## Open / next

1. **Confirm `[CONTEXT_GATE]` on the next live cycle.** Only remaining unverified item.
2. **Phase 4 — impact-driven test selection.** No prior art in the repo; `pytest`
   (9.0.3, `-xdist`) is already available. Would replace `python -m py_compile` as the
   real gate. Borrow the closure algorithm from `deploy-kit/scripts/lib-impact.js`
   (`affectedClosure`, `computeSurface`) — port the algorithm, not the TS/regex code.
3. **Decide on `SELF_HEAL_MODE=apply`.** Currently diagnose-only. Recommend leaving it
   until Phase 4 gives it real test gating.
4. **Fork drift.** `lazy-agent-service/python/` is a near-verbatim duplicate of
   `trading-service/app/` (same `debate.py`, `target_map.py`, `self_healing_watchdog.py`).
   **None of this wave was mirrored there.** Decide explicitly: mirror or let it drift.
5. **`SHARED_CODEBASE_PATH=/app`** is set in the Dockerfile with zero consumers — wire
   it or delete it.
6. **`_VALID_ARTIFACT_TYPES` drift** (found, not fixed): `tournament_result` has no
   schema in `ARTIFACT_SCHEMAS`, so `validate_artifact("tournament_result", …)` always
   returns `["Unknown artifact_type"]`; `portfolio_screener` has a schema but is not an
   appendable desk type.
