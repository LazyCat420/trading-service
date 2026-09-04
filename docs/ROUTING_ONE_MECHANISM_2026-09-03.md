# One routing mechanism — 2026-09-03

**Branch:** `routing-one-mechanism` (off `21bc4b6`)
**Scope:** the DGX Spark stopped receiving trading work on 2026-09-02 and the
09-03 fix was pushed but never deployed. This removes the mechanism that caused
it rather than re-tuning its default, and gates the two places it was armed.
**Suite:** 5,974 pass / 2 fail / 89 skip (unit, 5m00s). Both failures are
`test_budget_overrides::test_agent_budget_turns[v3_bull_agent / v3_debate_judge]`
and are **identical on `93330e6`** — pre-existing, untouched by this branch.

Companion (the reasoning, for readers): trading-client chapter
`110-one-routing-mechanism-2026-09-03.md`, served at
`http://10.0.0.16:8888/documentation`.

---

## The headline: the fix was real, the deploy was not

`21bc4b6` correctly added `glm` to `DECISION_MODEL_PATTERN` and moved the
contract check inside the candidate loop so a wrong model on one box falls back
to the other. It was pushed to master. It was never deployed: the NAS container
ran `GIT_SHA=93330e6` with `SOLO_JETSON_MODE=true` at `.env` line 380 for the
whole audit window. The handoff's "Verified live — DGX Spark is selected" was
measured somewhere other than production.

**Evidence (`v3_agent_telemetry`, 09-01 → 09-03, by `model_used`/`provider`):**

| population | result |
|---|---|
| `nemotron35` / `vllm` (Jetson) | 191 SUCCESS + 9 SCHEMA_INVALID |
| `deepseek-v4-flash-0731` / `vllm-2` | 2 rows, both 09-01 |
| `GLM-5.3-Flash-EXL3` / `vllm-2` | **0 rows** |

Prism's ledger for 09-03: 1,222 `nemotron35` requests, 4 GLM, three of which
were `CUSTOM_USER_CHAT`. Both boxes were healthy throughout — DGX serving GLM
at `max_model_len` 1,000,000, Jetson serving nemotron at 128,000.

**What it cost.** Not failures — degradations that still wrote rows:
`v3_fundamental_analyst`/CLIP EMPTY after 13 loops, 490,564 input tokens, 565 s;
`v3_junior_analyst` EMPTY on HPE and CLIP after 7 loops each;
`v3_decision_synthesizer` 9 of 27 runs SCHEMA_INVALID.

## Changes

| File | Change |
|---|---|
| `app/services/prism_agent_caller.py` | `COLLECTOR_KEYWORDS` + `is_collector_agent()` (one table; `21bc4b6` had two copies) with `translator` added; `box_is_saturated()`; the resolver rewritten as preference → overflow → fallback with no static pin; `VLLMEndpoint.max_model_len`, filled from `/v1/models`; `_poll_all_metrics` re-syncs the model each tick |
| `app/config/config.py` | `SOLO_JETSON_MODE` deleted; `PROVIDER_VLLM_1_CONCURRENCY` gains `validation_alias="JETSON_MAX_CONCURRENT"` (deploy.sh has appended that var for months and nothing read it) |
| `app/services/vllm_hosts.py` | the solo filter deleted |
| `app/services/startup_tasks.py` | `_resolved_models()`; readiness needs ONE resolved endpoint, not all of them |
| `app/routers/vllm_router.py` | the endpoint view returns only fields `VLLMEndpoint` has (it was reporting `max_model_len: 128000` for the 1M box) |
| `deploy.sh` | the `SOLO_JETSON_MODE` line deleted; `DECISION_MODEL_PATTERN` is a literal, not `${VAR:-default}` |
| `scripts/verify_shipped.py` | `deploy_env_violations()`, `routing_env_verdicts()`, `route_verdicts()`; the remote probe reports the routing env and what `v3_decision_synthesizer` / `janitor` resolve to right now |

## Why delete the flag rather than default it off

`SOLO_JETSON_MODE` made the Jetson the *only* candidate, so it could not let the
DGX back in when it returned — the failure is not that it was set, it is that
setting it is unrecoverable without a redeploy. Its value came from the
deploying operator's shell (`${SOLO_JETSON_MODE:-…}`), so a stray export re-arms
it whatever the default says. Three files read it, and the boot readiness check
had to special-case it. The fallback loop already does what the flag was for:
**to take a box out, leave its `PROVIDER_VLLM_*_URL` unset.** `ROUTING_MODE`
went with it — read at one call site, declared in no config, so both `force_`
branches were dead.

## Tests, each proven red on `93330e6`

A control worktree at `93330e6` was checked out and the new files copied in.

| File | New | Red on `93330e6` |
|---|---|---|
| `tests/unit/test_routing_overflow_and_no_pin.py` | 16 | 12 |
| `tests/unit/test_vllm_endpoints_view.py` | 3 | 3 |
| `tests/unit/test_deploy_routing_env_gate.py` | 11 | 10 |
| `tests/unit/test_preflight_and_feed_fixes.py` (2 added) | 2 | 1 |

The four that pass on the old code are arithmetic on the new helper's inputs and
the "both boxes off contract still aborts" case, which was already true.

Controls worth naming:
- **The stray flag.** Armed as production armed it: the env var plus a raw
  attribute in `settings.__dict__` (a pydantic `Settings` refuses `setattr` for a
  removed field, which is itself half the proof). Routing must still pick the DGX.
- **No reader, by ast.** A walk over every `app/**/*.py` looking for a `Name`,
  `Attribute` or string constant resolving to either flag. Prose recording the
  removal passes; a name the interpreter resolves does not.
- **The condemned deploy lines as FIXTURES**, copied from
  `git show 93330e6:deploy.sh` and pasted into the test, never re-read from git —
  a control pinned to a moving ref passes for the wrong reason once the fix lands.
- **The half fix is still caught**: `SOLO_JETSON_MODE=${SOLO_JETSON_MODE:-false}`
  is a violation, because a pin defaulted off is still a pin.

`tests/unit/test_solo_jetson_routing.py` was deleted with the feature (5 tests,
referenced by nothing else). The two existing endpoint stubs
(`test_smart_routing.py`, `test_model_contract.py`) gained the metrics fields;
`box_is_saturated` reads them directly so a stub without them raises rather than
reading as an idle box.

## Open item found while measuring: the "janitor" on the Jetson is the translator

`app/autoresearch/janitor.py` makes no LLM calls and runs post-cycle. The
`CUSTOM_SYSTEM_JANITOR_AGENT` traffic seen during a scrape is
`news_collector._translate_foreign_text`, whose `agent_id="translator"` matches
no entry in `prism_agent_registry.AGENT_ID_MAP` and lands on the janitor persona
by default. Measured: ~25.9k input tokens per three-sentence translation (prism
injects ten near-duplicate persona memories), ~16 s against a **5 s**
`asyncio.wait_for`, **59 timeout lines in cycle-v3-1788479270**, and a new
`memory:extract` + `memory:embed` pair per call — 1,723 janitor-persona memories
in `prism.memories`. This branch classifies `translator` as light work so the
traffic stays on the Jetson by name; the token bloat, the timeout mismatch and
the memory writes are a separate change.
