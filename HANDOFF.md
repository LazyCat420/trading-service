# HANDOFF — early-stop retry telemetry gap closed (2026-07-20)

**Deployed:** trading-service `d4d014b` + lazycat-sdk `c54f817` (v0.3.1) → synology, healthy. Bundled copy synced to lazy-agent-service `6f2f6ea` (python mirror; not executed in the Node container).

Closes the one open item from the telemetry fix below. The SDK emitter only fired for failures it meant to RETRY, so a give-up that stopped early — DoomLoopException, or a FATAL on a later attempt — was logged but emitted no dashboard event. Those are the failures most worth seeing.

**Fix:** the SDK emitter contract gained a keyword `final: bool` (True on any give-up, budget-exhaustion OR early-stop). The SDK emits from its stop branch with `final=True`; the shim's `_pipeline_emit` gates give-up-only emission on `final` instead of `attempt < max_attempts` (which silently dropped early stops — an early give-up has attempt < max_attempts yet is terminal). Event `status` and payload now carry `final` too.

**Contract note:** any `set_failure_emitter` callback must now accept `final` (or `**kwargs`). Only one consumer exists (this shim); updated in lockstep.

**Verified live in-container:** DoomLoop on attempt 1 of 5 → one event, status=error, final=True (was zero before). Budget-exhaustion and recovering-call paths unchanged. 906 unit + 109 SDK tests pass.

---

# HANDOFF — full 5-ticker cycle verified end to end (2026-07-20)

**Cycle:** `cycle-v3-1784578079`, 20:07:59→20:22:02 UTC (14 min), status `done`, no error, 202 events, 0 error events. Triggered via `scripts/trigger_cycle.py --max-tickers 5` in-container; **no deploys during the window** (the prior session's two attempts died to mid-run deploys).

**Results:** 5/5 tickers analyzed with decisions — ASML HOLD/72, AXP BUY/75, BLSH SELL/60, C BUY/60, TSLA HOLD/65; 4 `trade_results` rows.

**cycle→autoresearch handoff: CONFIRMED.** Report `ar-93202fdc5909` (status `done`) created the same second the cycle finished. The cycle-triggered SkillOpt path also ran: `v3_bear_agent`, `v3_fundamental_analyst`, `v3_junior_analyst` advanced to skill-doc **version 2** at ~13:23, one minute after cycle end (the v1 rows at 12:54–12:55 were the prior session's direct autoresearch invocation).

**Retry telemetry:** zero `recovery` events this cycle — a clean run has no exhausted retries, and the emitter is give-up-only by design (verified live in-container separately). Provenance of the 1881 pre-existing `phase='recovery'` rows resolved: all June 14–23, from the deleted RLM-harness era (`retry_run_adversarial_debate.<locals>…` steps) — telemetry worked then, broke in a later refactor, nothing written July 1–19.

**Timezone quirk noticed, not fixed:** `pipeline_state` timestamps are UTC-naive (20:22) while `autoresearch_reports`/`agent_skills` store local time labeled `Etc/UTC` (13:22). Same instant, two conventions — don't be fooled when joining across them.

---

# HANDOFF — SkillOpt VERIFIED LIVE: 7 agents now carry learned skill docs (2026-07-20)

**Deployed:** SkillOpt shipped in `7b4bd86`/`d05a1f9`, sanitizer in `4fc826c`; all live
(a parallel session's `10c7b81` deploy carried them).
**Tests:** 23 unit in `tests/unit/test_skill_optimizer.py`, full unit suite green.

## ⚠️ State change: all 7 V3 agents now have an active skill doc

This is live and **will affect the next real trading cycle**. Autoresearch run
`skillopt-ar2-7ce8cbe6` (against real cycle `cycle-v3-1784554200`: 7 tickers,
2 BUY / 2 SELL / 3 HOLD) produced:

```
[AUTORESEARCH] SkillOpt: {'baseline': 0.0938, 'updated': [all 7 agents],
                          'rejected': 0, 'skipped': 0}
```

Every agent got `version=1, action=ADD, status=active`, 631–881 chars. Content is
sensible and grounded in that cycle's reflection (temporal-relevance filters,
conviction calibration against the ~57% realized win rate, data-integrity checks).
Inspect with `SELECT agent_name, version, LEFT(skill_text, 120) FROM agent_skills`.

**To roll back entirely:** `UPDATE agent_skills SET status='archived';` then bounce the
container (or wait ≤15 min for the loader TTL). Kill switch: `SKILLOPT_ENABLED=false`.

## What was verified end-to-end (not just unit-tested)

1. **Safety rail fires.** A first run against a *stopped* cycle (0 tickers, 0 results)
   correctly returned `{'skipped': 'anomalous_cycle'}` and wrote nothing — the guard
   against mutating long-lived skills from a broken measurement works.
2. **Happy path writes.** The second run against a real cycle updated all 7.
3. **Loader serves it.** `load_skill_prefix('v3_bull_agent')` returns a 908-char prefix
   starting with `## Agent Skill Guidance (SkillOpt)`; an unknown agent returns `''`.
4. **Sanitizer works.** Stored docs start at `###`, with no `---` fence artifacts.

## Open items — read before trusting the gate

- **The score gate is not discriminating yet.** All 7 candidates scored the *identical*
  `+0.0150` — the maximum the heuristic can award — and **0 were rejected**. In practice
  every LLM proposal currently passes. `_simulate_score_with_skill` is a content-quality
  proxy (has digits / has imperatives / overlaps the reflection / length band), NOT a
  replay, so it cannot tell a good edit from a plausible one. Treat the current bar as
  "well-formed", not "proven better". Tighten `MIN_SCORE_DELTA` or add discriminating
  signals before letting versions climb.
- **Nothing has exercised v2+.** Only first-write (`""` → v1) has run in production; the
  archive-then-insert path and the difflib near-noop rejection are unit-tested only.
- **No outcome attribution.** There is no measurement tying a skill version to subsequent
  win rate, so a bad skill will persist silently. That is the natural next piece.
- **A full 5-ticker cycle was NOT completed end-to-end this session** — a parallel session
  restarted trading-service three times (12:34, 12:37, 12:48), killing two cycles mid-run.
  SkillOpt was therefore verified by invoking autoresearch directly, which is the exact
  code path a cycle triggers, but the cycle→autoresearch handoff itself was not observed.

---

# HANDOFF — retry/recovery telemetry actually emits now (2026-07-20)

**Deployed:** trading-service `811cb69` → synology, healthy
**Tests:** 905 unit (10 new) + 157 integration/regression

Follow-up to the lazycat-sdk extraction wave below, which is where this bug
was found.

## The bug

`PipelineService.emit()` **did not exist**. The only `emit` was a local
function nested inside the cycle runner — it closes over `cycle_id`, which is
why it was never a method. Both callers:

- `app/utils/resilience.py` (retry failures)
- `app/recovery/engine.py:208` (recovery decisions)

raised `AttributeError` on every call, straight into a bare
`except Exception: pass`. **No retry or recovery event has ever reached
`pipeline_events` or the dashboard.**

## The fix

- Real class-level `PipelineService.emit()` resolving `cycle_id` from
  `_state` (`append_events` already no-ops on a falsy one, so calling it
  outside a cycle just logs). `recovery/engine.py` needed no change — its call
  site started working the moment the method existed.
- `app/utils/resilience.py` registers `_pipeline_emit` via the SDK's
  `set_failure_emitter()` hook (lazycat-sdk v0.3.0).

### Two deliberate choices

1. **Does not touch `_state["progress"]`** the way the cycle runner's closure
   does — a background retry is ambient telemetry, not cycle progress, and
   writing it there would make the dashboard report a retry as the step the
   pipeline is currently on.
2. **One event per give-up, not per attempt.** `base_agent.py` runs
   `retries=5`; per-attempt would write 5 rows per exhausted call across
   agents x tickers x cycles. Interim attempts are already logged.
   `RESILIENCE_EMIT_EVERY_ATTEMPT=true` restores per-attempt for debugging a
   flapping upstream.

`append_events` uses the **sync** connection pool and these callers sit in
async retry paths, so the write is handed to a worker thread when a loop is
running rather than blocking it on every failure.

## Known gap (open)

The SDK only invokes the emitter for failures it intends to **retry**. A call
that stops early — FATAL on a later attempt, or a registered non-retryable
like `DoomLoopException` — is logged but produces **no event**. Closing it
means emitting from the stop branch in `lazycat/resilience.py` too.

## Watch after the next few cycles

Event volume in `pipeline_events` for `phase='recovery'`. It was structurally
zero forever; it is now non-zero for the first time. If it is noisy, the knob
is the give-up-only default above, not turning it back off.

## Do NOT half-sync this to lazy-agent-service

The fix spans two files. `pipeline_service.py` is one of the four that
genuinely differ between the twins **and** is dirty there from a parallel
session, so that repo's `PipelineService` still has no `emit`. Copying
`resilience.py` alone would register an emitter calling a missing method and
silently recreate this exact bug. Take both files or neither.

---

# HANDOFF — generic utils sourced from lazycat-sdk (2026-07-20)

**Date:** 2026-07-20
**Deployed:** trading-service `fa70560` → synology
**Tests:** 892 unit + 157 integration/regression + 11 multi-repo-audit, all passing
**Companion commits:** lazycat-sdk `cceda1c`/`9f0a65e`/`c790b1a` (v0.3.0), HTML-Notes `11dbb40`, lazy-agent-service `b60d842`

## What changed

Five modules that held *generic* infrastructure now source it from
lazycat-sdk v0.3.0 and remain here as thin import shims. **No call sites
changed** — `text_utils` alone has ~30 importers.

| File | Now delegates to | Kept locally |
|---|---|---|
| `app/utils/text_utils.py` | `lazycat.llm_json` | `parse_trading_decision`, `fmt_usd`, `parse_malformed_text_response`, scrape-artifact + sanitize helpers |
| `app/utils/resilience.py` | `lazycat.resilience` | DoomLoopException registration |
| `app/cache.py` | `lazycat.cache` | — |
| `app/scraper/core/rate_limiter.py` | `lazycat.ratelimit.KeyedRateLimiter` | `DOMAIN_LIMITS` table |
| `app/services/api_rate_limiter.py` | `lazycat.ratelimit.KeyedSemaphore` | `SERVICE_LIMITS` from settings |

`parse_json_response` stays a wrapper that supplies this app's two hooks:
placeholder-ticker rejection (`_is_placeholder_json`) and the markdown-report
fallback (`_malformed_fallback`, still gated on an `action` key).

**Verified behaviour-identical** to the previous implementation across 84
input/function combinations — 0 differences.

## Found, NOT fixed: retry telemetry is dead code

`resilience.py` emitted failure events via `PipelineService.emit(...)`.
**That method does not exist.** The only `emit` in `pipeline_service.py` is a
local function nested inside the cycle runner (line ~417), so every call
raised `AttributeError` straight into a bare `except Exception: pass`. No
retry event has ever been emitted.

`app/recovery/engine.py:208` calls the same non-existent method.

The SDK now exposes `lazycat.resilience.set_failure_emitter(fn)`. Wiring real
telemetry means giving `PipelineService` an actual class-level `emit` and
registering it at startup. Deliberately left alone — it is a behaviour change,
not a refactor.

## Gotchas for the next session

- **`app/cache.py` has zero importers.** (Do not confuse `timed_cache`/
  `invalidate_cache` with `app.services.parameter_store.invalidate_cache`,
  which is unrelated and widely used.) Shimmed rather than deleted.
- `DOMAIN_LIMITS` is passed to `KeyedRateLimiter` **by reference**, so runtime
  edits to the table still take effect. Don't "fix" that into a copy.
- Long-standing retry quirks are now pinned by SDK tests rather than fixed: a
  FATAL classification on the *first* attempt is still retried once, and a
  failing sync `on_failure` is swallowed before the original error raises.
- `tests/test_multi_repo_audit.py` asserts on the *source text* of
  `resilience.py` (it must contain the string "DoomLoopException"). The shim
  satisfies this, but keep it in mind if you rewrite that file.
- Deploying this repo **also ships lazycat-sdk to the NAS** (`deploy.sh`
  PRE_BUILD tars the sibling checkout). HTML-Notes mounts the same directory —
  deploy both together after an SDK change.

---

# HANDOFF — SkillOpt: per-agent skill docs learned from cycle outcomes

**Date:** 2026-07-20 (follows the grounded-extraction wave below)
**Deployed:** trading-service `7b4bd86` (+ test commit after) → synology, verified live
**Tests:** 1025 unit passing (1005 prior + 20 new in `tests/unit/test_skill_optimizer.py`)

## What shipped

SkillOpt (modeled on microsoft/SkillOpt's propose→validate→commit loop): each of the
7 target V3 agents (`v3_junior_analyst`, `v3_fundamental_analyst`, `v3_quant_analyst`,
`v3_bull_agent`, `v3_bear_agent`, `v3_regime_engine`, `v3_board_of_directors`) gets a
persistent markdown "skill doc" prepended to its system prompt, mutated once per
autoresearch run from the cycle reflection.

- **`app/autoresearch/skill_optimizer.py`** — post-cycle mutation. Baseline =
  confidence-weighted WIN(1)/FLAT(0.5)/LOSS(0) over the last 10 resolved directional
  `decision_outcomes` (cold-start guard: ≥5 rows; live baseline at deploy was 0.094).
  One `llm.chat` proposal per agent at `Priority.LOW` (ADD/DELETE/REPLACE/SKIP JSON).
  Gates: `is_poisoned_response`, meta-instruction-injection regex, 4k-char cap,
  difflib near-noop check, heuristic score gate (+0.5% over baseline). Rejects are
  audited to `rejected_skill_edits`. Skips entirely on rule-based/anomalous
  reflections. Time-boxed: 120s per agent, 420s total. Kill switch:
  `SKILLOPT_ENABLED=false` (settings/env, defaults on).
- **`app/autoresearch/skill_loader.py`** — inference-time half. In-process cache with
  15-min TTL (autoresearch runs in cycle_main; the API server is a separate process
  that explicit invalidation can't reach). Fail-silent `""` on any error; misses are
  cached too.
- **`app/v3/agent_runner.py`** — prepends the prefix right after `SYSTEM_PROMPT` is
  read. Byte-identical between mutations so vLLM prefix-cache reuse survives.
- **DB** — `agent_skills` (versioned, one `active` row per agent) +
  `rejected_skill_edits`; in BOTH `schema_pg.sql` and `migrations.py`, self-contained
  (schema runs before migrations — nothing references migration-added columns).
- **`core.py`** — new non-fatal `skill_mutation` phase after the lesson store
  (`autoresearch_reports.phase` is plain TEXT, no enum to extend).

## Gotchas for the next session

- The validation score is a HEURISTIC (content checks + baseline), not a replay.
  If skills start encoding noise, tighten `MIN_SCORE_DELTA` or the heuristic in
  `_simulate_score_with_skill` before touching the LLM prompt.
- Agent keys must stay the `v3_`-prefixed `AGENT_NAME` strings or loads silently miss.
- Verified live post-deploy: both tables exist, loader returns `""` cleanly, baseline
  computes. No cycle has run through skill_mutation yet — check
  `SELECT * FROM agent_skills` / `rejected_skill_edits` after the next cycle, and
  `[AUTORESEARCH] SkillOpt:` in the logs for the summary line.

---

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
