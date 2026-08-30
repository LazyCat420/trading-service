"""The SQL inventory has to be able to look somewhere other than app/.

`scripts/verify_translations.py` is the only oracle that can compare the two
stores statement by statement, and it judges whatever `sql_inventory.py`
collected. That scanner hardcoded `APP = REPO / "app"`; app/ finished its
conversion, so the default-argument run now returns an empty subject and the
oracle prints a percentage over nothing. The remaining Postgres readers live
under scripts/ and tests/, so the root has to be selectable.

Proven red on the pre-flag tree: `scan(("scripts",))` was a TypeError there,
and once the parameter existed but was ignored the second assertion below
failed with every site naming an app/ file.
"""
import subprocess
import sys
from pathlib import Path

from scripts.sql_inventory import DEFAULT_ROOTS, scan

REPO = Path(__file__).resolve().parents[2]


def test_the_default_root_is_still_app():
    assert DEFAULT_ROOTS == ("app",)
    assert all(s.file.startswith("app/") for s in scan())


def test_a_named_root_scans_that_tree_and_only_that_tree():
    sites = scan(("scripts",))
    assert sites, "scripts/ carries SQL; scanning it must not return nothing"
    assert all(s.file.startswith("scripts/") for s in sites)


def test_several_roots_are_unioned_without_duplication():
    both = scan(("scripts", "tests"))
    files = {s.file for s in both}
    assert any(f.startswith("scripts/") for f in files)
    assert any(f.startswith("tests/") for f in files)
    # scan(("scripts", "scripts")) must not double-count.
    assert len(scan(("scripts", "scripts"))) == len(scan(("scripts",)))


def test_a_missing_root_warns_rather_than_crashing():
    assert scan(("no/such/tree",)) == []


def test_the_cli_accepts_repeated_root_flags():
    out = subprocess.run(
        [sys.executable, "scripts/sql_inventory.py", "--root", "scripts",
         "--root", "tests"],
        cwd=REPO, capture_output=True, text=True, timeout=180)
    assert out.returncode == 0, out.stderr[-2000:]
    assert "SQL call sites in scripts, tests" in out.stdout
