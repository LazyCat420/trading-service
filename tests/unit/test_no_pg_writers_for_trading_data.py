"""No script may write trading data back to the frozen Postgres archive.

MEASURED 2026-08-30. The Mongo cutover was 08-19, but 17 scripts kept issuing
INSERT/UPDATE/DELETE against Postgres. Only one of them had actually run since
— `jetson_benchmark.py` — and that was enough: `box_benchmark_runs` rows
114..136 (08-20..08-27) existed ONLY in Postgres and were invisible to every
Mongo reader. The rest were loaded weapons: `trigger_canary` and `canary_loop`
enqueued onto a `v3_system_commands` queue nothing drains, and `clear_db`,
`fix_db` and `reset_pipeline_for_user` each printed a success line while the
live control plane stayed exactly as stuck as before.

The failure is silent by construction — Postgres accepts the write, the script
exits 0, and the data simply is not where the cycle looks. So the guard is a
static one: no `app/` or `scripts/` module outside the explicitly-allowed
migration tooling may contain a Postgres write statement.

AST-based where it can be (an import is an import), text-based for the SQL
itself, with the allowlist kept small and each entry justified.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]

#: Modules that legitimately still speak Postgres. Every one of them exists to
#: operate ON the archive rather than to store live trading data in it.
ALLOWED = {
    # The archive connection itself, and the DDL/migration tooling that owns it.
    "scripts/migration/__init__.py",
    "scripts/migration/pg_connection.py",
    "scripts/migration/pg_db_migrations.py",
    "scripts/migration/pg_init_db.py",
    "scripts/migration/pg_migrations.py",
    "scripts/migrations/add_column.py",
    # Stands up the Postgres TEST database; never touches production data.
    "scripts/init_test_db.py",
    # The cutover proof: it deliberately writes BOTH stores to show they agree.
    "scripts/prove_mongo.py",
    # Operates on the archive by definition — it is how bad archive rows go.
    "scripts/purge_bad_data.py",
    # This guard, and the image-level driver guard, quote SQL in their prose.
    "tests/unit/test_no_pg_writers_for_trading_data.py",
    "tests/unit/test_app_image_has_no_pg_driver.py",
}

#: A Postgres write. `DELETE FROM` is included without its WHERE clause on
#: purpose: an unqualified delete is the most destructive of the set.
WRITE_SQL = re.compile(
    r"""\b(
        INSERT\s+INTO
      | UPDATE\s+\w+\s+SET
      | DELETE\s+FROM
      | TRUNCATE\s+\w
      | COPY\s+\w+\s+(?:FROM|TO)\b
    )""",
    re.I | re.X,
)

PG_IMPORT = re.compile(r"\b(psycopg2?|asyncpg)\b")


def _python_files():
    for base in ("app", "scripts"):
        for p in sorted((ROOT / base).rglob("*.py")):
            rel = p.relative_to(ROOT).as_posix()
            if rel in ALLOWED or "__pycache__" in rel:
                continue
            yield rel, p


def _imports_pg(src: str) -> bool:
    """True only for a real import — a comment or docstring naming psycopg is
    not one, and `app/` is full of prose explaining what psycopg used to do."""
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return False
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            if any(PG_IMPORT.match(a.name.split(".")[0]) for a in node.names):
                return True
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            if PG_IMPORT.match(mod.split(".")[0]):
                return True
            if mod.endswith("pg_connection") or mod == "scripts.migration":
                return True
    return False


def _strip_prose(src: str) -> str:
    """Drop comments and docstrings before looking for SQL.

    Without this the guard fires on its own explanation. `scrub_poisoned_
    memories` records why a PG-only DELETE was wrong, and `wipe_13f` quotes the
    statement it no longer issues — both are exactly the prose worth keeping.
    """
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return src
    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                             ast.AsyncFunctionDef)):
            d = ast.get_docstring(node, clean=False)
            if d:
                docstrings.add(d)
    out = "\n".join(line.split("#", 1)[0] for line in src.splitlines())
    for d in docstrings:
        out = out.replace(d, "")
    return out


def test_no_module_writes_trading_data_to_postgres():
    offenders = []
    for rel, path in _python_files():
        src = path.read_text(errors="ignore")
        if not _imports_pg(src):
            continue
        code = _strip_prose(src)
        hits = sorted({m.group(1).split()[0].upper() for m in WRITE_SQL.finditer(code)})
        if hits:
            offenders.append(f"{rel}: {','.join(hits)}")
    assert not offenders, (
        "these modules write to Postgres; trading data belongs in Mongo, and a "
        "PG write is silent — it succeeds, exits 0, and the row is simply not "
        "where the cycle reads:\n  " + "\n  ".join(offenders)
    )


def test_the_scanner_can_see_a_write(tmp_path):
    """Negative control: a pass above must mean "no writer found", not "the
    scanner is broken". Mirrors the control in test_app_image_has_no_pg_driver.
    """
    bad = tmp_path / "bad.py"
    bad.write_text(
        "import psycopg\n"
        "def f(db):\n"
        "    db.execute(\"INSERT INTO box_benchmark_runs (box) VALUES ('x')\")\n"
    )
    src = bad.read_text()
    assert _imports_pg(src), "the import scanner missed a real psycopg import"
    assert WRITE_SQL.search(_strip_prose(src)), "the SQL scanner missed a real INSERT"


def test_prose_about_postgres_is_not_a_writer(tmp_path):
    """The other direction: a module that only DESCRIBES an old PG write must
    pass, or the guard becomes something people silence."""
    ok = tmp_path / "ok.py"
    ok.write_text(
        '"""We used to run INSERT INTO shared_desk here; it now goes to Mongo."""\n'
        "import psycopg  # kept for the archive reader below\n"
        "# DELETE FROM evolution_lessons WHERE id = %s  -- what this replaced\n"
        "x = 1\n"
    )
    src = ok.read_text()
    assert _imports_pg(src)
    assert not WRITE_SQL.search(_strip_prose(src)), (
        "prose and comments must not count as a write"
    )


@pytest.mark.parametrize("rel", sorted(ALLOWED))
def test_the_allowlist_has_no_stale_entries(rel):
    """An allowlist that outlives its files quietly stops guarding anything."""
    assert (ROOT / rel).exists(), (
        f"{rel} is allow-listed but does not exist — remove it from ALLOWED"
    )
