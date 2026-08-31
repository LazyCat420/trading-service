"""`DATABASE_URL` must not come back into the ambient environment.

The archive DSN sitting in `.env` is how roughly thirty-six legacy scripts kept
reporting July numbers as current after the 2026-08-19 cutover. They did not ask
for Postgres; they called `load_dotenv()` and `os.getenv("DATABASE_URL")` and
found it. Every one of them read as working.

It was removed from both repos' `.env` on 2026-08-30, once the last Postgres
reader outside `app/` was ported. The archive DSN now lives in `.env.migration`
as `PG_ARCHIVE_URL` and is loaded EXPLICITLY, by name, in
`scripts/quality_census.py::pg_url` — which every retained archive tool goes
through, so the oracle, the backfill, the seeder and the quiescence instrument
all still work. Verified after the removal: `pg_url()` resolves, the
two-store translation oracle still compares both, and `pg_quiescence --snapshot`
still connects.

What SHOULD break is anything that never said which store it wanted. That is
the point, and it is why this guard exists: putting the variable back would
restore the silent path without restoring the bug that made it dangerous, so
nothing else would notice.

Skips when `.env` is absent — a fresh checkout has none, and a guard that fails
on a clean clone teaches people to ignore it.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
_BANNED = ("DATABASE_URL", "TEST_DATABASE_URL")


def _env_files() -> list[Path]:
    here = REPO / ".env"
    sibling = REPO.parent / "trading-client" / ".env"
    return [p for p in (here, sibling) if p.is_file()]


def test_neither_env_file_defines_the_ambient_archive_dsn():
    files = _env_files()
    if not files:
        pytest.skip("no .env on this box — nothing to guard")
    offenders = []
    for f in files:
        for i, line in enumerate(f.read_text().splitlines(), 1):
            key = line.split("=", 1)[0].strip()
            if key in _BANNED:
                offenders.append(f"{f}:{i}  {key}")
    assert not offenders, (
        "the archive DSN is back in the ambient environment:\n  "
        + "\n  ".join(offenders)
        + "\n\nPut it in .env.migration as PG_ARCHIVE_URL instead. Every "
          "retained archive tool reads it through quality_census.pg_url(), "
          "which prefers that variable; only code that never said which store "
          "it wanted needs the ambient one.")


def test_the_archive_is_still_reachable_by_name():
    """The other half. Removing the ambient DSN must not have orphaned the
    tooling that legitimately reads the archive — otherwise the next person
    puts DATABASE_URL back and this guard is what they delete."""
    if not (REPO / ".env.migration").is_file() and not os.getenv("PG_ARCHIVE_URL"):
        pytest.skip("no .env.migration on this box — see .env.migration.example")
    from scripts.quality_census import pg_url

    url = pg_url()
    assert url.startswith("postgres"), url


def test_the_guard_would_notice(tmp_path):
    """Negative control — a planted line must be found."""
    f = tmp_path / ".env"
    f.write_text('FOO=1\nDATABASE_URL="postgresql://h/db"\nBAR=2\n')
    hits = [line for line in f.read_text().splitlines()
            if line.split("=", 1)[0].strip() in _BANNED]
    assert hits == ['DATABASE_URL="postgresql://h/db"']
