"""
Open-items wave, 2026-08-11 — items 15 and 31 from trading-client's Open items.

Item 15 was filed as "two scripts use a `get_db()` pattern that cannot work".
It was three, and the one it missed was `wipe_13f.py`, which deletes every 13F
holding. That script had two further defects hiding behind the broken call, so
these tests pin the guards rather than the repair.
"""

import runpy
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from app.services.pipeline_service import (
    DEFAULT_GATEKEEPER_MAX_TICKERS,
    _resolve_gatekeeper_max_tickers,
)

REPO = Path(__file__).resolve().parents[2]


# ── Item 31: max_tickers=0 was silently 15 ─────────────────────────────

def test_none_uses_the_documented_default():
    assert _resolve_gatekeeper_max_tickers(None) == DEFAULT_GATEKEEPER_MAX_TICKERS


def test_a_real_number_is_honoured():
    assert _resolve_gatekeeper_max_tickers(5) == 5
    assert _resolve_gatekeeper_max_tickers(40) == 40


@pytest.mark.parametrize("bad", [0, -1, -20])
def test_zero_and_negatives_resolve_to_the_default_and_say_so(bad, caplog):
    """0 was offered as "unlimited" by the UI and silently became 15.

    It still becomes 15 — unlimited would hand the gatekeeper the whole
    candidate pool — but it is no longer silent.
    """
    import logging
    with caplog.at_level(logging.WARNING, logger="app.services.pipeline_service"):
        assert _resolve_gatekeeper_max_tickers(bad) == DEFAULT_GATEKEEPER_MAX_TICKERS
    assert any("not 'unlimited'" in r.getMessage() for r in caplog.records), \
        "resolving 0 must not be silent"


def test_a_non_numeric_value_does_not_raise():
    assert _resolve_gatekeeper_max_tickers("abc") == DEFAULT_GATEKEEPER_MAX_TICKERS


def test_min_tickers_still_derives_sanely():
    """`min_tickers = min(5, max_tickers)` sat right below the old bug; a
    resolver returning 0 would have produced a "pick between 0 and 0" prompt."""
    for requested in (None, 0, 1, 3, 15, 50):
        resolved = _resolve_gatekeeper_max_tickers(requested)
        assert resolved >= 1
        assert 1 <= min(5, resolved) <= resolved


# ── Item 15: the three scripts, and the destructive one ────────────────

def test_the_two_superseded_migration_scripts_are_gone():
    """Both ALTER TABLEs live in the boot migrations (migrations.py:2564,2734)
    and every column already exists, so repairing them would have created a
    second writer for schema the boot path owns."""
    assert not (REPO / "scripts/db/migrate_sectors_returns.py").exists()
    assert not (REPO / "scripts/db/migrate_avg_ticker.py").exists()


def test_no_script_uses_the_get_db_pattern_that_cannot_work():
    """`db = get_db()` against a @contextmanager raises on the first execute."""
    hits = subprocess.run(
        ["grep", "-rn", "db = get_db()", "scripts/", "app/"],
        cwd=REPO, capture_output=True, text=True,
    ).stdout.strip()
    # Every surviving match must be prose ABOUT the bug, not the bug: the
    # docstrings in connection.py, dossier_sync.py and wipe_13f.py all quote it.
    real = [ln for ln in hits.splitlines()
            if ln.split(":", 2)[-1].lstrip().startswith("db = get_db()")]
    assert not real, f"still present as code:\n{chr(10).join(real)}"


def test_importing_the_wipe_script_deletes_nothing():
    """The DELETE used to sit at module scope with no __main__ guard, so an
    import was enough to wipe the table. The broken get_db() was the only thing
    standing in front of it."""
    with patch("scripts.migration.pg_connection.get_db") as fake_db:
        runpy.run_path(str(REPO / "scripts/db/wipe_13f.py"), run_name="not_main")
    assert not fake_db.called, "importing the module touched the database"


def test_the_wipe_refuses_without_an_explicit_yes():
    r = subprocess.run(
        [sys.executable, "scripts/db/wipe_13f.py"],
        cwd=REPO, capture_output=True, text=True,
    )
    assert r.returncode != 0
    assert "--yes" in (r.stderr + r.stdout)


def test_dry_run_reports_and_changes_nothing():
    from scripts.db import wipe_13f  # noqa: PLC0415 — import must follow the guard test

    executed = []

    class _Cur:
        def execute(self, sql, *a):
            executed.append(sql)
            return self
        def fetchone(self):
            return (7,)

    class _Ctx:
        def __enter__(self): return _Cur()
        def __exit__(self, *a): return False

    with patch("scripts.db.wipe_13f.get_db", lambda: _Ctx()):
        assert wipe_13f.wipe_13f(dry_run=True) == 7

    assert not any("DELETE" in s.upper() or "UPDATE" in s.upper() for s in executed), \
        f"dry-run must not mutate, ran: {executed}"


def test_a_confirmed_wipe_does_delete():
    """The counterpart — the guard must not have disabled the tool."""
    from scripts.db import wipe_13f

    executed = []

    class _Cur:
        def execute(self, sql, *a):
            executed.append(sql)
            return self
        def fetchone(self):
            return (3,)

    class _Ctx:
        def __enter__(self): return _Cur()
        def __exit__(self, *a): return False

    with patch("scripts.db.wipe_13f.get_db", lambda: _Ctx()):
        wipe_13f.wipe_13f(dry_run=False)

    assert any("DELETE FROM sec_13f_holdings" in s for s in executed)
    assert any("UPDATE sec_13f_filers" in s for s in executed)
