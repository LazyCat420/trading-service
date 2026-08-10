"""`schema_pg.sql` must not lose its tail to one failing statement.

WHAT WENT WRONG
---------------
`_init_schema` ran the whole 311-statement file through a single
`cur.execute(sql)`. Postgres treats that as one implicit transaction, so the
FIRST failure discards every statement after it — and the caller logs the error
and lets boot continue, by design, so nothing surfaces.

`CREATE TABLE IF NOT EXISTS` guarantees this eventually happens: it is a no-op
against a table that already exists, so a table which predates a column keeps
its old shape, and the later `CREATE INDEX ... (that_column)` fails. That is
exactly what `schema_pg.sql:1062`
(`idx_congress_bioguide_id ON congress_trades(bioguide_id)`) did.

Measured consequence: the isolated test database at `:5433/trading_bot_test`
carried **161 tables against production's 214**, so `TRADING_BOT_TEST_DB=1` —
the switch that turns on every real-persistence test — could not be turned on.
The whole persistence-testing surface was unreachable because of one index.

These tests are all local and touch no database.
"""

from __future__ import annotations

import os
import re

import pytest

from app.db.connection import split_sql_statements

_SCHEMA = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "app", "db", "schema_pg.sql",
)


@pytest.fixture(scope="module")
def schema_sql() -> str:
    assert os.path.exists(_SCHEMA), f"schema_pg.sql missing at {_SCHEMA}"
    return open(_SCHEMA, encoding="utf-8").read()


def test_the_splitter_has_something_to_split(schema_sql):
    """Vacuity guard — an empty split passes every assertion below."""
    assert len(split_sql_statements(schema_sql)) > 250


def test_no_dollar_quoted_bodies(schema_sql):
    """The splitter is exact only while the file has no `$$` blocks.

    A function body or `DO $$ ... $$` contains semicolons that mean nothing to
    the statement boundary, and this splitter would cut through the middle of
    one. If this test ever fails, the answer is a real lexer (or `sqlparse`),
    not a wider regex — do not just delete the assertion.
    """
    assert "$$" not in schema_sql, (
        "schema_pg.sql gained a dollar-quoted body; split_sql_statements() in "
        "app/db/connection.py can no longer be trusted to find statement "
        "boundaries"
    )


def test_semicolons_inside_string_literals_do_not_split():
    sql = """
    CREATE TABLE t (a TEXT DEFAULT 'x;y');
    CREATE INDEX i ON t(a);
    """
    stmts = split_sql_statements(sql)
    assert len(stmts) == 2, stmts
    assert "'x;y'" in stmts[0]


def test_semicolons_inside_comments_do_not_split():
    sql = """
    -- a comment; with a semicolon
    CREATE TABLE t (a TEXT);
    /* block; comment */
    CREATE INDEX i ON t(a);
    """
    stmts = split_sql_statements(sql)
    assert len(stmts) == 2, stmts


def test_comment_only_chunks_are_dropped():
    assert split_sql_statements("-- just a header\n\n") == []


def test_every_indexed_column_is_declared_by_the_file_itself(schema_sql):
    """The drift class that caused the truncation, caught statically.

    An index over a column the file never declares can only succeed on a
    database that got the column from somewhere else — a migration. On a fresh
    database it fails, and before the statement-at-a-time fix it took the rest
    of the file with it.
    """
    stmts = split_sql_statements(schema_sql)

    # table -> declared column names, from this file's CREATE TABLE bodies.
    declared: dict[str, set[str]] = {}
    for stmt in stmts:
        m = re.match(
            r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?([A-Za-z_][\w]*)\s*\((.*)\)",
            stmt, re.S | re.I,
        )
        if not m:
            continue
        table = m.group(1).lower()
        # Strip trailing line-comments before splitting on commas, or "--" is
        # parsed as a column name.
        body = re.sub(r"--[^\n]*", "", m.group(2))
        cols = set()
        depth = 0
        current = []
        for ch in body:
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
            if ch == "," and depth == 0:
                current, chunk = [], "".join(current)
                first = chunk.strip().split()
                if first:
                    cols.add(first[0].strip('"').lower())
                continue
            current.append(ch)
        chunk = "".join(current).strip()
        if chunk:
            first = chunk.split()
            if first:
                cols.add(first[0].strip('"').lower())
        declared.setdefault(table, set()).update(cols)

    # Self-calibrating vacuity guard: every CREATE TABLE the splitter finds must
    # also have been parsed for columns. A hardcoded floor would drift with the
    # file; this cannot.
    create_tables = [
        s_ for s_ in stmts if re.match(r"CREATE\s+TABLE", s_, re.I)
    ]
    assert create_tables, "no CREATE TABLE statements parsed — check below is vacuous"
    assert len(declared) == len(create_tables), (
        f"parsed columns for {len(declared)} of {len(create_tables)} CREATE TABLE "
        "statements — the unparsed ones are invisible to the check below"
    )

    offenders = []
    for stmt in stmts:
        m = re.match(
            r"CREATE\s+(?:UNIQUE\s+)?INDEX\s+(?:CONCURRENTLY\s+)?"
            r"(?:IF\s+NOT\s+EXISTS\s+)?[\w\"]+\s+ON\s+([A-Za-z_][\w]*)\s*\(([^)]*)\)",
            stmt, re.S | re.I,
        )
        if not m:
            continue
        table = m.group(1).lower()
        if table not in declared:
            continue  # index on a table this file does not create — out of scope
        for raw in m.group(2).split(","):
            col = raw.strip().split()[0].strip('"').lower() if raw.strip() else ""
            # Skip expression indexes and function calls — only bare columns.
            if not col or not col.isidentifier():
                continue
            if col not in declared[table]:
                offenders.append((stmt.splitlines()[0][:100], table, col))

    assert not offenders, (
        "These indexes reference a column schema_pg.sql never declares, so they "
        "fail on any database that does not already have it from a migration:\n"
        + "\n".join(f"  {head}  ({table}.{col})" for head, table, col in offenders)
    )
