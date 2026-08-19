"""scripts/check_backend_map.py must fail on every way the two files can drift.

The point of these tests is not that the checker passes today -- it is that it
can fail. A guard nobody has watched fail is not a guard; the previous version
of this interlock was a sentence in a comment claiming a script existed, and
the script did not.

Each test sabotages exactly one relationship and asserts the checker catches
that one, then the last test asserts the unsabotaged tree passes -- so a
checker that simply always failed would not satisfy this file either.
"""

from __future__ import annotations

import importlib.util
import json
import pathlib
import subprocess
import sys

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[2]
_SCRIPT = _ROOT / "scripts" / "check_backend_map.py"
_ENV = _ROOT / "app" / "db" / "mongo_backends.env"
_LEDGER = _ROOT / "app" / "db" / "migration_ledger.json"

spec = importlib.util.spec_from_file_location("check_backend_map", _SCRIPT)
cbm = importlib.util.module_from_spec(spec)
sys.modules["check_backend_map"] = cbm
spec.loader.exec_module(cbm)


@pytest.fixture
def tree(tmp_path, monkeypatch):
    """A copy of app/db/ that the checker reads via a patched REPO_ROOT."""
    (tmp_path / "app" / "db").mkdir(parents=True)
    (tmp_path / "scripts").mkdir()
    for src in (_ENV, _LEDGER):
        (tmp_path / "app" / "db" / src.name).write_bytes(src.read_bytes())
    monkeypatch.setattr(cbm, "REPO_ROOT", tmp_path)
    return tmp_path


def _set_mode(tree: pathlib.Path, table: str, mode: str) -> None:
    p = tree / "app" / "db" / "migration_ledger.json"
    d = json.loads(p.read_text())
    for row in d["tables"]:
        if row["table"] == table:
            row["mode_now"] = mode
    p.write_text(json.dumps(d))


def _patch_env(tree: pathlib.Path, old: str, new: str) -> None:
    p = tree / "app" / "db" / "mongo_backends.env"
    p.write_text(p.read_text().replace(old, new))


def test_unsabotaged_tree_passes(tree, capsys):
    assert cbm.main([]) == 0
    assert "OK:" in capsys.readouterr().out


def test_ledger_disagreeing_with_the_map_fails(tree):
    """The migration would report embeddings as un-migrated while it is at mongo."""
    _set_mode(tree, "embeddings", "pg")
    assert cbm.main([]) == 1


def test_map_naming_a_table_the_ledger_lacks_fails(tree):
    _patch_env(tree, "embeddings:mongo", "embeddings:mongo,not_a_table:dual")
    assert cbm.main([]) == 1


def test_ledger_claiming_a_promotion_the_map_lacks_fails(tree):
    """The direction that catches progress the containers never received."""
    _set_mode(tree, "watchlist", "mongo")
    assert cbm.main([]) == 1


def test_an_invalid_mode_fails(tree):
    _patch_env(tree, "embeddings:mongo", "embeddings:sideways")
    assert cbm.main([]) == 1


def test_a_drifted_sibling_copy_fails(tree, tmp_path):
    """Both containers stage their own copy; a drift splits the two stores."""
    sib = tmp_path / "sibling"
    (sib / "app" / "db").mkdir(parents=True)
    (sib / "app" / "db" / "mongo_backends.env").write_text(
        _ENV.read_text().replace("embeddings:mongo", "embeddings:dual")
    )
    assert cbm.main(["--sibling", str(sib)]) == 1


def test_a_matching_sibling_copy_passes(tree, tmp_path):
    sib = tmp_path / "sibling"
    (sib / "app" / "db").mkdir(parents=True)
    (sib / "app" / "db" / "mongo_backends.env").write_bytes(_ENV.read_bytes())
    assert cbm.main(["--sibling", str(sib)]) == 0


def test_a_missing_file_is_exit_2_not_exit_1(tree):
    """A missing file is 'cannot tell', which must not read as 'they agree'."""
    (tree / "app" / "db" / "migration_ledger.json").unlink()
    assert cbm.main([]) == 2


def test_the_real_repo_passes():
    """The committed tree must actually be consistent, not just checkable."""
    r = subprocess.run(
        [sys.executable, str(_SCRIPT), "--sibling", str(_ROOT.parent / "trading-client")],
        capture_output=True,
        text=True,
    )
    assert r.returncode == 0, r.stdout + r.stderr


SHARED_WITH_CLIENT = ("app/db/money_policy.py", "app/db/collection_map.json")


@pytest.mark.parametrize("rel", SHARED_WITH_CLIENT)
def test_the_real_repo_actually_has_the_shared_files(rel):
    """The script SKIPS a missing file, so something must assert presence.

    A skip is right for the script — it runs against synthetic trees and
    partial checkouts, and failing on absence would just teach people to point
    `--sibling` at an empty directory. But "skipped" and "verified" then look
    identical in its output, so if nothing else checked, deleting
    `money_policy.py` from this repo would make the check PASS.

    This is the half that cannot be skipped: in the committed tree the file is
    present, full stop.
    """
    assert (_ROOT / rel).exists(), (
        f"{rel} is missing from this repo — check_backend_map.py would silently "
        "SKIP its byte-identity check and report OK"
    )


@pytest.mark.parametrize("rel", SHARED_WITH_CLIENT)
def test_the_shared_files_match_the_client(rel):
    """Byte-identity against the real sibling, asserted in the suite.

    The script only checks this when someone runs it; this fails in the normal
    test run. Skips when no client checkout is present here, because that is an
    environment fact rather than drift.
    """
    ours = _ROOT / rel
    for sibling in (_ROOT.parent / "trading-client",
                    _ROOT.parent / "tc-mongo-conversion"):
        theirs = sibling / rel
        if theirs.exists():
            assert theirs.read_bytes() == ours.read_bytes(), (
                f"{theirs} is not byte-identical to {ours} — the service and "
                "the client would disagree about the same contract at runtime"
            )
            return
    pytest.skip("no trading-client checkout found beside this one")
