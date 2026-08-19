"""The application image must not carry the Postgres driver.

Teardown (2026-08-18) moved `app/db/connection.py` — the repo's only psycopg
importer under `app/` — to `scripts/migration/pg_connection.py`, and split
psycopg out of `requirements.in` into `requirements-migration.in`. These tests
are what stops it drifting back.

Two distinct failures are guarded, because either alone would put the driver
back in the image:

  1. an `import psycopg` reappearing in `app/` or `cycle_main.py`, and
  2. psycopg reappearing in `requirements.in` (or its lockfile), which puts it
     in the image whether or not anything imports it.

The scan is AST-based, not a text grep: a docstring or a comment mentioning
psycopg is not an import, and `app/` is full of comments explaining what
psycopg used to return. A grep here would fire on those and get silenced,
which is how a guard becomes ignored.

Negative control: `test_the_scanner_can_see_a_driver_import` runs the same
scanner over a module that DOES import psycopg and asserts it is caught, so a
pass above means "no import found", not "the scanner is broken".
"""

from __future__ import annotations

import ast
import pathlib

import pytest

REPO = pathlib.Path(__file__).resolve().parents[2]

# Roots that become the Docker image's import graph. `scripts/` is excluded on
# purpose: Dockerfile copies it, but nothing at runtime imports from it —
# entrypoint.sh execs `cycle_main.py` only. See scripts/migration/__init__.py.
APP_ROOTS = ("app", "cycle_main.py")

DRIVER_ROOTS = {"psycopg", "psycopg2", "psycopg_pool", "pgvector"}


def _iter_py(root: str):
    path = REPO / root
    if path.is_file():
        yield path
    else:
        yield from (p for p in path.rglob("*.py") if "__pycache__" not in p.parts)


def _driver_imports(path: pathlib.Path) -> list[tuple[int, str]]:
    """Every psycopg/pgvector import in one module, as (lineno, name).

    Catches `import psycopg`, `import psycopg.rows`, `from psycopg_pool import
    ...` and function-local imports alike — `_ensure_pool()` hid a
    `from pgvector.psycopg import register_vector` inside a function body, so a
    module-level-only scan would have missed it.
    """
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except SyntaxError:  # not our business here; the suite has its own guard
        return []

    hits: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".")[0] in DRIVER_ROOTS:
                    hits.append((node.lineno, alias.name))
        elif isinstance(node, ast.ImportFrom) and node.module:
            if node.module.split(".")[0] in DRIVER_ROOTS:
                hits.append((node.lineno, node.module))
    return hits


def test_no_pg_driver_import_under_app():
    offenders = [
        f"{path.relative_to(REPO)}:{lineno} imports {name}"
        for root in APP_ROOTS
        for path in _iter_py(root)
        for lineno, name in _driver_imports(path)
    ]
    assert not offenders, (
        "The Postgres driver is back in the application image's import graph:\n  "
        + "\n  ".join(offenders)
        + "\n\nPostgres-era code belongs in scripts/migration/, which the image "
          "copies but never imports. If this module genuinely has to read the "
          "frozen Postgres backup, it is migration tooling — move it there."
    )


def test_the_scanner_can_see_a_driver_import(tmp_path):
    """Negative control: the scanner must fail on a module that DOES import it.

    Without this, deleting the body of `_driver_imports` would leave the test
    above passing forever.
    """
    module = tmp_path / "still_on_pg.py"
    module.write_text(
        "import os\n"
        "import psycopg\n"
        "def _pool():\n"
        "    from pgvector.psycopg import register_vector\n"
        "    return register_vector\n",
        encoding="utf-8",
    )
    hits = _driver_imports(module)
    names = {name for _, name in hits}
    assert "psycopg" in names, "scanner missed a module-level `import psycopg`"
    assert "pgvector.psycopg" in names, (
        "scanner missed a function-local `from pgvector.psycopg import ...` — "
        "exactly the shape `_ensure_pool()` used"
    )


def test_psycopg_is_not_in_the_image_requirements():
    text = (REPO / "requirements.in").read_text(encoding="utf-8")
    offending = [
        line for line in text.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
        and any(d in line.lower() for d in ("psycopg", "pgvector"))
    ]
    assert not offending, (
        "psycopg/pgvector is back in requirements.in, so the image installs it "
        f"again: {offending}. Migration tooling pins it in "
        "requirements-migration.in instead."
    )


def test_lockfile_matches():
    """requirements.txt is generated; a stale lockfile still ships the driver."""
    text = (REPO / "requirements.txt").read_text(encoding="utf-8")
    offending = [
        line for line in text.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
        and any(line.lower().startswith(d) for d in ("psycopg", "pgvector"))
    ]
    assert not offending, (
        "requirements.txt still pins the Postgres driver — re-run "
        "`pip-compile --output-file=requirements.txt requirements.in`: "
        f"{offending}"
    )


@pytest.mark.parametrize("relpath", [
    "scripts/migration/pg_connection.py",
    "scripts/migration/pg_migrations.py",
    "scripts/migration/schema_pg.sql",
])
def test_migration_tooling_still_exists(relpath):
    """The DDL is retained deliberately, not lost.

    Parity checks still have to read the source store. Asserting the files are
    present stops a later "finish the teardown" pass from deleting the only
    thing that can describe the Postgres schema — and keeps the DDL findable,
    since a DROP TABLE that does not also delete its CREATE here comes back on
    the next `run_migrations()`.
    """
    assert (REPO / relpath).exists(), f"{relpath} is missing"
