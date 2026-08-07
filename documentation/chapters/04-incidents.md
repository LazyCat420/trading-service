# Incidents

## 2026-08-06 — A bench "off the critical path" threw away the desk's stock selection

`cycle-v3-1786072624`, the first real cycle run after the transport wave. The
gatekeeper worked perfectly and chose nine tickers with a written rationale:

> FB, CPS, GEN, RNGR, MU, RDDT, ASML, USFR, MSBT — *"high relative volume and
> fresh institutional/trending catalysts…"*

The next line in the log:

```
ERROR [PipelineService] Portfolio screener failed, falling back to AAPL:
cannot access local variable 'active_bot_id' where it is not associated with a value
```

The cycle analysed **one hardcoded ticker** and reported success.

### Cause

`maybe_shadow_gatekeeper(..., bot_id=active_bot_id, ...)` referenced a local
that is first assigned **~250 lines further down the same function**. Python
evaluates arguments **at the call site**, before the callee is entered — so the
`UnboundLocalError` never reached the `try` inside the helper, which exists
precisely to keep a bench from touching the cycle. The screener's own handler
caught it and degraded.

Two things made it invisible:

* **Every unit test passed**, and still would: they all call the helper
  directly, so none of them ever evaluates the pipeline's argument list.
* The `5f42260` version had the *same* unbound reference, but inline, inside a
  `try` that swallowed it. There the bug was a silent no-op — the shadow simply
  never fired. Extracting the helper in `f073679` moved argument evaluation
  outside the guard and converted a silent no-op into a destructive one.

### Fix — `fd62533`

The signature no longer accepts anything the caller has to compute; the bot id
is resolved **inside** the guard, where failing costs the bench and nothing
else. (`_record` drops it anyway — see open item 1e. It was a required argument
for a value the table has no column for.)

The regression test parses the call site with `ast` and asserts every name in
it is one the gatekeeper block owns. Put the old argument back and it fails.

### What it cost, and what it says

One cycle's ticker selection. Both cycles were paper-only, so no position was
opened on the wrong symbol — `app/trading/paper_trader.py` is the sole
execution path by architectural invariant.

The lesson is the same one this repo keeps paying for and is worth stating
plainly: **a guarded callee does not protect its own call site**, and a
degraded decision still looks like a healthy cycle. It was caught only because
a real cycle was run and its log was read line by line — the unit suite,
the live acceptance check, and the deployed-container probe were all green
through it.

## 2026-08-05 — Cycles processed 0–1 tickers while reporting success

Three consecutive cycles appeared healthy in the client: one ticker, then zero,
then zero. Three independent causes were stacked, which is why fixing only the
visible one would have looked like a failed fix.

### An impossible conflict clause

```sql
INSERT INTO tool_playbook (id, ...) VALUES (%s, ...)
ON CONFLICT DO NOTHING          -- id is a fresh uuid4: never conflicts
```

`ON CONFLICT DO NOTHING` against a random primary key is a no-op. The table
reached **4,948 rows growing ~831/day**, collapsing to **63** distinct natural
keys — 98.7% duplication. Those rows are injected into agent prompts, so
`v3_junior_analyst` prompts reached 130,982 characters containing 1,387
identical lines, prism rejected them, and the circuit breaker aborted every
ticker.

The natural key could not be the sequence text: it embeds live statistics
(`avg score: 94.2 over 104 uses`) and changes almost every run. `tool_name` had
to become a real column.

> `ON CONFLICT DO NOTHING` is only a deduplication guard when a conflict is
> *possible*. Against a random key it silently does nothing, forever.

### A failed agent recorded as a decision

The gatekeeper returned nothing, `parse_json_response` produced `{}`,
`selected_tickers` defaulted to `[]`, and the cycle ended **green**:

```
Gatekeeper chose 0 tickers. Ending cycle early. Rationale:
```

Twenty eligible candidates were in hand — RDDT at a 0.92 freshness delta, SHOP
up 30.3% — and all were discarded.

> A component that cannot answer has not answered "no". Failure and refusal
> need different code paths, or the system reports confident decisions it never
> made. The empty rationale was the tell.

### A stale instance claiming production cycles

A local container built **2026-06-26**, six weeks behind master, shared the
command queue with the NAS. It claimed the 13:45 and 14:00 UTC cycles, killed
each in ~1.5s, and wrote no `pipeline_events` — invisible in the UI. Its crash
left `pipeline_state='running'`, so the healthy NAS instance skipped its own
14:00 cycle as "stuck from a previous crashed cycle".

> `FOR UPDATE SKIP LOCKED` guarantees a single claimant, not the *right* one.
> Unstamped claims make "who ran this?" unanswerable from logs.

### A verification that passed while the code was broken

The first check of the upsert fix:

```
rows before: 63 -> after 2 runs: 63
VERDICT: UPSERT OK — no growth
```

Wrong. The count held because `update_tool_playbook()` raised on **every** row:
Postgres will not infer a *partial* unique index for `ON CONFLICT` unless the
statement repeats the predicate, and the exception was swallowed into one log
line. The writer was entirely dead and the metric read healthy.

The corrected check asserts three things — row count stable,
`last_validated_at` refreshed (proving the UPDATE path ran), and no errors
logged.

> A check that passes for both the working and the broken state is not a check.
> Ask what the metric would read if the thing were **entirely** broken; if the
> answer is "the same", measure something else.

### A permanent failure disguised as a transient one

Found while verifying the deploy: `startup_tasks.py` imported
`_V3_AGENT_MODULES`, a static list replaced by `_discover_v3_agent_modules()`
without this call site being updated. Because the `ImportError` sits inside a
retry loop, it printed 36 identical `Retrying in 5s...` lines and read as a slow
dependency. The readiness check — prism health, endpoint model resolution,
agent registration — had been dead since `c276d1d`. Fixed in `3653899`.

> A retry loop around a deterministic error manufactures the appearance of a
> transient fault. If every attempt fails identically, it is not transient.

## 2026-08-06 — The Gatekeeper's "empty response" was a sampling parameter

`GATEKEEPER_DEGRADED` fired four times on 2026-08-06 and twice on 08-05, every
one of them `Agent failed: empty response from v3_portfolio_manager`. Each
occurrence silently demoted ticker selection from the Gatekeeper's judgement to
raw top-N-by-score — the cycle stayed green.

The cause is not the model, the prompt, or the box. Prism's `ParameterRegistry`
gives `minP` an `agentDefault` of **0.05** and injects it whenever the caller
omits the field, which `trading-service` always did — `AgentHarness.run` never
forwarded `min_p`, so no `BaseAgent` caller could set it. vLLM running
speculative decoding refuses any `min_p > 0`:

```
ValueError: The min_p and logit_bias sampling parameters are not yet
supported with speculative decoding
```

and it raises that **inside the stream generator, after already answering HTTP
200**. Prism therefore receives an empty stream rather than an error, and
reports a successful call with no content.

Measured against the Jetson, interleaved rounds, identical prompt, one variable
changed:

| arm | non-empty | valid artifact | median |
|---|---|---|---|
| as production sent it | **0/3** | 0/3 | 1.8s |
| `min_p=0.0` | **3/3** | **3/3** | 52.7s |

`0.0` is vLLM's own default, so the fix restores standard sampling rather than
tuning it. Shipped as a `min_p` passthrough in lazycat-sdk `0.3.10` plus
`base_agent.min_p_for()`, which sends `0.0` to the local vLLM boxes only and
leaves cloud models on the gateway default.

> A provider that answers `HTTP 200` and *then* fails inside its own stream is
> indistinguishable from a terse model. The failure has to be diagnosed from
> the request that produced it, not from the empty response it returned.

Two corrections to the record fell out of the same measurement:

- **The `/agent` tool floor is not 275 tools / 91,255 tokens.** It measures
  **83 tools / ~21k tokens**, leaving **38,179** available output tokens on the
  Jetson's 65k window. `/agent` reaches the Jetson fine; the older figure
  predates whatever narrowed the catalog and should not be used to justify
  routing anything to `/chat`.
- **`enable_tools=False` does not remove tools.** It is a client-side flag that
  only stops the SDK sending schemas; prism attaches its catalog server-side
  regardless, and the SDK's own comment notes prism forces the agentic loop.

## Diagnostic notes

- Container clocks are **PDT**; cycle IDs encode **UTC** epochs.
- A dead cycle's errors are filed under `cycle_id='system-log'` in
  `cycle_audit_log` — query by time window, not by cycle ID.
- `wd-` prefixed IDs in `pipeline_events` are Watch Desk trips, not cycles.
- Cycles with explicit tickers bypass discovery *and* the gatekeeper. A working
  watch-desk cycle is not evidence that discovery works — this is exactly why
  the 07:48 cycle succeeded while the scheduled ones returned nothing.
