#!/usr/bin/env python3
"""Build a COMPLETE trading database at the isolated test target.

Table creation in this service is split three ways and no single command ever
built all of it:

  1. `app/db/schema_pg.sql`      — 311 statements, run by `_init_schema`
  2. `run_migrations()`          — `_safe_add_column` plus some whole tables
  3. lazy per-module DDL         — `shared_desk` is created by
                                   `app/v3/desk_persistence.py` at first use,
                                   and twenty-odd others like it

So `:5433/trading_bot_test` sat at 161 tables against production's 214, every
`real_db` test was gated behind a flag nobody could turn on, and the resulting
failures read as code bugs (`UndefinedColumn`) rather than as an incomplete
schema. This script runs all three, in order, against a target it has proved is
not production.

    export TRADING_BOT_TEST_DB=1                    # required, always
    python3 scripts/init_test_db.py                 # create or top up
    python3 scripts/init_test_db.py --check         # report only, issue no DDL
    TRADING_BOT_TEST_DB_RESET=1 \
      python3 scripts/init_test_db.py --reset       # drop everything first

THE GUARD IS THE POINT
----------------------
This issues DDL, and `--reset` destroys data. Every check below is fail-closed,
and the safe target is DERIVED FROM CONFIG rather than hardcoded — a hardcoded
allowlist drifts away from the database it is meant to guard, and the version
of this plan that proposed `{localhost, 127.0.0.1, trading-db-test}` had it
exactly backwards: the test database is `10.0.0.16:5433`, while `localhost` is
where `DATABASE_URL` points **production**.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from dataclasses import dataclass
from urllib.parse import urlparse

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# TRADING_BOT_TEST_DB=1 is REQUIRED FROM THE OPERATOR and is never set here.
#
# It does double duty: `validate_test_target` refuses without it, and
# `_ensure_pool` reads it to decide whether `get_db()` binds to
# TEST_DATABASE_URL or to production — which is what makes the lazy DDL in
# step 3 land on the right server. A script that sets its own precondition
# satisfies its own check, and it would also mean importing this module was
# enough to flip the whole process onto the test database; doing exactly that
# turned two skipped `real_db` tests into two failures in an unrelated suite.

TEST_DB_NAME_RE = re.compile(r"trading_bot_test(?:_[a-z0-9_]+)?$")


class UnsafeTarget(Exception):
    """The DSN is not provably the isolated test database. Nothing was run."""


@dataclass(frozen=True)
class Target:
    scheme: str
    host: str
    port: int
    database: str
    dsn: str

    def redacted(self) -> str:
        """The DSN with any password removed — safe to print before DDL."""
        parsed = urlparse(self.dsn)
        if parsed.username:
            auth = parsed.username + (":***" if parsed.password else "")
            netloc = f"{auth}@{self.host}:{self.port}"
        else:
            netloc = f"{self.host}:{self.port}"
        return f"{self.scheme}://{netloc}/{self.database}"


def parse_dsn(dsn: str) -> Target:
    """Parse a URI DSN. Key-value DSNs are REJECTED, not guessed at.

    `urlparse` is happy to return an empty hostname for a string it does not
    understand, and an empty hostname compared against an allowlist is the
    shape that lets `dbname=trading_bot host=prod` through a host check.
    """
    if not dsn or not dsn.strip():
        raise UnsafeTarget("empty DSN")
    dsn = dsn.strip()
    parsed = urlparse(dsn)
    if parsed.scheme not in ("postgresql", "postgres"):
        raise UnsafeTarget(
            f"DSN scheme must be postgresql:// or postgres://, got {parsed.scheme!r} "
            "(key-value DSNs are not accepted)"
        )
    host = (parsed.hostname or "").lower()
    if not host:
        raise UnsafeTarget("DSN has no host")
    database = (parsed.path or "").lstrip("/")
    if not database:
        raise UnsafeTarget("DSN names no database")
    return Target(parsed.scheme, host, parsed.port or 5432, database, dsn)


def validate_test_target(
    dsn: str,
    *,
    reference_dsn: str,
    production_dsn: str,
    env: dict[str, str] | None = None,
    for_reset: bool = False,
) -> Target:
    """Prove the DSN is the isolated test database, or raise. Fail-closed.

    `reference_dsn` is `settings.TEST_DATABASE_URL` — host and port must match
    it exactly. `production_dsn` is `settings.DATABASE_URL`, which must not be
    the same database on the same server, however the name is spelled.
    """
    env = os.environ if env is None else env
    target = parse_dsn(dsn)
    reference = parse_dsn(reference_dsn)

    if env.get("TRADING_BOT_TEST_DB") != "1":
        raise UnsafeTarget("TRADING_BOT_TEST_DB=1 is required")

    if (target.host, target.port) != (reference.host, reference.port):
        raise UnsafeTarget(
            f"target {target.host}:{target.port} is not the configured test server "
            f"{reference.host}:{reference.port} (TEST_DATABASE_URL)"
        )

    if not TEST_DB_NAME_RE.fullmatch(target.database):
        raise UnsafeTarget(
            f"database {target.database!r} does not match "
            f"{TEST_DB_NAME_RE.pattern!r} — refusing to touch it"
        )

    try:
        production = parse_dsn(production_dsn)
    except UnsafeTarget:
        production = None
    if production is not None and (
        (target.host, target.port, target.database)
        == (production.host, production.port, production.database)
    ):
        raise UnsafeTarget("target is the production database")

    if for_reset and env.get("TRADING_BOT_TEST_DB_RESET") != "1":
        raise UnsafeTarget(
            "--reset destroys every table: set TRADING_BOT_TEST_DB_RESET=1 as well"
        )

    return target


# ── steps ────────────────────────────────────────────────────────────────

def _table_count(dsn: str) -> int:
    import psycopg

    with psycopg.connect(dsn, autocommit=True, connect_timeout=10) as conn:
        row = conn.execute(
            "SELECT count(*) FROM information_schema.tables "
            "WHERE table_schema = 'public' AND table_type = 'BASE TABLE'"
        ).fetchone()
    return int(row[0])


def _drop_everything(dsn: str) -> None:
    import psycopg

    with psycopg.connect(dsn, autocommit=True, connect_timeout=10) as conn:
        conn.execute("DROP SCHEMA public CASCADE")
        conn.execute("CREATE SCHEMA public")


# The lazy DDL entry points, named rather than discovered. A registry that
# scans for `_ensure_*` and calls whatever it finds is a registry that will one
# day call something that is not DDL; `tests/unit/test_test_db_builder.py`
# fails when a new lazy CREATE TABLE appears that is not listed here, which
# gives the same coverage without the reflection.
LAZY_DDL: tuple[tuple[str, str], ...] = (
    ("scripts.migration.pg_init_db", "run_auto_migrations"),
    ("app.db.checkpoints", "ensure_checkpoints_table"),
    ("app.db.memory_repo", "_ensure_schema"),
    ("app.analytics.returns_engine", "_ensure_tables"),
    ("app.autoresearch.component_health", "ensure_health_table"),
    ("app.autoresearch.variance", "_ensure_table"),
    ("app.quant.regime_hmm", "ensure_posterior_table"),
    ("app.quant.trial_registry", "ensure_table"),
    ("app.services.worklist_shadow", "_ensure_table"),
    ("app.v3.agent_chat", "_ensure_table"),
    ("app.v3.challenger", "_ensure_table"),
    ("app.v3.desk_persistence", "_ensure_table"),
    ("app.v3.invariants", "_ensure_table"),
    ("app.v3.model_shadow", "_ensure_shadow_table"),
    ("app.v3.telemetry", "_ensure_telemetry_table"),
    ("app.v3.telemetry", "_ensure_guardrail_table"),
)

# Lazy DDL that needs a live cursor handed to it.
LAZY_DDL_WITH_DB: tuple[tuple[str, str], ...] = (
    ("app.db.evolution_repo", "_ensure_table"),
    ("scripts.migration.pg_db_migrations", "ensure_summary_columns"),
    ("scripts.migration.pg_db_migrations", "ensure_source_quality_table"),
)


def build(target: Target, *, reset: bool) -> int:
    import importlib

    print(f"target      : {target.redacted()}")
    print(f"tables now  : {_table_count(target.dsn)}")

    if reset:
        print("reset       : DROP SCHEMA public CASCADE")
        _drop_everything(target.dsn)
        print(f"tables now  : {_table_count(target.dsn)}")

    from scripts.migration.pg_connection import _init_schema, get_db
    from scripts.migration.pg_migrations import run_migrations

    print("step 1/3    : schema_pg.sql")
    _init_schema(target.dsn)
    print(f"  -> tables : {_table_count(target.dsn)}")

    print("step 2/3    : run_migrations()")
    import psycopg

    with psycopg.connect(target.dsn, autocommit=True, connect_timeout=30) as conn:
        run_migrations(conn)
    print(f"  -> tables : {_table_count(target.dsn)}")

    print("step 3/3    : lazy per-module DDL")
    failures: list[tuple[str, str]] = []
    for mod_name, fn_name in LAZY_DDL:
        try:
            fn = getattr(importlib.import_module(mod_name), fn_name)
            fn()
        except Exception as e:
            failures.append((f"{mod_name}.{fn_name}", str(e).splitlines()[0]))
    for mod_name, fn_name in LAZY_DDL_WITH_DB:
        try:
            fn = getattr(importlib.import_module(mod_name), fn_name)
            with get_db() as db:
                fn(db)
        except Exception as e:
            failures.append((f"{mod_name}.{fn_name}", str(e).splitlines()[0]))

    final = _table_count(target.dsn)
    print(f"  -> tables : {final}")

    if failures:
        # Named, not counted. "3 lazy DDL steps failed" sends the reader back
        # to the source; the names say which tables are missing.
        print(f"\n{len(failures)} lazy DDL step(s) FAILED:")
        for name, err in failures:
            print(f"  {name}  ->  {err}")
    return final


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--reset", action="store_true",
                    help="DROP SCHEMA public CASCADE first (needs TRADING_BOT_TEST_DB_RESET=1)")
    ap.add_argument("--check", action="store_true",
                    help="validate the target and report table count; issue no DDL")
    ap.add_argument("--dsn", default=None,
                    help="override the target (still fully validated)")
    args = ap.parse_args()

    from app.config import settings

    dsn = args.dsn or settings.TEST_DATABASE_URL
    try:
        target = validate_test_target(
            dsn,
            reference_dsn=settings.TEST_DATABASE_URL,
            production_dsn=settings.DATABASE_URL,
            for_reset=args.reset,
        )
    except UnsafeTarget as e:
        print(f"REFUSED: {e}", file=sys.stderr)
        return 2

    if args.check:
        print(f"target      : {target.redacted()}")
        print(f"tables now  : {_table_count(target.dsn)}")
        return 0

    build(target, reset=args.reset)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
