# HANDOFF — Mongo migration: scope, and the tools that gate it (2026-08-17)

Shipped and deployed as **`3868181`** (three commits: `ac802fd`, `6b1e1bc`,
`3868181`). The container was verified to carry it — `master@3868181 → synology`,
`Up (healthy)`, `verify_shipped.py` 9 pass / 0 fail — and the shipped flag map
was read back off the NAS at **13 tables**, unchanged. A deploy of this repo is
a migration event; this one moved no table.

> **The full write-up is chapter 72 in `trading-client/documentation/`**, served
> at <http://10.0.0.16:8888/documentation>. That is where this service's chapters
> live by standing agreement — see `CLAUDE.md`. This file is the in-repo handoff:
> what changed here, which invariants it discovered, and what the next session
> must not re-derive.
>
> ⚠ At the time of writing, :8888 still serves a build from before ch.70 — the
> trading-client deploy is blocked by uncommitted files belonging to another
> session, and its Dockerfile copies the working tree. Read the chapter from the
> repo until that clears.

---

## What changed here

### The ledger's scope was wrong in both directions

`app/db/migration_ledger.json` described 183 tables against a database of 214.
Measured against live Postgres, the gap was not all foreign:

- **Four live tables are ours and were outside the migration.** They are created
  at runtime by a script rather than declared in `schema.sql`, so the manifest
  never saw them — and since the ledger's scope came from the manifest, no
  amount of live traffic could pull them in. `box_benchmark_runs` was being
  written the same evening this was measured.

  | table | rows | proof of ownership |
  |---|---|---|
  | `box_benchmark_runs` | 93 | `scripts/jetson_benchmark.py:486` |
  | `agent_registry` | 1 | `trading-client/app/client_agents/base_agent.py:75` |
  | `agent_tasks` | 0 | `trading-client/app/client_agents/base_agent.py:138` |
  | `autofix_runs` | 0 | `scripts/autofix/run_autofix.py:272` |

- **Three are dead and now say so** rather than staying `UNCLASSIFIED`:
  `llm_traces` (21,454 rows), `context_telemetry` (789), `fallback_data` (0) —
  all last written 2026-06-25, zero code references. `archive-only`.
- **`rejected_symbols` does not exist.** It held a `migrate` disposition and a
  collection-map entry with no table behind it. It was the only `migrate` row
  with a null `row_count` and the only table whose spec could not be generated:
  two visible signals, neither wired to anything.

Scope: **158 → 161 migrate**, 25 → 28 archive-only, 1 `absent`.
`known_tables()` now enumerates 161 real tables and the phantom no longer aborts
a sweep.

Adoption is a **positive list** in `scripts/build_migration_ledger.py`
(`ADOPTED` / `RETIRED`), matching the ownership rule already in that file — an
inference is not a title deed. Each entry names the write site and declares its
key, because there is no manifest constraint block to read one from, and
`tests/unit/test_migration_ledger.py` checks that declared key against the
**live** primary key.

### The ledger is now a function of committed code

Its classifier scanned **working trees**, across every repo in sun. Shape drives
both disposition and collection prefix, so one parallel session's half-finished
edit could re-file a trading table — `llm_audit_logs` flips `mutable`/`append`
on that alone.

`snapshot_head()` exports each repo at committed `HEAD` (`git archive`) and the
ledger records `scanned_at: "HEAD"`, `dirty_repos_ignored` (**7 repos** were
dirty at that scan) and `working_tree_fallback_repos`. Two `--no-db` runs are
byte-identical. A unit test builds a throwaway repo, commits one string, changes
it on disk, and fails if the scanner sees the uncommitted version.

**Note for whoever regenerates next:** `llm_audit_logs` still resolves to
`append` from HEAD. That is not today's dirty trees — it means the *committed*
ledger disagreed with committed code, so it was generated from a contaminated or
older state. At HEAD there are zero `UPDATE` references to that table, and
`collection_map.json` had already overridden it by hand to `log_llm_calls`
("append-only call log; the ledger shape is unstable here"). The ledger now
agrees with the judgement the map made without it.

### Two checks that could not have caught any of the above

- **`scripts/check_generated_specs.py` had never run.** At clean `542f410` it
  died on its first table with `syntax error at or near "["` — `ORDER BY ['id']`.
  When composite-key support landed, `spec_for()` began returning the key as a
  *list*, and this script still interpolated it into SQL. Every "the generator
  agrees with the hand-written mappers" claim rested on it. Fixed by normalising
  both spec flavours to a column list, quoting them, and keying composite
  documents on a tuple. It now reports **9/10 agree**; `pipeline_events` differs
  by the known `data_json → data` rename, which all **372,608** live documents
  use, and that override already existed.
- **`scripts/pg_to_mongo_backfill.py` addressed Mongo by raw table name** at two
  sites (`get_doc_db()[table]`) and never imported the resolver, while
  `mongo_store._coll()` resolves through `collection_for()`. Inert today — the
  map is the identity function while `apply_renames:false` — and armed the moment
  renames flip: the writer would fill a collection nothing reads, and
  `--verify-all` would read the new one empty and report **every row missing**,
  indistinguishable from total mirror failure on the one tool whose job is to be
  believed about parity. Both sites resolve; a test fails on any unresolved
  `get_doc_db()[...]`.

### New tooling

- **`scripts/prove_mongo.py`** — the per-table evidence bundle (flags, guard,
  counts, provenance, parity, logs; `--json`; exit `0/1/2/3`). Read-only against
  both stores, Postgres session pinned `READ ONLY`. It *observes* the guard
  rather than re-deriving it, and it reports **INSUFFICIENT** rather than PASS
  whenever a check could not run — including a **DEAD FEED** verdict when the
  log stream carries no WARN/ERROR at all, so a zero from a dead feed can never
  read as clean.
- **`scripts/promote_table.py`** — the cutover ceremony as refusals, over four
  tiers (`one-shot` / `full` / `drain` / `replay`). Dry-run is a *measurement*:
  it mirrors the files to a tempdir, really applies the edit and really runs
  `check_backend_map.py` against the mirror. It edits both repos' maps and
  reverts itself if byte-identity fails afterwards.

---

## Invariants discovered (do not re-derive)

1. **A parity verdict is only as good as its join key and its comparator.**
   `embeddings` — the one table considered cut over — was recorded as missing
   701 vectors and drifting 728 timestamps. Both are artifacts. `vector_store`
   re-keys with a fresh UUID on re-embed and dedups on
   `(source_table, source_id)`, while the ledger joins on `id`, so the "missing"
   rows are present under another id; and the 27,956 "drifted" `embedding`
   values are a pgvector list compared against a BSON `Binary` (max abs
   difference `3.7e-9` — the same vector). **`--verify-all` is therefore not a
   valid oracle for any table whose natural key is not `id`, and nothing detects
   that class.** The sample that overturned this was 3 rows: enough to disprove
   the claim, not enough to certify the table. Re-sweep on
   `(source_table, source_id)` before calling `embeddings` anything.
   `pg_write_guard.py:233-235` describes the same 701 in the opposite direction
   and should be corrected with it.
2. **A schema-derived manifest cannot see a table a script creates.** Any
   `CREATE TABLE` outside `schema.sql` is invisible to the manifest, therefore
   to the ledger, therefore to the migration — silently, and regardless of how
   live its writer is. Adopt it or the cutover removes its store.
3. **A generated artifact that reads working trees is not reproducible**, and
   the damage is silent because it lands as a *classification*, not an error.
4. **An unresolved collection name is inert until the day it is catastrophic.**
   The collection map moved ~245 call sites for free precisely because they all
   went through one resolver; the ones that did not are invisible until renames
   activate.

---

## Open items for the next session

- **`trade_results` is not promotable as it stands.** It sits at `mongo_read`,
  so Mongo already serves its reads, and 8 rows carry `internal_consensus_score`
  / `dynamic_trigger` / `policy_action` in Postgres while the Mongo document
  omits the field entirely (537 of 1,065 documents lack
  `internal_consensus_score` against 498 non-null in Postgres, newest written
  2026-08-17 20:09). The live writer still emits a column subset for some rows.
- **Two sources disagree about `trade_results`' shape.** The ledger classifies
  it `money`/`dec128`; `collection_map.json` deliberately overrides that
  (ch.64 — decision parameters, not settled amounts) with a written
  `numeric_policy_reason`. `promote_table.py` consults the ledger, so it refuses
  anything but `--tier replay`. It fails **closed**, so this is a correctness
  note rather than an incident; resolve it by teaching the tool the map's
  recorded override, not by loosening the money rule.
- **`build_migration_ledger.py` regenerates `promoted_*` as `null`**, so the
  durable per-table cutover record is `promote_table --json`, not the ledger
  stamps. Fix on the generator side if those stamps are meant to mean anything.
- Phase 0 **0.4b** (dead-letter queue for failed mirror writes — the mirror
  still drops rows permanently: 196 / 196 / 119 / 14) and **0.6** (the ledger's
  state machine is still all `null`) remain open.
- `agent_tasks` classified `mutable` on zero rows, but its columns (`status`,
  `result_json`, `completed_at`) read like a work queue. Re-check its shape
  before choosing a ceremony if it ever takes rows — a queue gets
  drain-cutover, never dual-write.
- `source_strain_records` is the last owner-unclassified live table
  (treesearch-inferred, 0 rows).

## Verification

```
scripts/check_backend_map.py      OK: 13 flagged tables agree (7 dual, 1 mongo, 5 mongo_read)
scripts/check_collection_map.py   OK: 161 tables -> 161 unique collections; renames inert
scripts/check_generated_specs.py  9/10 agree (pipeline_events differs by a known override)
tests/unit                        4071 passed, 2 failed
```

The two failures are **pre-existing and order-dependent**: the same two fail on
the clean primary checkout at `542f410` (4041 passed, 2 failed) and both pass
when `tests/unit/test_context_blob_timestamp_parity.py` runs alone in either
tree. Measured, not assumed.
