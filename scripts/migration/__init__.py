"""Postgres-era modules, kept for migration and parity tooling only.

These are NOT part of the application image's import graph. `cycle_main.py`
and everything under `app/` run against Mongo; the only psycopg importer in
the repo is `pg_connection.py` in this package, and its callers are the
backfill, parity and one-off analysis scripts that read the frozen Postgres
backup.

Moved here from `app/db/` at teardown (2026-08-18) rather than deleted: the
parity checks still have to read the source store. The DDL that used to run
at boot (`_init_schema` / `_seed_and_migrate` / `run_migrations`) is now
reachable only from these scripts, so a `DROP TABLE` no longer comes back on
the next boot.
"""
