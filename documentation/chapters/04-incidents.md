# Incidents

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

## Diagnostic notes

- Container clocks are **PDT**; cycle IDs encode **UTC** epochs.
- A dead cycle's errors are filed under `cycle_id='system-log'` in
  `cycle_audit_log` — query by time window, not by cycle ID.
- `wd-` prefixed IDs in `pipeline_events` are Watch Desk trips, not cycles.
- Cycles with explicit tickers bypass discovery *and* the gatekeeper. A working
  watch-desk cycle is not evidence that discovery works — this is exactly why
  the 07:48 cycle succeeded while the scheduled ones returned nothing.
