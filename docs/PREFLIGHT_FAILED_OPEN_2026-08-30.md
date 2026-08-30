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
