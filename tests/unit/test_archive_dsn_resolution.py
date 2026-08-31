"""The archive DSN is asked for BY NAME, not found lying in the environment.

`DATABASE_URL` in the ambient environment is how ~36 legacy scripts kept
reporting July numbers as current after the 2026-08-19 cutover: they never
asked for the archive, they just found it. The plan of record (ch.106, and both
repos' `.env.migration.example`) is that the archive DSN lives in
`.env.migration` as `PG_ARCHIVE_URL`, loaded explicitly by the tooling that
legitimately wants it — and that `DATABASE_URL` is then deleted from `.env`.

For that deletion to be a one-line change rather than a hunt, `pg_url()` has to
prefer the explicit variable NOW, while the compatibility path still works.
This pins the order and pins the warning, which names the caller: "something
read the archive" is not actionable and "purge_bad_data read the archive" is.

Proven red on the pre-change tree: `PG_ARCHIVE_URL` was not consulted at all,
so the first test read DATABASE_URL and returned the wrong DSN.
"""
import importlib
import sys

import pytest


@pytest.fixture
def census(tmp_path, monkeypatch):
    """quality_census with REPO pointed at a scratch tree and env cleared."""
    import scripts.quality_census as qc
    importlib.reload(qc)
    monkeypatch.setattr(qc, "REPO", tmp_path)
    monkeypatch.delenv("PG_ARCHIVE_URL", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    qc._ARCHIVE_FALLBACK_WARNED = False
    return qc


def test_the_environment_variable_wins(census, monkeypatch, capsys):
    monkeypatch.setenv("PG_ARCHIVE_URL", "postgresql://a/1")
    (census.REPO / ".env").write_text('DATABASE_URL="postgresql://b/2"\n')
    assert census.pg_url() == "postgresql://a/1"
    assert capsys.readouterr().err == "", "no warning is due on the intended path"


def test_env_migration_beats_the_ambient_database_url(census, capsys):
    (census.REPO / ".env.migration").write_text('PG_ARCHIVE_URL="postgresql://a/1"\n')
    (census.REPO / ".env").write_text('DATABASE_URL="postgresql://b/2"\n')
    assert census.pg_url() == "postgresql://a/1"
    assert capsys.readouterr().err == ""


def test_database_url_still_works_but_says_so(census, capsys):
    (census.REPO / ".env").write_text('DATABASE_URL="postgresql://b/2"\n')
    assert census.pg_url() == "postgresql://b/2"
    err = capsys.readouterr().err
    assert "PG_ARCHIVE_URL" in err and ".env.migration" in err
    assert "test_archive_dsn_resolution.py" in err, (
        f"the warning must name the caller, got: {err!r}")


def test_the_warning_is_emitted_once_not_per_call(census, capsys):
    (census.REPO / ".env").write_text('DATABASE_URL="postgresql://b/2"\n')
    census.pg_url()
    capsys.readouterr()
    census.pg_url()
    assert capsys.readouterr().err == ""


def test_the_asyncpg_scheme_is_normalised(census, monkeypatch):
    monkeypatch.setenv("PG_ARCHIVE_URL", "postgresql+asyncpg://a/1")
    assert census.pg_url() == "postgresql://a/1"


def test_no_dsn_anywhere_is_a_clean_exit_naming_the_fix(census):
    with pytest.raises(SystemExit) as exc:
        census.pg_url()
    assert "PG_ARCHIVE_URL" in str(exc.value)
    assert ".env.migration" in str(exc.value)


def test_pg_quiescence_uses_the_shared_helper_not_a_copy():
    """The retirement instrument must not carry its own DSN resolution.

    It did — a byte-equivalent private copy — so the seam close had two places
    to find, and only one of them would have learned to prefer PG_ARCHIVE_URL.
    The one left behind would have been the instrument that certifies the
    retirement.
    """
    import ast
    from pathlib import Path

    src = (Path(__file__).resolve().parents[2] / "scripts" / "pg_quiescence.py").read_text()
    tree = ast.parse(src)
    defined = [n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)]
    assert "pg_url" not in defined, (
        "pg_quiescence.py defines its own pg_url again — import it from "
        "scripts.quality_census instead")
    assert "from scripts.quality_census import pg_url" in src
