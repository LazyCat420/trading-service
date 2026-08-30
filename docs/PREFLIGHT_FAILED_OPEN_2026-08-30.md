# The pre-flight certified the outage it was built to catch — 2026-08-30

Service-side record of the behavioral-audit step 3 batch. Client chapter:
`trading-client/documentation/chapters/103-*`.

Branch `preflight-endpoint-offline`. Audit resumed from ch.99/ch.100, which
left off mid-way through the per-agent behavioral inventory.

## The incident

`app/services/llm_preflight.py` exists because the vLLM backend was down
2026-08-21..24 and **28 cycles ran to completion anyway**. Its contract is
*fail-open on ambiguity, fail-closed on proof*.

It failed open on the proof.

`resolve_default_model_for_agent` raised:

```
VLLM endpoint offline: http://10.0.0.16:5591/vllm-shim/gold-spark
(RuntimeError: HTTP 502 with no usable model list)
```

Everything that was not a `ModelContractError` was classified as "probe
machinery broken — proceed", so `llm_can_answer()` returned **`ok=True`**
while the decision box had no servable model at all.

## What it cost (measured from `trading_bot.shared_desk`)

| | 2026-08-28 | 08-29 | 08-30 |
|---|---|---|---|
| desks | 20 | 7 | 6 |
| clean decisions | 0 | 0 | 0 |

**33 desks, 66 regime-engine calls at 75–102 s each** (2 attempts per desk,
each retried 5 times internally), **zero decisions, zero pages** over three
days. Every desk carries, verbatim:

```
error_message: "ResilientCallError: All 5 attempts failed
                for run_agent.<locals>._agent_llm_call
                [5 attempts, last_type=transient]"
failure_reason: RUNNER_EXCEPTION
model_used: null   provider: null
```

That string is the exact signature `llm_preflight`'s docstring names as the
thing it exists to prevent.

Each desk then ends the same way: `v3_regime_engine` never produces
`regime_classification`, the pipeline attempts `INIT → PM_DONE`, the phase
machine refuses it, and the `except ValueError` handler stores a
`board_degraded_fallback` `final_decision` whose *reasoning is the transition
error text*. 33/33 carry `cycle_metadata.pipeline_incomplete`. That part is
working as designed (see `tests/unit/test_desk_phase_transition.py`) — the
desk is deliberately kept so the failure is countable.

**The instrument read clean.** `scripts/measurement_window_report.py` printed
`degraded/aborted rows .. 0` throughout, because an aborted desk writes no
decision row. Desk mortality is the only place the outage was visible: 94 of
139 desks (68%) since 08-23.

## Why it was misclassified

The abort verdict arrived **one step earlier than the probe was written for**
— at model *resolution*, not at *completion* — and an earlier arrival read as
"our own tooling is broken" rather than "the endpoint is dead".

`get_live_model_from_vllm` raises only after it has queried `/v1/models`
twice AND has no cached id to degrade to. That is positive evidence. But it
raised a bare `RuntimeError`, indistinguishable at the catch site from the two
*configuration* `RuntimeError`s beside it ("endpoint not configured or
disabled", "no configured URL") — which genuinely say nothing about whether
the box is alive.

## The fix

Classify by **exception type at the seam**, never by message substring.

- `app/services/prism_agent_caller.py`: new `ModelUnavailableError(RuntimeError)`,
  raised at both exhaustion sites of `get_live_model_from_vllm`. A
  `RuntimeError` subclass, so every existing `except RuntimeError` is
  unchanged (no caller matches on the message — checked repo-wide).
- `app/services/llm_preflight.py`: aborts on `ModelUnavailableError` as well as
  `ModelContractError`; pages through the existing `alert_preflight_abort`.
  Config `RuntimeError`s still fail open — one bad env var must not block all
  trading, which is the false red the module refuses by design.

## Verification

- `test_no_servable_model_aborts` and
  `test_the_resolver_raises_the_typed_error_when_it_gives_up`: **red on master,
  green on the fix**.
- `test_a_config_runtimeerror_still_fails_open`: passes both ways — it pins the
  boundary the fix must not cross.
- **Live, against the actually-dead box** (models were down while this was
  written): master `ok=True`; fixed `ok=False — no servable model: VLLM
  endpoint offline: … HTTP 502 with no usable model list`.

## Open items

- ⚠ **The probe measures a route no failing agent uses.** `llm_can_answer`
  calls `chat_toolless` → `/chat` under the name `v3_decision_synthesizer`, a
  **toolless** agent. 8 of 13 v3 agents declare tools and therefore route to
  `/agent` (which attaches the ~21k-token MCP catalog server-side) — including
  `v3_regime_engine`, the first agent every desk runs. A `/chat`-alive,
  `/agent`-dead box still passes. **Latent, and NOT the cause of this
  incident** (resolution failed before any route was chosen).
- **Grounding decay got worse, not better.** 139 desks since 08-23: bull 94.1%
  → bear 82.4% → defense 77.0% → judge 85.1%. Three of four hops below
  ch.100's 85.6% floor. Mismatches are gross (`sma50: 200.0 vs 105.99`,
  `pe: 8.35 vs 22.5`) and the BLSH `oper_margin: 3.14 vs -3.14` sign flip
  survives all three hops. The judge is not correcting; it is laundering.
- **ch.99's market_context item is answered, and the answer is no.** Inside the
  whiteboard's real retention window (08-17..08-27): **57.9% → 89.3%**
  (25/28), fallback fired only twice, 3 desks still bare (ANDG, DKS, HUM
  @08-26). Not the predicted 100%. Small sample.
- **Tournament / peer-request residue** from `1cf3c0b`, all statically
  confirmed, none swept yet: `tournament_pitch` whitelist for a nonexistent
  agent (`app/agents/tool_whitelists.py:133`); the dead
  `artifact_type == "tournament_debate"` branch in `app/v3/quality_scorer.py`
  whose only test covers the dead branch; the `[JURY VETO]` /
  h2h / jury render block at `app/v3/shared_desk.py:715-759` that can never
  fire (both surviving writers hardcode `vetoed: False` and emit `{}`); the
  `"A peer agent requested your specific analysis"` block at
  `app/v3/agent_runner.py:1267` now reachable only from `challenger.py`; and
  `app/v3/agents/fundamental_analyst.py:67`, which still instructs the FA
  about peer-request semantics for a mechanism that no longer exists.
- **`AGENT_ROLE_BUDGETS` is nearly dead.** 7 entries; only `junior_analyst` is
  reachable in production, via `flash_briefing.py`'s `CUSTOM_V3_JUNIOR_ANALYST`
  (→ 10 turns, while the cycle's own junior gets 7 from
  `AGENT_BUDGET_OVERRIDES`). `get_budget_for_role` strips `custom_v3_`/`custom_`
  but **not** a bare `v3_`, so no `v3_*` spelling can ever hit the table. The
  existing tests only exercise reachable spellings.
- **Resolved, was open in `EMPTY_OUTPUT_SPIRAL_2026-08-26.md`:** the
  `"Acknowledged. I am ready to process the quantitative data."` primer is not
  a leak — it is a deliberate Qwen-compatibility shim at
  `prism_agent_caller.py:500`, documented in the comment above it, forcing
  prism's vLLM patch to rewrite the injected system block into a user message.
  Note it is calibrated for Qwen while `DECISION_MODEL_PATTERN` is now
  `deepseek`, and its text says "the quantitative data" for every caller
  including the news translator and the memory briefer.

## Probe provenance

Read-only, rerunnable, in this session's scratchpad: `probe_tourn.py`,
`probe_shape.py`, `probe_mortality.py`, `probe_desk.py`, `probe_tel.py`,
`probe_tel2.py`, `probe_mc.py`, `probe_mc2.py`, `probe_wb.py`,
`probe_preflight.py`. Standing instruments re-run:
`scripts/measurement_window_report.py`, `scripts/grounding_decay_report.py`.

Two traps hit while probing, recorded so the next reader does not repeat them:
`shared_desk.desk_data` is **JSON text** — querying `tournament_result` at the
top level returns 0 across all 2,036 desks, inside `desk_data` it is 426; and
`whiteboard_entries` only retains 08-17..08-27, so any "pre-fix" rate computed
outside that window is a retention artifact, not a behaviour.

---

# Addendum — the measurement shelf is bound to a frozen store (2026-08-30)

Follow-on from the same session. Where the audit above asked "did the cycle
run", this asks "can we still measure it". Answer: mostly no, and the two
failure modes look nothing alike.

## The frozen archive is still up, and still answering

The Mongo cutover happened 2026-08-19. The Postgres archive was never taken
down, and it still serves:

| table | frozen PG | newest row | live Mongo |
|---|---|---|---|
| `shared_desk` | 1,762 | **2026-08-19 22:54** | 2,036 (current) |
| `decision_scores` | 312 | — | 508 |
| `trade_results` | 1,084 | — | 1,155 |
| `whiteboard_entries` | 2,666 | — | 1,226 |

Anything reading it gets an answer that stops 11 days ago, with no error and
no staleness marker.

## 91 scripts are still bound to it, and they split two ways

- **~70 fail loudly.** They reach the DSN through `settings.DATABASE_URL`, a
  pydantic attribute that no longer exists, so they raise
  `AttributeError: 'Settings' object has no attribute 'DATABASE_URL'`.
  Verified on `decision_score_report.py`, `calibration_report.py`,
  `agent_scorecard.py`; `calibrate_confidence_floor.py` dies one step earlier
  on `KeyError: 'SIM_DSN'`.
- **19 answer silently.** They call `load_dotenv()` and then
  `os.getenv("DATABASE_URL")`, which resolves fine from `.env` — so they
  connect to the archive and report pre-cutover data as current.

**This is the dangerous half.** `scripts/shadow_report.py` — the
contradiction-shadow aggregate, the empirical input for deciding whether to
promote the shadow into a real gate — runs clean today and prints rows dated
**2026-07-29** citing `tournament_result`, a subsystem deleted on 08-28. It
does not say the data is a month old. `scripts/simplification_baseline.py` is
in the same set, which is consistent with `.simplification_baselines/*` still
naming `tournament.py`.

Also in the silent set and worth its own look: `clear_db.py`, a destructive
script still pointed at the archive rather than at Mongo.

## Why this matters to the audit series

ch.102 closed with "`confidence_audit.py` still reads the frozen Postgres
archive — every future calibration question needs the Mongo port first", and
`ba9db6d` ported `confidence_audit` and `power_report`. **That was 2 of ~30.**
The rest of the calibration shelf — `decision_score_report`,
`calibration_report`, `calibrate_confidence_floor`, `gate_ablation`,
`agent_scorecard`, `grade_regime_calls`, `self_consistency_bench` — cannot
run at all. The measurement window that ch.100 and ch.102 are both waiting on
is being judged by instruments that are either dead or frozen.

`scripts/verify_shipped.py` has the same defect and it is the one that hides
best: it catches the `DATABASE_URL` AttributeError and downgrades it to a
WARN, then prints `0 pass, 0 fail, 3 warn` and exits 0. A broken instrument
reporting as a warning is how a shelf rots quietly.

## A related correction

`decision_scores` is NOT a decision ledger. It is a quant score written in the
context-injection layer before any agent runs, so it has a row for every desk
including the ones that die. Measured: **161 of 508 rows (31.7%) belong to
desks that never produced a decision**, each carrying a `band`, a `score`, a
`percentile` and a `baseline_confidence` — e.g. JPM `cycle-v3-1788074145`,
`score 66.1 / percentile 92.6 / baseline_confidence 72`, on a desk that died
at the regime engine. `decision_score_report.py` is aware of this and counts
them ("desks that never reached a verdict. NOT the same as a HOLD") — but it
is one of the scripts that can no longer run. Any new calibration join must
filter on decision provenance, not on the presence of a score row.

ch.100's backfill item is **closed**: 508 of 508 `decision_scores` rows carry
a proper BSON date `created_at`, none missing, none string-typed. Note the
backfill script's own vacuity check is capped by its `limit=1`, so it always
prints "collection holds 1 sample row(s)" when non-empty — misleading wording,
verified harmless.

## Open

- Port or retire the 19 silent readers first (they are the ones that can
  mislead), then the loud-dead calibration shelf.
- `verify_shipped.py`: the `DATABASE_URL` read, and the WSL container probe
  that reports `sudo: docker: command not found` as a warning.
- `scripts/ops/full-suite.sh` — the landing queue's runner duty names this
  script and it does not exist in the repo.

---

# Correction — the archive is not trading's to retire (2026-08-30)

The addendum above said "the Postgres archive was never taken down". That is
true but incomplete, and the missing half changes what to do about it. Asked
directly *"why do we still have Postgres if we migrated to Mongo?"*, the
measured answer is: **the trading migration IS complete; the database was never
trading's alone.**

## The trading half is genuinely done

The application image carries **no Postgres driver at all**. `app/db/connection.py`
moved to `scripts/migration/pg_connection.py` at teardown and psycopg was split
into `requirements-migration.in`;
`tests/unit/test_app_image_has_no_pg_driver.py` enforces both halves with an
AST scan and a negative control. The cycle reads and writes Mongo only. Nothing
below is a cycle regression.

## The `trading_bot` database has another live tenant

`deploy-kit/.env.deploy` sets, verbatim:

```
DATABASE_URL=postgresql+asyncpg://trader:***@10.0.0.16:5433/trading_bot
```

— and that is **treesearch-service's production DSN**. Its ORM
(`treesearch-service/src/models/orm.py`) declares 14 tables, and they live
inside `trading_bot`:

| table | rows | newest |
|---|---|---|
| `genomic_samples` | 650 | **2026-08-26 21:16** |
| `canonical_strains` | 471 | 2026-08-16 21:25 |
| `chemical_profiles` | 258 | (no timestamp column) |

plus `strain_aliases`, `breeders`, `genetic_relationships`,
`source_genomics_records`, `observations`, `observation_images` and the
`glass_*` set. Written eight days after the trading cutover. **The database
cannot be dropped: a live service is inside it.**

The server at `10.0.0.16:5433` hosts five databases — `trading_bot` (4,278 MB),
`trading_bot_test` (18 MB), `treesearch_test` (8 MB), `smartgarden` (8 MB, with
two live connections), `postgres` — so the *box* is shared infrastructure too.

## What IS still wrong

- **`box_benchmark_runs` is split-brain.** 136 rows in Postgres, newest
  **2026-08-27 01:40**; 113 rows in Mongo. `scripts/jetson_benchmark.py:486`
  writes the PG side with raw SQL. It is a script, not the image, so the driver
  guard never fires — but benchmark history now exists in two stores that
  disagree. It was one of the four tables ch.67 recorded as OUTSIDE the
  migration manifest, and it is the one still being written.
- **`autofix_runs` does not exist in Mongo at all**, and `agent_tasks` exists
  with 0 rows — consistent with the purge's empty gate having dropped them.
- **Three connections have been idle in `ROLLBACK;` since 2026-08-26 20:49**,
  from a docker bridge address. Four days, never reaped.
- **198 tables** remain in `trading_bot` (ch.63 measured 197 after the
  drop-and-reboot cycle; the count has not gone down).

## A measurement caveat for the next reader

`pg_stat_user_tables.n_live_tup` on this database is **useless** — the stats
were reset at some point, so `shared_desk` reports 0 live tuples while
`SELECT count(*)` returns 1,762. Count rows; do not trust the planner stats
here. The same reset is why the write-activity counters show only ~23 rows
across the whole database.

## So: what would actually retire the archive

Not a drop. In order: port or retire the 19 silent PG readers; move
`jetson_benchmark.py` to Mongo so `box_benchmark_runs` stops splitting; give
treesearch its own database (it already has `treesearch_test` next door);
reap the leaked connections; and only then is what remains in `trading_bot`
purely a trading archive that can be snapshotted and dropped.
