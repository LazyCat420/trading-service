# trading-service — working agreement

`AGENTS.md` is the source of truth for harness-level pipeline constraints. This
file covers how a session should be run.

## Read this first

`documentation/chapters/02-current-state.md` and `03-open-items.md` record what
is already verified working and what is already known broken. Reading them
first is how a session avoids spending its opening hour re-deriving either.
`01-agent-pipeline.md` holds the invariants that cause outages when violated.

## Documentation is part of the work

**Every report goes in `documentation/`, never into a hosted artifact.**
Findings, architecture notes, status summaries, incident write-ups — all are
chapters in `documentation/chapters/*.md`, rendered by:

```bash
python3 documentation/build_docs.py          # rebuild index.html
python3 documentation/build_docs.py --check  # fail if the page is stale
```

Markdown is the source of truth; `index.html` is generated and never
hand-edited. Update the documentation **in the same change as the code**:

- shipped a fix → what it proved, with evidence, in `02-current-state.md`
- found something broken → `03-open-items.md`, even if not fixing it now
- diagnosed a failure → `04-incidents.md`; the reasoning outlives the patch
- discovered an invariant → `01-agent-pipeline.md`

A hosted report expires from reach and cannot be corrected in place. A chapter
is versioned beside the code it describes, greppable, and diffable.

The full house style — page construction, diagram rules, writing standards —
is in `trading-client/documentation/chapters/06-report-standards.md`.

## Operational notes

- **This service runs on the NAS** (`10.0.0.16:3031`). A second instance
  pointed at the shared database will claim production cycles: any process that
  can reach Postgres is an equal claimant. This caused a real outage on
  2026-08-05 — see `04-incidents.md`.
- **A deploy restarts the container and kills any in-flight cycle.** Check
  `pipeline_state` before deploying.
- **The container has no `psql` and no `asyncpg`.** Use
  `from app.db.connection import get_db` for Postgres and `pymongo` for Mongo.
- **Migrations run at boot** from `app/db/connection.py`, and a failing
  migration is logged as a warning while the service starts anyway. Verify
  schema changes landed; do not assume.
