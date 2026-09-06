# Audit: does the agents' long-term memory loop actually close?

Read-only audit, trading-service, 2026-09-06. All numbers below are live reads
from `trading_bot` Mongo (`.env` `PRISM_MONGO_URI`), taken via
`.venv/bin/python`, scripts under `scratchpad/q*.py`. No file, DB row, or
deploy state was changed.

## Headline

The consolidator (`app/services/memory/consolidator.py`) runs, and the
janitor (`app/autoresearch/janitor.py`) runs and correctly deletes what it
should. But the entire output — every canonical memory either subsystem
produces — currently reaches **zero** agent prompts, because the one flag
that gates injection into the live pipeline (`MEMORY_CONTEXT_ENABLED`)
defaults to `0` and has **never once** been set in `runtime_parameters`
(0 rows, ever). Confirmed at the runtime-data level: of the 300 most recent
`shared_desk` documents, 83 explicitly stamp
`cycle_metadata.memory_context_state = "off"`; zero stamp `"on:*"`.
So today, the answer to "is memory working" is: **the write side limps
along, the read side is dead by configuration.**

Separately, and independent of that flag: even on the write side, 100% of
tickers that are currently past the promotion threshold have a canonical
memory that is either absent or over a week stale — the gate is open but the
work is not landing at the rate new observations accrue.

## 1. Pipeline health (per-ticker)

- `episodic_observations`: 1004 total → **757 unpromoted**, 247 promoted.
- `canonical_memories`: **219 total**, 212 with `status != 'deprecated'`
  (65 distinct tickers have ever had one).
- `consolidation_reports`: 127 total (oldest run 2026-07-16, newest
  2026-09-06).

**Gate-closed check** (unpromoted ≥ `NEW_EPISODIC_THRESHOLD` = 5, canonical
memory absent or not touched — by `updated_at`, the generous measure — in the
last 7 days):

- 51 tickers are currently over the promotion threshold.
- **51 / 51 (100%) have no canonical-memory refresh in the last 7 days** —
  30 have no canonical memory at all, 21 have one but it is 12–48 days stale
  (worst: AMD/JNJ/MCD/CRM/GS at 44–48 days). Zero tickers over threshold have
  a memory touched within the last week.
- Top backlogs: JPM (22 unpromoted, last touched 21d ago), TSM (15, 20d),
  META (12, 34d), LITE (11, no canonical memory ever), MP (10, none),
  PLTR (10, 29d), FCF (10, 20d).

This was double-checked two ways — `created_at` (first-write) and
`updated_at`/`created_at` coalesced (last-touch, since the consolidator
upserts into an EXISTING memory id rather than always minting a new one) —
and the 100%-stale result holds under both.

## 2. Does it run at all?

`maybe_consolidate` is called from exactly one place:
**`app/v3/orchestrator.py:1953-1955`**, fire-and-forget
(`asyncio.create_task`) at the end of a completed V3 cycle, once per ticker
per cycle — so its firing rate is bounded by how often each ticker gets a V3
cycle, further gated inside `maybe_consolidate` by the ≥5-unpromoted
threshold and a 6-hour per-ticker cooldown
(`app/services/memory/consolidator.py:25-38`).

Store-side, over the last 14 days (`consolidation_reports`):

| metric | value |
|---|---|
| runs w/ real work (`observations_consumed > 0`) | 19 → **~1.36/day** |
| runs total (incl. "nothing extracted") | 22 → ~1.57/day |

That is **not zero, but not remotely enough** for ~250 tickers that
regularly cross the 5-observation threshold (see §1) — at this rate a single
ticker's backlog would take weeks to clear even if nothing else were
competing for the slot, and new observations keep arriving faster than that.

**A real, confirmed 7-day total outage was found inside that window**:
`consolidation_reports` has **zero rows for 2026-08-26 through 2026-09-02**
inclusive (checked day-by-day). This was NOT a quiet pipeline — in the same
window, `cycle_run_summaries` (6-11/day) and `episodic_observations`
(6-50/day, `source_type='v3_pipeline'`) both continued completely normally,
so V3 cycles were running and calling `maybe_consolidate` throughout. The
failure is specific to the consolidation call path.

**Why the DB can't say more**: `log_consolidation_run` (the only writer of
`consolidation_reports`) is called from exactly two places in
`run_ticker_consolidation` — the success path and the "LLM returned nothing
parseable" path
(`app/services/memory/consolidator.py:187-195` and `:236-244`). The
`except asyncio.TimeoutError` / `except Exception` paths that return
`"transient"`/`"failed"` (`consolidator.py:250-263`) write **nothing** to the
store. So a week where every attempt fails at the transport/LLM layer is
*indistinguishable in the DB* from a week where the code never ran at all —
a confident zero. The only corroborating evidence of a live transient-failure
class is in the code itself: a comment dated **2026-09-06**, the same day as
this audit, citing a live-observed failure
(`consolidator.py:33-36`: "Server disconnected without sending a response"
for NBIS, cycle-v3-1788660665), which is what motivated the
`TRANSIENT_RETRY_SECONDS` fast-retry path already in the code. `agent_audit_log`
/ `llm_audit_logs` have essentially no rows for this agent past 2026-06-17
(2 and 13 rows respectively, all in June) — those audit tables are not
comprehensive for every Prism-routed agent, so they could not corroborate or
refute the gap either.

## 3. The janitor

Found: **`app/autoresearch/janitor.py`**, invoked from
**`app/autoresearch/core.py:374`** (`run_janitor()`) inside
`run_autoresearch`, which itself runs once per `AUTORESEARCH` system-command
job (queued once per cycle by the pipeline; polled every 5s by
`app/autoresearch/eval_worker.py:poll_system_commands`).

The 30-day rule: `EPISODIC_PROMOTED_RETENTION_DAYS = 30`
(`janitor.py:26`), enforced by `_prune_promoted_observations()`
(`janitor.py:99-112`) → `MemoryStore.delete_promoted_observations_older_than`
(`app/services/memory/store.py:69-80`):
```
mongo_store.delete_docs('episodic_observations',
    {'promoted_to_memory': True, 'created_at': {'$lt': cutoff.isoformat()}})
```
**It only ever deletes already-*promoted* `episodic_observations` rows.
`canonical_memories` are never hard-deleted anywhere in the codebase** —
`deprecate_canonical_memories` only flips `status` to `'deprecated'`
(`app/db/memory_repo.py:89-100`); grepped the whole tree for a
`delete`/`drop` on `canonical_memories` and found none.

- **Deletion count (historical)**: not recorded anywhere (`run_janitor`'s
  return dict is logged, not persisted) — the code comment in `store.py:69-80`
  claims `created_at` is stored as an ISO string compared lexicographically,
  which would be **wrong** on its face (BSON Date always ranks above String,
  so a string cutoff would never match a Date field and the delete would
  silently no-op) — except `mongo_store.delete_docs` runs every filter
  through `app/db/date_fields.coerce_filter`, which (per
  `schema_manifest.json`, where `episodic_observations.created_at` is
  `timestamp with time zone`) converts the string cutoff to a real
  `datetime` before the query executes. Verified this is actually working,
  not just theoretically fixed: consolidations started **2026-07-16** (52
  days ago), yet the **oldest surviving promoted row is only 29 days old**
  (2026-08-07) — everything promoted in the ~3-week gap between those two
  dates is gone, which is exactly what a functioning 30-day janitor produces
  and could not happen by accident.
- **Would delete in the next 7 days**: **84** promoted rows currently sit
  between 23 and 30 days old and will cross the retention cutoff in that
  window (0 are past it *right now* — the oldest is 29 days, cutting it
  close, consistent with the janitor running at least daily).
- **Orphan risk** (canonical memory deleted while its ticker's unpromoted
  queue is non-empty): **not possible as coded** — the janitor deletes only
  rows already marked `promoted_to_memory=True` (i.e. already consumed into
  a canonical memory), never touches `canonical_memories`, and never touches
  unpromoted rows (`store.py:71-72`, explicit in the docstring). The
  "memory disappears and is never rebuilt" scenario the task worried about
  does not exist in the current code — the actual risk is the opposite one
  found in §1/§2: memories that were never built in the first place, or
  were built once and then never refreshed.

## 4. What the memories contain

Sampled 10 at random (`canonical_memories`, active status) plus full corpus
stats (n=219): summary length **min 126 / median 441 / mean 477 / max 1040**
chars.

They are **specific and dated, not generic prose** — every sample carried
real numbers (RSI, P/E, D/E, ROE, price levels, dated catalysts) and named
mechanisms, e.g.:

> ORCL (601 chars): "...Strong fundamentals (20-21% revenue growth,
> AI/NVIDIA moat, PEG ~0.73-0.80, forward P/E ~12.5-13.3...) are consistently
> offset by...extreme leverage (D/E ~388), S&P downgrade to BBB- on
> 2026-07-21..., ~$23.7B FCF burn, net debt/EBIT 5.57x...kept the verdict at
> HOLD (55-65% confidence) across multiple cycles."

> UBER (822 chars): "...CRITICAL UPDATE: the prior conclusion that 'HOLD (no
> entry) is correct' has been contradicted by outcomes — the HOLD stance
> produced repeated HOLD_MISS results (+9.54%, +1.24%, +1.24%)... Correct
> stance should lean toward accumulation..."

That UBER example is a genuine self-correction (a later consolidation pass
revising an earlier stance against realized outcomes) — evidence the
mechanism, when it fires, does real synthesis rather than restating input.
No sampled memory was vacuous boilerplate.

**Prompt-token cost of the memory block, per run**: the honest number is
**0**, because §5 below shows the injection path is switched off. If
`MEMORY_CONTEXT_ENABLED` were turned on: `MemoryRetriever.build_memory_brief`
caps the canonical-memory portion at `MAX_BRIEF_CHARS=3000`
(`retriever.py:13`), and observed per-ticker combined lengths (top-10 active
memories) ranged 1.9k–4.7k chars (AAPL 4394, C 4728, NVDA 3056, AMZN 2180,
JPM 1911) before that cap — so realistically ~1.9k–3k chars ≈ **~475–750
tokens** for the canonical-memory portion alone, plus up to ~1500 chars
(≈375 tokens) each for up to 4 more capped "addenda" blocks
(`app/services/retrieval_context.py:20`, working-memory / retrieved-context /
brain-graph / macro), for a combined "## Past Cycle Memory" section
plausibly **~750–1,900 tokens**. Because the injection has no per-agent gate
(unlike alt-data/valuation blocks), turning the flag on would apply that cost
to **every** agent in the desk (~10 agents: regime engine, quant/fundamental/
valuation/junior analysts, board, bull/bear/bull-defense, debate judge,
decision synthesizer) — i.e. ~10x that per ticker-cycle.

## 5. Who reads them

- **`app/v3/orchestrator.py:468-504`** (`_build_memory_task`) — the one
  producer into the live prompt path. Gated at line 471 by
  `get_param("MEMORY_CONTEXT_ENABLED")`
  (`app/services/parameter_store.py:112-117`, `default=0`). **This param has
  zero rows in `runtime_parameters` — ever** (only `HMM_REGIME_MODE`,
  `MAX_PORTFOLIO_DRAWDOWN_PCT`, `MAX_POSITION_SIZE_PCT` have any rows there),
  so it always resolves to its coded default: **off**. When on, it calls
  `MemoryRetriever.retrieve`/`build_memory_brief`
  (`app/services/memory/retriever.py`) plus
  `build_memory_addenda` (`app/services/retrieval_context.py:243-255`) and
  stores the combined text at `desk.cycle_metadata["memory_context"]`.
- **`app/v3/agent_runner.py:1057-1059`** — the one consumer: every agent
  whose prompt is built through this shared dynamic-sections path gets
  `## Past Cycle Memory\n{memory_context}` appended, with **no agent_name
  filter** (unlike neighboring blocks in the same function, e.g. the
  valuation/alt-data blocks a few lines up, which ARE agent-scoped) — so it
  is all-agents-or-none.
- **`app/services/memory/briefing.py:18`** (`generate_memory_brief`) is a
  second, older memory-brief builder with **zero importers anywhere in the
  tree** (confirmed by grep and by `app/cognition/evolution/target_map.py:68`'s
  own comment: "memory_briefing removed 2026-08-28 ... had no importer") —
  dead code, not a real consumer.

**Runtime confirmation** (not just static analysis): sampled the 300 most
recent `shared_desk` docs (`desk_data` is JSON text, parsed per doc) —
`cycle_metadata.memory_context_state` is `"off"` in 83, absent in 217
(older/lighter cycle shapes that don't run this task), and **`"on:*"` in
zero**. No live desk in this sample ever carried a memory block.

## Other findings

- `app/db/memory_repo.py`'s module docstring claims table ownership of a
  `memory_usage_logs` collection alongside the three real ones. **That
  collection does not exist in the database** (checked
  `db.list_collection_names()`) and has zero writers anywhere in the app —
  a doc claim with no producer, same shape as other findings in this
  codebase's history.

## Numbers behind the summary JSON

- `runs_per_day_14d` = 1.36 (real consolidations, `observations_consumed>0`,
  last 14 days: 19 runs / 14 days). Includes a confirmed 7-day (2026-08-26 →
  2026-09-02) complete outage with no corroborating failure record.
- `tickers_over_threshold_but_stale` = 51 (of 51 over-threshold tickers — 100%)
- `canonical_total` = 219 (212 active)
- `unpromoted_total` = 757
- `janitor.would_delete_7d` = 84, `orphans_possible` = false (reasoned above)
- `prompt_tokens_per_run` = 0 (confirmed live; ~750-1,900 if the flag were
  turned on)
