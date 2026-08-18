"""`init_test_db.py` issues DDL, and `--reset` destroys data. Prove the guard.

Every check is fail-closed and the safe target is DERIVED FROM CONFIG. A
hardcoded host allowlist drifts away from the database it guards: the plan that
proposed `{localhost, 127.0.0.1, trading-db-test}` had it backwards, because
the test database is `10.0.0.16:5433` while `localhost` is where
`DATABASE_URL` points **production**.

These tests never connect to anything, and importing the script must not
change the environment: `TRADING_BOT_TEST_DB=1` also decides which database
`get_db()` binds to, so a module-level `setdefault` would switch the whole
pytest session onto the test database. It did, and it turned two skipped
`real_db` tests in an unrelated suite into two failures.
"""

from __future__ import annotations

import importlib.util
import pathlib
import sys

import pytest

_SCRIPTS = pathlib.Path(__file__).resolve().parents[2] / "scripts"


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, _SCRIPTS / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


init_test_db = _load("init_test_db")
UnsafeTarget = init_test_db.UnsafeTarget

TEST_DSN = "postgresql://10.0.0.16:5433/trading_bot_test"
PROD_DSN = "postgresql://localhost:5432/trading_bot"
OK_ENV = {"TRADING_BOT_TEST_DB": "1"}


def _validate(dsn: str, *, env=None, for_reset: bool = False):
    return init_test_db.validate_test_target(
        dsn,
        reference_dsn=TEST_DSN,
        production_dsn=PROD_DSN,
        env=OK_ENV if env is None else env,
        for_reset=for_reset,
    )


# ── what must be accepted ────────────────────────────────────────────────

def test_the_configured_test_database_is_accepted():
    target = _validate(TEST_DSN)
    assert (target.host, target.port, target.database) == ("10.0.0.16", 5433, "trading_bot_test")


def test_a_suffixed_test_database_on_the_same_server_is_accepted():
    """Per-worker databases: trading_bot_test_gw0, _gw1, …"""
    assert _validate("postgresql://10.0.0.16:5433/trading_bot_test_gw0").database == "trading_bot_test_gw0"


def test_the_postgres_scheme_alias_is_accepted():
    assert _validate("postgres://10.0.0.16:5433/trading_bot_test").scheme == "postgres"


# ── what must be refused ─────────────────────────────────────────────────

def test_production_is_refused():
    with pytest.raises(UnsafeTarget):
        _validate(PROD_DSN)


def test_the_production_database_name_on_the_test_server_is_refused():
    """Right server, wrong database — the name check is not decorative."""
    with pytest.raises(UnsafeTarget, match="does not match"):
        _validate("postgresql://10.0.0.16:5433/trading_bot")


def test_a_foreign_host_is_refused_even_with_the_right_database_name():
    with pytest.raises(UnsafeTarget, match="not the configured test server"):
        _validate("postgresql://10.0.0.99:5433/trading_bot_test")


def test_the_wrong_port_on_the_right_host_is_refused():
    with pytest.raises(UnsafeTarget, match="not the configured test server"):
        _validate("postgresql://10.0.0.16:5432/trading_bot_test")


def test_a_key_value_dsn_is_refused_rather_than_parsed_loosely():
    """`urlparse` returns an empty host for this, which would sail past a host check."""
    with pytest.raises(UnsafeTarget, match="scheme"):
        _validate("host=10.0.0.16 port=5433 dbname=trading_bot_test")


def test_a_non_postgres_scheme_is_refused():
    with pytest.raises(UnsafeTarget, match="scheme"):
        _validate("mysql://10.0.0.16:5433/trading_bot_test")


def test_a_name_that_merely_starts_with_the_test_prefix_is_refused():
    """`trading_bot_testing` is not `trading_bot_test` — fullmatch, not prefix."""
    with pytest.raises(UnsafeTarget, match="does not match"):
        _validate("postgresql://10.0.0.16:5433/trading_bot_testing")


def test_a_name_that_merely_contains_the_test_string_is_refused():
    with pytest.raises(UnsafeTarget, match="does not match"):
        _validate("postgresql://10.0.0.16:5433/prod_trading_bot_test")


def test_the_environment_flag_is_required():
    with pytest.raises(UnsafeTarget, match="TRADING_BOT_TEST_DB=1"):
        _validate(TEST_DSN, env={})


def test_the_flag_must_be_exactly_one():
    with pytest.raises(UnsafeTarget, match="TRADING_BOT_TEST_DB=1"):
        _validate(TEST_DSN, env={"TRADING_BOT_TEST_DB": "true"})


def test_reset_needs_its_own_second_flag():
    with pytest.raises(UnsafeTarget, match="TRADING_BOT_TEST_DB_RESET=1"):
        _validate(TEST_DSN, for_reset=True)


def test_reset_is_allowed_with_both_flags():
    env = {"TRADING_BOT_TEST_DB": "1", "TRADING_BOT_TEST_DB_RESET": "1"}
    assert _validate(TEST_DSN, env=env, for_reset=True).database == "trading_bot_test"


def test_an_empty_dsn_is_refused():
    with pytest.raises(UnsafeTarget):
        _validate("")


# ── the password must not be printed ─────────────────────────────────────

def test_the_printed_dsn_redacts_the_password():
    target = _validate("postgresql://bot:hunter2@10.0.0.16:5433/trading_bot_test")
    printed = target.redacted()
    assert "hunter2" not in printed
    assert "bot:***@10.0.0.16:5433/trading_bot_test" in printed


# ── the lazy-DDL registry must not silently fall behind ──────────────────

def test_every_lazy_ddl_entry_point_is_registered():
    """A new lazy CREATE TABLE that nobody registers is a table the builder misses.

    That is the whole reason the test database sat 53 tables short: the DDL
    existed, ran at first use in production, and no build step knew about it.
    """
    import ast
    import re

    app_root = pathlib.Path(__file__).resolve().parents[2] / "app"
    registered = {
        (m, f)
        for m, f in (*init_test_db.LAZY_DDL, *init_test_db.LAZY_DDL_WITH_DB)
    }
    # Run by the builder's first two steps, not by the lazy registry.
    known_elsewhere = {
        ("app.db.connection", "_init_schema"),
        ("app.db.migrations", "run_migrations"),
        ("app.db.migrations", "_fix_eth_cagr_data"),
        ("app.db.migrations", "_create_decision_scores"),
        ("app.db.migrations", "_create_persistent_research_tables"),
        # Not schema: a local sqlite cache and a runtime profiler table.
        ("app.scraper.core.failure_cache", "__init__"),
        ("app.monitoring.pipeline_profiler", "_persist_to_db"),
    }

    found: set[tuple[str, str]] = set()
    for path in sorted(app_root.rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        if "CREATE TABLE" not in text.upper():
            continue
        tree = ast.parse(text)
        module = str(path.relative_to(app_root.parent)).replace("/", ".")[: -len(".py")]
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                seg = ast.get_source_segment(text, node) or ""
                if re.search(r"CREATE TABLE", seg, re.I):
                    found.add((module, node.name))

    unregistered = sorted(found - registered - known_elsewhere)
    assert not unregistered, (
        "these functions create tables but no build step runs them, so a fresh "
        f"test database will be missing those tables: {unregistered}"
    )


def test_every_table_the_service_writes_to_is_created_by_something():
    """A table the code INSERTs into but nothing creates is not drift — it is a
    table that only exists because someone made it by hand.

    `v3_system_commands` was exactly that: read and written by
    `research_governor`, `boot_service` and `watch_desk`, present in
    production, created by no statement in this repository, and therefore
    absent from every fresh database — where the failure reads as
    `UndefinedTable` in application code rather than as a missing build step.

    Scoped to a named list rather than every table in the schema: the point is
    to keep these particular holes closed, not to re-derive the whole schema
    from SQL string parsing.
    """
    import re

    repo = pathlib.Path(__file__).resolve().parents[2]
    schema_sql = (repo / "app" / "db" / "schema_pg.sql").read_text(encoding="utf-8")
    py_ddl = "\n".join(
        p.read_text(encoding="utf-8")
        for p in (repo / "app").rglob("*.py")
        if "CREATE TABLE" in p.read_text(encoding="utf-8").upper()
    )
    creatable = schema_sql + "\n" + py_ddl

    for table in ("v3_system_commands", "cycle_checkpoints", "shared_desk",
                  "whiteboard_entries", "v3_agent_telemetry"):
        assert re.search(rf"CREATE TABLE IF NOT EXISTS {table}\b", creatable, re.I), (
            f"{table} is used by the service but no statement in this repository "
            "creates it — a fresh database will not have it"
        )
