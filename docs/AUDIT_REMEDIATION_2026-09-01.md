# Remediating the 2026-08-31 audit — the no-model half

**Branch:** `fix/audit-no-model-items` (9 commits off `bed708d`)
**Scope:** every item from the 2026-08-31 audit that could be fixed and proved
without an LLM. Gold Spark (DGX) is down; only the Jetson is available, so the
ladder re-climb is deliberately NOT in here.
**Suite:** 5,957 pass / 1 fail / 81 skip. The one failure,
`test_migration_ledger::test_adopted_tables_declare_the_key_the_database_actually_has`,
is the documented pre-existing one and is untouched by this branch. It was 2
failures before; `test_prism_prompt_injection` is repaired below.

Companion: the audit itself, `sun/.agents/AUDIT-ladder-and-bed708d-2026-08-31.md`.

---

## The headline: bed708d did not fix what it named

`bed708d` ("resolve empty responses via chat alternation and custom agent
mappings") changed the interleaved turn from `user` to `assistant` and added a
single-turn retry. In the hour after it deployed: **17 retries, 0 successes, 17
still empty.**

The real mechanism was already documented in this repo, on 2026-08-06, in
`base_agent.min_p_for`. Prism's `ParameterRegistry` gives `minP` an
`agentDefault` of 0.05 and injects it whenever the payload carries an `agent`
field. vLLM under speculative decoding refuses any `min_p > 0`, and on prism's
streaming path the refusal arrives as an in-band SSE error frame **after** the
200 header, which the parser skips — so the caller sees a successful call with
empty text, and prism's own empty-output recovery retries with the temperature
*raised*, away from the only setting that works.

`run_agent` (BaseAgent) and `chat_toolless` already sent `min_p=0`. The four
`prism_client.call_agent(...)` sites inside `call_prism_agent` did not.

**Evidence (prism's request ledger, deepseek-v4-flash-0731 via `/agent`):**

| population | result |
|---|---|
| `call_prism_agent` callers, temperature > 0, 2026-08-26 → 09-01 | **477 empty / 0 ok** |
| the same callers at temperature == 0 | all ok (vLLM zeroes min_p under greedy sampling) |
| v3 agents, which send `min_p=0` | unaffected throughout |

**Re-probed 2026-09-01 against the Jetson** (`Qwen3.6-35B-AWQ`, 1.85M
spec-decode drafts), which matters because it proves this is not one box's
quirk and that the fix is testable without the DGX:

```
temp=0.3 + min_p=0.05  -> HTTP 400 "The min_p and logit_bias sampling
                          parameters are not yet supported with speculative
                          decoding."
temp=0.3 + min_p=0     -> HTTP 200, content
```

Six days of silence: memory consolidation, autoresearch reflection, the
evolution auditors, morning/flash briefings and decomposed retrieval all
returned nothing while every cycle reported success.

**Why the existing tests missed it.** `test_min_p_reaches_the_wire.py` followed
the value down *two* transports and `test_min_p_on_local_boxes.py` pinned the
decision — both thorough, neither aware of the third. The new guard parses
`app/` and fails on **any** `call_agent` site without `min_p`, so a fourth
transport inherits the rule instead of inheriting the gap.

---

## What else was found and fixed

### The stale-price gate was silently disarmed
`22c95d8` made the context builders concurrent and folded the staleness probe
into `build_technical_baseline_block`'s try/except. A baseline failure **or a
15s timeout** then skipped detection, and `_apply_policy_gates` reads an absent
age as *fresh* — so `HOLD_POLICY_BLOCKED_STALE_PRICE_DATA` stopped firing in
exactly the case it exists for. (What it is for: RBLX at 10 trading days stale
produced a 75-confidence thesis with a stop-loss **above** the real spot,
priced 24% off.) Restored to sibling try blocks. Fail-open is unchanged and now
pinned by a test; a failed probe stamps `stale_price_detection_failed` and
records a shadow firing, so "we don't know" is countable instead of
indistinguishable from "fresh".

### A partial state write published "idle" to the deploy interlocks
`PipelineStateDB.save_state` used `state.get("status", "idle")`. `get_state`
had been hardened against exactly this after a read fault told the guards the
desk was quiet and a deploy killed a live cycle (EXLS/OWL/CARS/GM, 2026-07-27)
— the write side kept the hole.

On 2026-08-31 `deploy_preflight` (which fails *closed* and was **not** skipped)
printed `pipeline idle (status=idle) — deploy may proceed` at 23:29:14Z while
`cycle-observe-1788217529` was mid-synthesizer; the swap 49 s later cancelled
it. Whether this default produced that specific read is **UNKNOWN** — the
singleton keeps no history — but it is a confirmed path to the same wrong
answer. Now writes `unknown`, which is in neither guard's `IDLE_STATUSES`.

### Boot analytics ran on the loop the tool bridge shares
Five `async def` functions with zero awaits (pandas pivot/corr/groupby) were
awaited directly. Measured 40 s after a redeploy: the 5 s metrics poller stopped
for **207 s** and **129 s**, and every agent tool call in those windows returned
`bridge timeout after 30000ms` — 12 of 62 in `cycle-observe-1788219049`, which
then overrode **both** tickers' triage to FULL for "degraded research". All 12
executed successfully once the loop resumed: queued, not lost. The same message
is 98 of 174 tool failures since 08-24. Now routed through `_off_the_loop`.

### Test cycles were feeding production
`observe_cycle.py` runs a real cycle with orders disabled — by design — but
nothing downstream knew that, and the ladder's own probe could not see the
consequences (it counts only `cycle-v3-` ids, so a flat counter was evidence
about the filter). New `app/services/cycle_scope.py` is the single definition,
applied to the four paths with live blast radius:

1. **previous-desk handoff** — becomes the "Manila Envelope" in *every* agent
   prompt and sets triage's `hours_old`. Already chained in the wild: MP's
   "Age: 2h" prior desk in `cycle-observe-1788220872` was itself
   `cycle-observe-1788211432`, so MP took the cheap delta path instead of the
   full panel its 490-hour-old production desk would have forced.
2. **the 48h data-report fast path** — quotes the prior thesis *and* skips
   fundamentals / multi-API news / reddit / youtube.
3. **watch-desk arming** — a trip enqueues `START_V3_CYCLE` with `trade: True`.
4. **decision_outcomes** — the recorder refuses synthetic cycles and the
   *resolver* excludes them, which covers the 13 rows already stored. They stay
   unresolved rather than deleted: the evidence survives, the cohort stays clean.

**Live residue cleared:** `scripts/deactivate_synthetic_watches.py --apply`
disarmed **8** active triggers — NVDA/JPM/MP from the 08-31 ladder and
ANET/APP/FSLR/GEV/NKE armed since 2026-08-07. 211 → 203 active, 0 synthetic.
Reversible; the script prints per-row undo.

**The AST guard immediately earned itself**, finding five prefixes nobody had
listed: `variance-`, `ondemand-chart-`, `sc-`, `stress-concurrency-`, and
`v3-<uuid>` — `run_v3_pipeline`'s **own fallback**, which produced real desks
that every report skipped. Now mints `cycle-v3-<epoch>-<rand>`. Zero rows carry
it, so this closed a latent hole rather than repairing data.

### Correcting my own audit note
The audit claimed the `cycle_audit` confidence check graded a cohort that was
"93% non-production". Censusing it: of 1,252 rows only 12 are synthetic (4
observe, 7 `sim-bearish-negative`, 1 `audit-cycle`). The other ~1,141 are
`cycle-<epoch>` and `cycle_<date>` — the **pre-V3 production** id schemes, i.e.
legacy production, not contamination. That is precisely why `is_synthetic_cycle`
is a denylist: an allowlist would have discarded most of the desk's outcome
history to remove twelve rows. Verdict unchanged after filtering (conf~95 wins
46% vs conf~91 at 72%), which is the point — the filter removed noise, not the
finding.

### The readiness gate's rule could never fire
`data_readiness.py` required the literal `"No technical indicators"`, which no
producer emits (`build_technical_baseline_block` writes `**NONE ON FILE** …`).
Its unit test passed only because the fixture manufactured the exact string the
code sought. Its staleness threshold was `> 5` while the gate it shadows blocks
at `> 3`. Its docstring advertised three rules it never implemented. All fixed
and cross-checked against the real producer; still shadow-only by design.

### The registry's persona fallback is silent — and ten callers use it
`bed708d` mapped the five names someone noticed. Ten more resolve to the
generic janitor persona with no error: `memory_consolidator`,
`judge_evaluator`, `strategy_evaluator`, `query_decomposer`, `memory_briefer`,
`skillopt_optimizer`, `equation_lab`, `audit_worker`, `translator`,
`Translator`. The fallback now logs once per name; the ten are **pinned as an
open item, not changed** — re-mapping them is a model-behaviour decision.

---

## Open items — deliberately not done here

| # | Item | Why it waits |
|---|---|---|
| 1 | **Re-climb the ladder (Phases C→D→E)** | needs Gold Spark. Different model ⇒ numbers not comparable to the pinned baselines; the judge's 49,151-token prompt leaves only ~8k headroom in the Jetson's 65,536 window, and on a *different tokenizer*; the Jetson's knee is 8 concurrent at ~911 prefill tok/s against ~900k prompt tokens per 3-ticker rung. |
| 2 | Prove the min_p fix live in a real cycle | the seam is proved by test + direct probe; the cycle-level proof is part of item 1. A single `endpoint_override="jetson"` call would prove it end-to-end today if wanted. |
| 3 | Re-map the ten fallback personas | model-behaviour decision for the operator. |
| 4 | Phase 3 hard readiness gate | plan of record wants ≥1 week of shadow vs `conviction_vector.data_quality` first. |
| 5 | 6-ticker `to_thread` starvation measurement | needs a full-width production cycle. |
| 6 | Softer observe-row readers | 12h re-analysis exclusion, cooldown, freshness gate, morning briefing, `/verdicts`. Lower blast radius; left visible rather than silently filtered. |
| 7 | trading-client ch.109 results chapter | belongs with the re-climbed numbers, not these. |

⚠️ **A trap for whoever does item 1:** `DECISION_MODEL_PATTERN="deepseek"` is
checked **only** when `endpoint_key == "dgx_spark"`
(`prism_agent_caller.py:437`). Overriding to `"jetson"` skips the contract
entirely, so forcing a rung onto the Jetson would *silently succeed* on a model
the decision agents were never built for.

---

## Nothing here is deployed

The container still runs `bed708d`. Every fix above is proved by test and by
read-only measurement; none has been observed in a live cycle. The deploy is
deliberately left for when the operator wants it — and note that two of these
commits repair the interlocks that guard deploys.
