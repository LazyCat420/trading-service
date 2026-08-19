# trading-service — working agreement

`AGENTS.md` is the source of truth for harness-level pipeline constraints. This
file covers how a session should be run.

## Read this first

**The documentation for this service lives in `trading-client/documentation/`,
served at `http://10.0.0.16:8888/documentation`.** Read *Current state* and
*Open items* there before starting; *From an agent call to an artifact* holds
the harness invariants that cause outages when violated.

## Documentation is part of the work

> **`trading-service/documentation/` was deleted on 2026-08-07.** It was copied
> into no container, so ten chapters — the whole gatekeeper and Jetson thread —
> were readable only by opening this repo. Two sessions independently
> re-derived facts that were already written there, because the document could
> not be opened. Every chapter was absorbed into `trading-client`, which is the
> only `documentation/` in this stack that a server actually serves.

**Every report goes in `trading-client/documentation/chapters/*.md`** — plans,
findings, architecture notes, status summaries, incident write-ups — and never
into a standalone markdown file or a hosted artifact. Write the chapter there
and name this service in it; do not move the chapter to the code.

```bash
cd ../trading-client
python3 documentation/build_docs.py          # rebuild index.html
python3 documentation/build_docs.py --check  # fail if the page is stale
npm run deploy                               # the page serves the CONTAINER
```

Every chapter must name its part — in `documentation/chapters/_parts.json`
(canonical; it also controls order) or via a front-matter `part:` line, which
files it at the end of that part. Only a chapter with **neither** fails the
build (measured 2026-08-16; this file used to claim any unmanifested chapter
fails, which was false). Prefer the manifest.
Update the documentation **in the same change as the code**:

- shipped a fix → what it proved, with evidence, in *Current state*
- found something broken → *Open items*, even if not fixing it now
- diagnosed a failure → *Incidents*; the reasoning outlives the patch
- discovered an invariant → *From an agent call to an artifact*

If it is not visible at that URL, it is not written down.

## Operational notes

- **This service runs on the NAS** (`10.0.0.16:3031`). A second instance
  pointed at the shared database will claim production cycles: any process that
  can reach Postgres is an equal claimant. This caused a real outage on
  2026-08-05 — see `04-incidents.md`.
- **A deploy restarts the container and kills any in-flight cycle.** Check
  `pipeline_state` before deploying.
- **The container has no `psql`, no `asyncpg`, and — since 2026-08-18 — no
  psycopg either.** The service is on Mongo: use `pymongo`, or the
  `app/db/mongo_query.py` helpers that mirror the old cursor API. Nothing under
  `app/` may import a Postgres driver; `tests/unit/test_app_image_has_no_pg_driver.py`
  fails the build if one comes back.
- **No migrations run at boot any more.** `app/db/connection.py` moved to
  `scripts/migration/pg_connection.py` at teardown, taking `_init_schema()` and
  `run_migrations()` with it; boot now runs `init_mongo_schema()` only. The
  Postgres DDL is retained there deliberately, for the backfill and parity
  scripts that still read the frozen Postgres backup.
- **Dropping a table means deleting its DDL in the same change.** The
  `CREATE TABLE IF NOT EXISTS` blocks in `scripts/migration/pg_migrations.py`
  and `schema_pg.sql` recreate anything a `DROP TABLE` removes, the next time
  a migration script runs. That is how 40 of 57 "purged" tables came back.
