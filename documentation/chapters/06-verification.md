# Verifying the 2026-08-06 wave

Written **2026-08-06**, after five commits shipped in one session: the `min_p`
root cause (`6e9ebd4`), the benchmark (`07e51a4`), the transport rule and the
gatekeeper shadow (`5f42260`), and their documentation.

Everything in that wave was tested, and the suite was green at 3,048. This
chapter is about the difference between *green* and *verified*, what closing
that gap found, and what still cannot be answered without a cycle.

## Why re-verify a green suite

Of the tests that shipped with the wave, **twelve assert on
`inspect.getsource(...)`** — that a function's text contains a string. That
family of test is useful for pinning a decision that has no runtime seam, and
it is what most of the wiring claims rested on:

```python
assert 'kwargs["min_p"] = resolved_min_p' in src
assert "dispatch_shadow(" in src
assert 'if result and result.get("response"):' in src
```

None of those can distinguish *code that exists* from *code that runs*. The
wave itself produced the proof: `test_base_agent_output_schema` patched only
the `AgentHarness` seam, so after the transport reroute its tool-less cases
were making **real network calls** and still passing. The tests were green
against a path they no longer exercised.

So the verification was built in four layers, each answering something the one
below it cannot.

<div class="status-grid">
  <div class="tile ok"><div class="label">Unit suite</div><div class="value">3,105</div><div class="note">was 3,048; +57 new</div></div>
  <div class="tile ok"><div class="label">Live acceptance</div><div class="value">5 / 5</div><div class="note">0 fail, 0 warn</div></div>
  <div class="tile warn"><div class="label">Defects found</div><div class="value">2</div><div class="note">both in the shipped shadow</div></div>
  <div class="tile warn"><div class="label">Gatekeeper shadow rows</div><div class="value">0</div><div class="note">still needs a cycle</div></div>
</div>

## Layer 1 — follow the value, don't read the source

`tests/unit/test_min_p_reaches_the_wire.py` drives `run_agent` with a fake
`BaseAgent` and asserts on the **constructor kwargs**, then calls the SDK's own
payload builder and asserts on the **JSON**:

| claim | how it is now checked |
|---|---|
| a local-box call carries `min_p=0` | `BaseAgent(**kwargs)` captured, `kwargs["min_p"] == 0.0` |
| a cloud model is left alone | same capture, `min_p` absent |
| the SDK puts it on the wire | `PrismClient.get_stream_payload_and_url(min_p=0.0)` → `payload["minP"] == 0.0` |
| omitting it is the known-bad shape | same call with `min_p=None` → no `minP` key |
| `/chat` sends it too | `httpx` intercepted, real payload inspected |

`tests/unit/test_transport_routing.py` gained the same treatment: three tests
that run `run_agent` and assert **which seam was awaited**, in both directions.
The truth table was already behavioural; the wiring claims were not.

## What prism actually does with `minP` — a correction

The commit message says prism "injects `minP=0.05` into every `/agent` call".
Read from `prism-service` while writing these tests, that is not the rule.
`ChatRoutes.prepareGenerationContext` applies the agent defaults here:

```js
if (agent) {
  const agentDefaultValues = getAgentDefaults();   // includes minP: 0.05
  for (const [k, v] of Object.entries(agentDefaultValues))
    if (options[k] == null) options[k] = v;
}
```

The trigger is **the `agent` field in the request body**, not the endpoint —
`/chat` and `/agent` share that function. The SDK's `/agent` payload always
sets `agent`; `chat_toolless` happened to omit it. That omission, not the URL,
is why `/chat` measured 10/10 non-empty while `/agent` failed 0/10.

This matters more after `5f42260` than before it: the transport rule routes
**every tool-less role** through `chat_toolless`, so most of the desk's LLM
calls were being protected by the absence of a field nobody would recognise as
sampling configuration. Adding `agent` for persona attribution — an obvious,
harmless-looking change — would have silently restored `GATEKEEPER_DEGRADED` on
every local box.

`chat_toolless` now sends `minP` explicitly, derived from the same `min_p_for`
decision function as the `/agent` path rather than a second copy of the rule,
and a test holds both the value and the continued absence of `agent`.

## Layer 2 — two defects in the shipped shadow

Both are in `5f42260`, both invisible to a source-text assertion, and both
corrupt the **measurement the Jetson decision is blocked on** rather than
breaking anything loudly.

### The shadow fired on the degraded fallback

`_gatekeeper_unusable` returns a synthetic response — the scoring engine's
top-N wearing the gatekeeper's JSON shape — precisely so that a failure cannot
read as a verdict downstream. It carries a non-empty `response`, so the shipped
guard accepted it:

```python
if result and result.get("response"):     # ← a fallback passes this
    dispatch_shadow(...)                  #   primary_text = the scoring engine
```

Three routes reach that fallback (timeout, call failure, unparseable output),
and **four of them happened on 2026-08-06 alone**. Rows produced that way would
have been compared under the `>= 9 of 10 agreement` rule in
[the Jetson plan](#jetson-plan) as though the primary were a gatekeeper
decision, when it was a sort.

Fixed by marking the fallback (`"degraded": True`) at the point it is built,
declining it in the dispatcher, and moving the dispatch **below** the parse
check so the third route is covered too.

### Every shadow row would have booked the primary at 0 ms

`chat_toolless` never returned `execution_ms`, and the dispatch reads
`result.get("execution_ms")` for `primary_elapsed_ms`. Since the gatekeeper's
primary runs through that helper, every gatekeeper row would have recorded the
primary as instant — next to a shadow timed properly, which is the direction
that flatters the shadow box.

The dispatch is now `pipeline_service.maybe_shadow_gatekeeper()`, a module-level
function that **returns whether it fired**, so its refusals are assertable
rather than being a debug log. `tests/unit/test_gatekeeper_shadow_dispatch.py`
covers the good path, all four refusals, and both swallow-and-continue paths.
Removing the degraded guard makes exactly one of them fail — checked, because a
guard test that passes in both states is not a test.

## Layer 3 — the benchmark measures what the desk sends

`scripts/jetson_benchmark.py` is where the transport decision was measured and
where the Jetson's fitness will be re-measured. Its arms are hand-built payload
dicts sitting *next to* the production callers, not derived from them — and the
`/chat` arm had already drifted, because production gained `minP` that day.

`tests/unit/test_benchmark_parity.py` holds the chat arm to the fields
`chat_toolless` sends, holds the agent arm to the SDK's shape, and holds the
**known-bad arm broken**: `agent-nominp` must keep omitting `minP` entirely.
A control that gets "fixed" turns the next "it works now" into an unfalsifiable
claim.

## Layer 4 — `scripts/verify_shipped.py`, against the live system

The unit suite proves the code is right *in this checkout*. Three things decide
whether the desk is actually fixed, and none of them are visible from here:
whether the deployed container runs this code, whether prism and the boxes
still behave as diagnosed, and what the database has recorded since.

```
python3 scripts/verify_shipped.py                 # everything
python3 scripts/verify_shipped.py --skip-live     # no LLM calls
```

It is **not** a reliability measurement and not a CI gate — it answers "is this
wired up and alive" in a handful of calls; `jetson_benchmark --phase
reliability` answers "how often" at n≥10, and only that number should ever be
quoted as a rate.

First run, 2026-08-06:

| check | result |
|---|---|
| Jetson answers `/v1/models` | serving `cyankiwi/Qwen3.6-35B-A3B-AWQ-4bit` |
| `min_p=0.0` on `/agent` | **2/2 non-empty**, median 2,776 ms |
| omitting it still fails | **0/2 non-empty** — `EMPTY_RESPONSE`, as diagnosed |
| production `/chat` helper | 21 chars in **535 ms** |
| `execution_ms` populated | yes — shadow rows will record a real primary latency |

The A/B is interleaved and keeps the known-bad arm. If that arm ever stops
failing, the script reports it as a **warning, not a pass**: it would mean the
diagnosis has expired, not that the fix got better.

The deployment probe runs the check inside the container over `ssh`, and its
first useful output was a red one — it correctly reported the NAS as behind
this checkout while these fixes were still local.

## Checked afterwards, because the first run only covered one box

`verify_shipped.py` exercises the **Jetson**. The gatekeeper does not resolve
there — `resolve_default_model_for_agent('v3_portfolio_manager')` returns
`deepseek-v4-flash-0731 / vllm-2`, i.e. **Gold Spark** — so the box that would
actually have broken if the new explicit `minP` were unwelcome was the one
untested. Checked directly against the deployed helper: **21 chars in 528 ms,
38 tokens**, clean JSON. Both boxes accept the field.

The same pass found that `bot_id` is passed to `dispatch_shadow` by both call
sites, accepted by `_run_and_record`, and then dropped — there is no such
column. Pre-existing, does not affect the comparison, recorded as open item 1e.

## What is still not verified

**The gatekeeper shadow has zero rows**, and nothing here changes that. The
dispatch only fires during a real cycle, no cycle has run since it was
deployed, and a cycle runs the desk and can place trades — it is the user's to
trigger. Everything measured so far still describes a **tool-less** job.

Worse, the rows will *stay* tool-less when they arrive: the gatekeeper's own
call site calls `chat_toolless` directly (`1755c3d`, a parallel session), so
`transport_for` never runs for it. Its 13-tool whitelist and the rule-6
instruction to call `get_parameters` still disagree with its route. That is not
hidden — `/chat` is what made the gatekeeper work again — but it means the
n≥10 rows answer *which box*, not *what the catalog costs*.
`TestTheGatekeeperIsStillOutsideThisRule` asserts the contradiction so that
resolving it either way requires updating a test on purpose.

The `AGENTS.md` line describing `/chat` as tool-less is still unamended, and
nothing yet explains what happened to the Jetson on 2026-06-25 — 42 days ago
as of this run, and still the last local-box call in `llm_audit_logs`.
