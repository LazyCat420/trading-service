# What can be known about a cancelled agent's token spend?

Read-only audit, 2026-09-06. Primary: `/home/lazycat/github/projects/sun/trading-service`.
SDK: `/home/lazycat/github/projects/sun/lazycat-sdk`. prism-service read for evidence only.

**Verdict: C (provider-derived) — and it is not a close call. The number is not
merely estimable; it is *exactly* reconstructable. prism's `requests` ledger
reconciles to the trading-service telemetry to the token.**

But the headline finding is upstream of the A/B/C question: **193 of 584
non-success rows (33.0%) already had their spend measured in-process and threw
it away at the telemetry call**, worth 11,817,729 tokens. Only 39 rows (6.7%)
are the "in-flight usage unknown" case the decision is actually about.

---

## 1. Where the number comes from today

### The commit point, and what a cancellation destroys

`lazycat/agent.py` `AgentHarness.run()`:

| line | what happens |
|---|---|
| 289 | `request_usage: dict = {}` — a **local**, re-declared per loop iteration |
| 296 | `async for data in iter_sse_json_lines(resp.aiter_lines(), ...)` |
| 354-357 | `elif event_type == "usage_update": ... request_usage = usage` — arrives **mid-stream** |
| 370-371 | `finally: await resp.aclose()` |
| **373-379** | **`if request_usage: self.last_usage = request_usage; self.total_tokens += ...`** |

The commit at 373 is **outside and after** the `async for`. Every `usage_update`
already received lives only in the local `request_usage` until the stream ends
normally. An `asyncio.CancelledError` raised inside the `async for` runs the
`finally` at 370, then propagates — **line 373 is never reached and every
delivered usage snapshot is discarded.**

This is not a provider gap. It is a statement-ordering defect in the SDK.

### Why `usage_update` arrives long before the stream ends

prism forces the agentic loop **server-side** (`AgentRoutes.ts:174
agenticLoopEnabled: true`), so one SDK iteration wraps N prism ReAct passes.
`ReActHarness.ts:714` calls `this.emitUsageUpdate()` **inside the loop**, right
after each pass finalizes and before its tools run.
`BaseAgenticHarness.ts:447-460` builds it from `state.overallUsage`, which
`StreamChunkRouter.ts:58` merges **cumulatively** across passes.

Measured proof (ZS / `v3_bear_agent`, 2026-09-06 13:31:19):

```
prism requests, conv 99151c5c, 4 agent:iteration rows
  it1 in=25177 out=  61
  it2 in=25944 out=  31
  it3 in=26413 out=4686
  it4 in=26558 out=3056
  sum_in = 104,092      sum_in+sum_out = 111,926

v3_agent_telemetry row
  prompt_tokens = 104,092      token_usage = 111,926
```

Exact, to the token. This confirms three things at once: `usage_update` is
cumulative; it is emitted per server-side pass (so it is available *during* the
stream); and the ledger is a perfect mirror of what the harness would have
reported.

### The path up into trading-service

`app/agents/base_agent.py`
- 617-619: `partial_cost = cost_sink if cost_sink is not None else {}` — the
  caller-owned accumulator.
- 1064-1087: `harness = AgentHarness(...)`, `final_text = await harness.run(...)`.
- **1126-1141** `finally:` — `partial_cost["tokens"] += harness.total_tokens`,
  `partial_cost["tool_calls"] += tool_call_count`, `partial_cost["loops"] = max(..., tool_call_count+1)`.
- 1166: `exc.partial_cost = dict(partial_cost)` on the escape path.

`tool_call_count` is incremented by `_on_tool_result`, which fires on prism's
**`tool_execution` SSE events** — i.e. it advances *during* the stream. `total_tokens`
does not. That asymmetry is exactly why the surviving rows read `loops=7, tokens=0`.

`app/v3/agent_runner.py`
- 697: `_cost_sink = {"tokens":0,"loops":0,"tool_calls":0}`
- 2421-2425: `_spent(sink, fallback)` → `(loops, tokens)`
- 2219 / 2241 / 2279: TIMED_OUT / CANCELLED / AGENT_ERROR rows, all passing
  `prompt_tokens=_spent_tokens, cost_partial=True`.

**Everything downstream is correct. The plumbing is sound end to end. The single
break is `agent.py:373` sitting after the loop instead of inside it.**

---

## 2. Is C possible? YES — two independent routes, both measured

### C-1: in-band, on the SSE stream

`usage_update` **already reaches the SDK mid-stream**, one per prism pass, with
cumulative `inputTokens`/`outputTokens`/`reasoningOutputTokens`. It is currently
parked in a local and dropped on cancel. No new protocol, no new field, no
cross-DB read — the data is already in the process at the moment of death.

Caveat on the provider layer: the *provider* stream (`openai-compat.ts:822-833`)
yields its `usage` chunk only in the `finally` / on error, so there is **no
sub-pass prefill delta**. Granularity is one completed prism pass, not one
prefill. `continuous_usage_stats` is not set anywhere in prism.

### C-2: prism's `requests` ledger — exact, and sees what C-1 cannot

Location: database **`prism`**, collection **`requests`** (337,900 docs).
One row per prism pass: `operation: "agent:iteration"`, carrying `inputTokens`,
`outputTokens`, `cacheReadInputTokens`, `inputCharacters`, `timeToGeneration`,
`generationTime`, `totalTime`, `conversationId`, `agentConversationId`,
`agent`, `project`, `model`, `agenticIteration`. Written at
`BaseAgenticHarness.ts:1120-1200`; `createdAt` is the pass **start**.

**Does a row exist for a request whose client disconnected? Yes — and rows keep
being written for ten more minutes.** `/agent` sets `persistOnDisconnect: true`
(`AgentRoutes.ts:194`); `SseUtilities.ts:258-280` aborts only the
`connectionController` on socket close, never the `stopController`, which is
abortable solely by `POST /agent/stop`. **trading-service never calls
`/agent/stop`** (grep: only client-side `arm_kill_switch`). So on every timeout
and cancellation, prism finishes the pass, runs the tools, and keeps looping.

**Worked example — the CANCELLED row, reconciled exactly.**
`v3_agent_telemetry`: LULU / `v3_bear_agent` / CANCELLED / `2026-09-06
05:34:29.644` / `elapsed_ms=549274` → run started `05:25:20.370`.
`prism.requests`, `conversationId = fff925dd-6b9d-4057-a6b4-54ad70c95f7e`
(first pass starts `05:25:21.314` — a 0.9 s alignment, so the join is certain):

```
  it1  05:25:21 -> 05:32:01   in=25491 out=8192  = 33,683   COMPLETED BEFORE CANCEL
  it2  05:32:01 -> 05:36:28   in=25636 out=3454  = 29,090   IN FLIGHT AT CANCEL
  it3  05:36:28 -> 05:37:56   in=31206 out=3045  = 34,251   STARTED AFTER CANCEL
  (+ 3 small embed/aux passes)                    =  2,186

  TOTAL ACTUALLY SPENT: 99,210 tokens.   The telemetry row says 0.
    recoverable in-band (C-1, delivered before cancel):  34,607  (34.9%)
    visible ONLY in the ledger (C-2, post-disconnect):   64,603  (65.1%)
```

The same shape holds for the ABT `v3_fundamental_analyst` TIMED_OUT
(`2026-09-06 03:03:45`, 1,800,068 ms, row says `loops=0 tokens=0`): 26
`agent:iteration` rows in that window at 28k-42k input tokens each, **continuing
to 03:13:33** — ten minutes of GPU work after trading-service had given up.

**Two caveats, stated plainly.**
1. *Correlation key.* The telemetry row carries no `conversationId`. The SDK
   holds it in `prism_client._conversations[f"chat-{agent}-{sid[-8:]}"]` and
   exposes `get_conversation_id(agent_name, session_id)` (`llm.py:315-323`), but
   `base_agent` builds the `ConversationSession` locally (line 1058) and never
   returns it. Joining on `(project, agent, time-window)` alone is **ambiguous** —
   measured: 6 concurrent `CUSTOM_V3_DECISION_SYNTHESIZER` conversations inside
   15 minutes. So C-2 needs the conversationId plumbed out (into `cost_sink`,
   say). The cross-DB read itself needs nothing new: `app/routers/eval_trust_router.py:205`
   already does `get_mongo_client()["prism"]["requests"]`.
2. *Orphans.* 49 rows sit at `status: "pending"` with `inputTokens: 0` — the
   skeleton from `RequestLogger.ts:620-665` whose `completePending` never ran
   (one is visible mid-ABT at 02:49:48). A ledger sweep must treat `pending` as
   unknown, not as zero.

---

## 3. What B would cost

### Is a tokenizer available in-process?

**In the deployed image, yes.** `requirements.txt:560 tiktoken==0.13.0`
(`requirements.in:55 tiktoken>=0.7.0`), installed by `Dockerfile:17-18`.
`app/services/context_gate.py:49-70` already lazy-loads `o200k_base` and
`estimate_tokens()` already falls back to `CHARS_PER_TOKEN = 4`.
(The local `.venv` does **not** have it — `import tiktoken` → ModuleNotFoundError.
Local reproduction of any B behaviour will silently take the chars/4 branch.)

So B is not blocked on a dependency. It is blocked on accuracy.

### The measured error

Sample: **4,427 SUCCESS rows** with both `sys_prompt_chars + user_prompt_chars`
and a true `prompt_tokens`.

```
chars per token          median 0.294   p10 0.125   p90 0.580   p1 0.072  p99 1.298
  (single-pass subset, loops_used == 1, n=429)
                         median 0.836   p10 0.535   p90 1.333
```

Estimator error, `est/true` (1.0 = perfect):

| estimator | median | p10 | p90 | median abs err | p90 abs err |
|---|---|---|---|---|---|
| `chars / 4` (the constant already in `context_gate`) | **0.074** | 0.031 | 0.145 | **92.6%** | 96.9% |
| `chars / 0.294` (best single global constant) | 1.000 | 0.425 | 1.970 | **38.7%** | 97.0% |
| per-agent calibrated constant (**upper bound on B**) | 1.000 | 0.572 | 1.619 | **23.5%** | 66.7% |

Note the ratio is ~0.3 chars per token, not ~4. Tokens **outnumber** the
characters trading-service can see by 3.4x, because the real prefill contains
material the process never holds: prism's injected `<system-context>` /
`<agent-memory>` block, and the tool schemas. Measured on one CSCO synthesizer
pass — `requestPayload.messages` = 11,304 chars against `inputTokens` = 25,496.

And the deeper problem is not tokenization at all. The unknown at cancellation is
**how many passes ran**, and each pass re-sends a grown context:
`prompt_tokens/loop` median 30,776 (p10 21,533, p90 39,275) with `loops_used`
median 4, p90 7, max 16. Characters describe pass 1's prompt; the spend is
`Σ passes`. **No tokenizer can close that gap** — which is why even a perfect
tokenizer would not move the table above much.

**Verdict on B: at its best, 23.5% median / 66.7% p90 error. Well past the "label
it differently" threshold the question sets, and it would be a labelled estimate
sitting next to an exact measurement that is already on disk.**

---

## 4. How often it matters

`v3_agent_telemetry`, **9,759 rows over 56 days** (2026-07-12 → 2026-09-06).
Non-success: **584**.

| class | rows | share of non-success |
|---|---|---|
| (A) work happened, tokens **measured**, `prompt_tokens` **dropped at the call site** | **193** | **33.0%** |
| (B) work happened, tokens **genuinely not measured** (the A/B/C question) | **39** | **6.7%** |
| (C) `loops_used == 0` — no model work recorded | 352 | 60.3% |

`prompt_tokens == 0` on **584 of 584** non-success rows (100%). TIMED_OUT +
CANCELLED together: **4 rows in 56 days** = 0.68% of non-success, **0.041% of all
rows**. Since `cost_sink` landed (first `cost_partial` row `2026-09-06
05:34:29`), 3 non-success rows, all 3 in class (B).

**So the pure "cancelled mid-request" case is rare — and class (A) is 5x larger
and is not an estimation problem at all.** Class (A) is a wiring defect:

```
app/v3/agent_runner.py — _record_telemetry call sites
  line 1687  no-parseable-artifact / post-repair-failure  prompt_tokens=NO  cost_partial=NO
  line 1776  decision artifact missing required fields    prompt_tokens=NO  cost_partial=NO
  line 1836  analyst artifact collapsed                   prompt_tokens=NO  cost_partial=NO
  line 2161  SUCCESS                                      prompt_tokens=YES
  line 2219  TIMED_OUT                                    prompt_tokens=YES cost_partial=YES
  line 2241  CANCELLED                                    prompt_tokens=YES cost_partial=YES
  line 2279  AGENT_ERROR (crash)                          prompt_tokens=YES cost_partial=YES
```

At lines 1687/1776/1836 the local `prompt_tokens` (bound at line 1407 from the
completed `run_agent` result) is **in scope and simply not passed**. These rows
carry `token_usage > 0` — 194 of them, summing **11.8M tokens** — while
`prompt_tokens`, the field the 20k invariant and every audit probe read, says 0.
Recent examples: `v3_bull_defense` `tok=202,446 pt=0`; `v3_decision_synthesizer`
`tok=139,189 pt=0`.

---

## 5. The three test paths, and what the row looks like today

### Path 1 — timeout during tool use
- **Trigger:** `app/v3/agent_runner.py:1383` `result = await asyncio.wait_for(run_agent(..., cost_sink=_cost_sink), timeout=timeout_seconds)`; handler `app/v3/agent_runner.py:2206` `except asyncio.TimeoutError:`, row written at `:2219`.
- **Mechanism:** `wait_for` cancels the coroutine and raises its **own** TimeoutError, so nothing rides out on the exception — the caller-owned `_cost_sink` is the only channel. It is read correctly.
- **Row today (CSCO `v3_decision_synthesizer`, `2026-09-06 10:17:07`, `cycle-v3-1788687074`):**
  `TIMED_OUT elapsed_ms=300037 loops_used=1 token_usage=0 prompt_tokens=0 cached_tokens=0 cost_partial=True failure_reason=TIMEOUT sys_prompt_chars=24833 user_prompt_chars=1856 model_used=None`.
  Loops survived; tokens did not. Prompt chars ARE recorded on this path.
- **Older row, pre-sink (ABT `v3_fundamental_analyst`, `2026-09-06 03:03:45`):** identical but `loops_used=0, cost_partial=False` — 18 tool calls in `agent_tool_telemetry` and 26 prism passes in the ledger, all reading as free.

### Path 2 — cancellation during an in-flight model request
- **Trigger:** `app/v3/agent_runner.py:2228` `except asyncio.CancelledError:`, row at `:2241`, then `raise` (re-raised for the orchestrator). Cancellation is delivered into `agent.py`'s `async for` at line 296; `finally: await resp.aclose()` (370) runs; **line 373 does not**.
- **Row today (LULU `v3_bear_agent`, `2026-09-06 05:34:29`, `cycle-v3-1788668370`):**
  `CANCELLED elapsed_ms=549274 loops_used=2 token_usage=0 prompt_tokens=0 cost_partial=True failure_reason=CANCELLED sys_prompt_chars=0 user_prompt_chars=0 model_used=None`.
- **Ground truth from the ledger for that exact run: 99,210 tokens.** Note also
  `sys_prompt_chars=0` — the CANCELLED call at `:2241` does not pass the char
  counts, so **option B is not even implementable on this path without a code
  change of its own.**

### Path 3 — failure during artifact repair after model work accrued
- **Trigger:** repair pass at `app/v3/agent_runner.py:1576` `repair_result = await asyncio.wait_for(run_agent(..., enable_tools=False, cost_sink=_cost_sink), ...)` — the sink is correctly shared, so the repair's spend joins the run's. Local failure handler at `:1614` `except Exception as e:` (sets `repaired=False`, recomputes `elapsed_ms`, swallows).
- **Row today:** written at `:1687` — `_record_telemetry(desk, agent_name, elapsed_ms, loops_used, token_usage, outcome.value, ..., failure_reason=rule.name)`. **No `prompt_tokens=`, no `cost_partial=`.** So `prompt_tokens` defaults to 0 while `token_usage` carries the real first-pass total *plus* `repair_result["tokens_used"]` (added at `:1611`).
- **Concrete (`2026-09-04 21:08:34`, `v3_decision_synthesizer`):**
  `AGENT_ERROR loops_used=3 token_usage=134613 prompt_tokens=0 cost_partial=False failure_reason=SCHEMA_INVALID`.
  **Nothing was lost here. The number was measured, sat in a local, and was not passed.** This is class (A) — 193 rows, 11.8M tokens.

---

## Recommendation

**C**, staged, with a zeroth step that is not on the ballot:

0. **Pass the `prompt_tokens` that already exists** at `agent_runner.py:1687`,
   `:1776`, `:1836`. 193 rows / 33% of non-success / 11.8M tokens, recovered
   with no estimation and no new source. This is the largest single item and
   options A/B/C do not address it.
1. **C-1 (in-band).** Move the usage commit in `lazycat/agent.py` from line 373
   into the `usage_update` branch at 354-357 (assign the cumulative snapshot on
   arrival; keep a per-request base so the loop-level `+=` cannot double-count).
   The timeout and cancel paths then record measured partial usage with no
   cross-DB read. Recovers ~35% of the LULU run's true spend.
2. **C-2 (ledger reconciliation).** Plumb the `conversationId`
   (`prism_client.get_conversation_id(agent_name, session.session_id)`) out
   through `cost_sink` onto the telemetry row, then a post-hoc sweep over
   `prism.requests` (`operation: "agent:iteration"`) closes the row exactly —
   including the 65% of spend that happens *after* trading-service disconnects
   and which C-1 structurally cannot see. Treat `status: "pending"` as unknown.

**Against A:** a 0 with a reason is defensible only when the number is
unknowable. It is knowable, it is exact, and it is already persisted one
database away. A `0` closes a question a blank would keep open.

**Against B:** best case 23.5% median / 66.7% p90 error, on a path
(CANCELLED, `:2241`) that does not even record the characters it would need — and
its dominant error term is the unknown pass count, which no tokenizer can
recover. Shipping a labelled estimate beside an exact measurement is strictly
worse than reading the measurement.

