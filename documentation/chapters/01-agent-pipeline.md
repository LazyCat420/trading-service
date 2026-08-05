# From an agent call to an artifact

Most cycle failures are agent failures wearing a disguise. This chapter traces
the path an agent call takes and names the places where a failure stops looking
like one.

## Prompt assembly is the load-bearing step

`run_agent()` in `app/agents/base_agent.py` composes a prompt from several
sources: the static system prompt, injected data context, prior-outcome
context, and — the dangerous one — advisory guidance pulled from the database.

The danger is that advisory context is *unbounded by nature*. It comes from a
table that another subsystem writes to on a schedule, so its size is not a
property of this code at all. When `tool_playbook` accumulated duplicates, the
junior analyst's prompt reached **130,982 characters** without a single line of
this module changing.

Prism does not truncate an over-long prompt. It returns a harness error:

```
Error: The conversation's context window is critically full — only 0 output
tokens remain out of a 0 token window.
```

Which arrives downstream as "agent produced no parseable artifact".

> **Invariant.** Anything appended to a prompt from a database must be bounded
> at the point of injection, not just at the point of writing. Two guards are
> deliberate redundancy: `_get_tool_playbook_tips` caps at
> `_PLAYBOOK_MAX_CHARS = 2000`, *and* `tool_playbook` carries a unique natural
> key. Either alone would have prevented the outage; the writer guard can be
> lost to a schema change, and the reader guard cannot.

## Empty responses become a sentinel string

When an agent returns no content, `run_agent` substitutes:

```python
if not content or not str(content).strip():
    content = f"Agent failed: empty response from {agent_name}"
```

This is convenient and quietly hazardous. The failure is now a *string in the
response field*, indistinguishable in type from a real answer. Every caller
that does `parse_json_response(result["response"])` gets `{}` and must decide
for itself whether that means "the model said nothing useful" or "the call
failed".

Callers must therefore check for the **presence of the expected key**, not the
truthiness of the parsed value:

```python
# Wrong — a failure and a genuine empty selection are identical here.
selected = parsed.get("selected_tickers", [])
if not selected:
    end_cycle()

# Right — only a parsed verdict that CARRIES the key is a decision.
if "selected_tickers" not in parsed:
    degrade_loudly()
```

`pipeline_service.py` does this for the gatekeeper. Any new agent whose empty
output would change control flow needs the same treatment.

## The circuit breaker aborts the ticker, not the cycle

`app/v3/guardrails.py` retries a failed phase once, then trips:

```
[V3Runner] v3_junior_analyst produced no parseable artifact for LLY
[CircuitBreaker] Phase 'junior_analyst' retry 1/1 on AGENT_ERROR
[V3] LLY: Circuit breaker tripped on junior_analyst — aborting pipeline
[SharedDesk] cycle-v3-…/LLY: Phase INIT → ABORTED (outcome: AGENT_ERROR)
```

A result is still saved for the aborted ticker, and the cycle proceeds to the
debate stage. A cycle can therefore reach "complete" having produced nothing of
value — the per-ticker aborts are the signal, and they are one log level below
where anyone looks.

## Cycle commands are claimed, not addressed

`cycle_main.poll_system_commands()` claims from `v3_system_commands` with
`SELECT … FOR UPDATE SKIP LOCKED`. That guarantees exactly one claimant and
says nothing about *which* process wins — every instance pointed at the shared
database is an equal candidate, including a stale container elsewhere on the
network.

Claims are stamped with `WORKER_ID` (`nas-prod/<sha>`, from `WORKER_NAME` and
`GIT_SHA` written by the deploy script) so that "who ran this cycle?" is
answerable from one log line rather than by diffing container code against
master.

## Migrations run at import time

`app/db/connection.py` calls `run_migrations(conn)` during pool setup, so
schema changes in `app/db/migrations.py` apply on container start. Two
consequences worth knowing:

- A migration that raises is caught and logged as
  `[DB] Migration warning: …` — the service still starts. Schema changes are
  therefore best-effort and must be verified, not assumed.
- Because `schema.sql` runs before migrations, a column added in both places
  must be idempotent in the migration (`_safe_add_column`).

Partial unique indexes have a sharp edge here: Postgres will not infer one for
`ON CONFLICT` unless the statement repeats the index predicate. Omit it and
every write raises, which a surrounding `except` turns into a log line while
row counts continue to look healthy.
