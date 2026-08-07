# The Jetson plan

Written 2026-08-06 at the end of the session that root-caused the empty
responses. It exists because the next step is **gated on evidence that does not
exist yet**, and without this file the next session would re-derive the same
findings from scratch — which has already happened twice.

## Where things stand

Four things shipped and were verified against the live container:

| commit | what |
|---|---|
| `327a73d` (lazycat-sdk 0.3.10) | `AgentHarness` forwards `min_p` |
| `6e9ebd4` | `base_agent.min_p_for()` — sends `min_p=0` to local vLLM boxes |
| `07e51a4` | `scripts/jetson_benchmark.py` + 20 tests |
| `5f42260` | `transport_for()` routes on the tool declaration; gatekeeper shadow wired |

**The Jetson works now.** It answered a tool-less call in **978ms** through the
deployed service, where the same call returned **zero bytes** before the fix.

**Nothing routes production work to it.** That is open item 1a, and it is
deliberate: assigning a live role is a trading-behaviour change and must follow
the evidence rather than precede it.

## The one blocking measurement

Every box comparison to date describes **`v3_regime_engine`** — a role whose
tools show **zero calls in 60 days**. All of it is therefore evidence about a
*tool-less* job. The gatekeeper (`v3_portfolio_manager`) is the tool-declaring
case: 14 tools in its whitelist, and its own system prompt (rule 6) instructs
it to call `get_parameters`.

Until 2026-08-06 the gatekeeper was **structurally unshadowable** — it does not
run through `agent_runner`, which was the only place that dispatched a shadow,
so `MODEL_SHADOW_AGENTS=v3_portfolio_manager` did nothing at all, silently.
That is now fixed and deployed.

**The shadow only fires during a real cycle.** As of this writing there are
**zero** gatekeeper shadow rows, because no cycle has run since the deploy.

> **UPDATE 2026-08-06 (night).** Cycles have now been run and the dispatch
> works, but the count is still effectively zero and three things below have
> changed:
>
> * **The first row is `AGENT_ERROR`** — a transient model-probe timeout, not
>   a model failure (open item 1f). Useful rows: **0 of 10**. Each cycle
>   contributes one, so this is roughly ten cycles away, not ten minutes.
> * **The rows will be tool-LESS after all.** The gatekeeper's call site
>   bypasses `run_agent`, so `transport_for` never governs it, and both sides
>   of the comparison run `/chat`. It answers *which box*, not *what the
>   catalog costs* (open item 1d). The premise of this section — that the
>   gatekeeper is the tool-declaring case — is **not** what the measurement
>   will deliver.
> * **"14 tools" is 13** as counted from `AGENT_TOOL_WHITELISTS`.
>
> Also settled: the 42-day silence was a **desk-wide outage**, not a Jetson
> fault — zero cycles 06-21→07-13, `dgx_spark` stops on the same day, and the
> roles that used the box were retired. Nothing needs repairing before the box
> can take work; a role needs assigning. See `02-current-state.md`.

## Next session, in order

### 1. Collect gatekeeper shadow evidence (blocking everything else)

```sql
SELECT shadow_outcome, count(*), round(avg(shadow_elapsed_ms))
FROM model_shadow_runs
WHERE agent_name = 'v3_portfolio_manager'
GROUP BY 1;
```

Wait for **n >= 10** before concluding anything. At n=3 a 2/3 result is
compatible with a true success rate from ~15% to ~95% — that is not a finding,
and this session already watched an n=3 read (whitelist arm 2/3 vs 3/3)
evaporate at n=10, where both arms landed on 8/10.

Compare `shadow_text` against `primary_text` on the field that matters: did the
two boxes select the **same tickers**? Latency is secondary — the gatekeeper is
one call per cycle, so +2.6s is irrelevant next to picking different stocks.

### 2. Then, and only then, decide whether the Jetson gets a live role

Candidates by measured loop count (loops multiply the ~1.9x per-token gap and
sit in the cycle's critical path): `v3_regime_engine` 1.1, `v3_delta_analyst`
1.3. Keep the 6-8 loop agentic roles on Gold Spark.

Decision rule to set BEFORE looking: agreement on the selected set in >= 9 of 10
shadow runs, or it does not move.

### 3. Amend the `/chat` rule in `sun/.agents/AGENTS.md`

It currently reads *"never use the `/chat` endpoint (always use `/agent`)"*.
Measured n=10 on a tool-less role: `/chat` 10/10 valid at 16.2s / 2.9s TTFT vs
`/agent` 8/10 at 68.1s / 8.4s. `/chat` is **not** tool-less — prism's
`ChatRoutes` honours `enabledTools` and executes calls via
`ToolOrchestratorService` — so the accurate rule is the one now in code:
**route on the agent's tool declaration**, `/agent` whenever tools are declared.

### 4. Investigate the 2026-06-25 cliff

`llm_audit_logs` holds **12,720** jetson calls between 2026-06-06 and
**06-25**, and nothing after. The box served thousands a day, then stopped
dead. Nobody knows why. This matters before putting it back in the critical
path — whatever silently removed it may still be in place.

## Traps this session paid for

- **`/metrics` cannot tell you a box is idle.** vLLM's counters are since
  process start and die with a restart. 127 requests looks like activity until
  you divide by 50 hours of uptime. Use `--phase inventory`, which snapshots the
  DB attribution alongside them.
- **An HTTP 200 with empty content is not a terse model.** vLLM raises inside
  the stream generator *after* answering 200, so prism reports a successful
  call with no content. Diagnose from the request, not the response.
- **`enable_tools=False` never removed tools** on `/agent` — it is a
  client-side flag; prism attaches its catalog server-side regardless.
- **Tests that patch one transport seam pass for the wrong reason** once
  routing changes. `test_base_agent_output_schema` started making real network
  calls and only failed because the calls were slow.
- **Two sessions can fix the same bug in opposite directions.** This one and a
  parallel session both diagnosed the gatekeeper within an hour; one moved it to
  `/chat`, the other fixed `min_p`. Check `git worktree list` and other
  sessions' branches before starting.
