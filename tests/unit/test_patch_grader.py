"""Tests for the patch grader.

What is worth testing here is what decides whether a bad patch gets through:
the score ladder, the scope gate, and the two static checks that catch what a
syntax check cannot (a broken file, a deleted function).

Trimmed from test_coral_repair.py on 2026-07-31 when the autonomous repair loop
was removed. The tests that went with it covered the diff extractor, the patch
applier and the queue-claim logic — all deleted along with the proposing. The
grading is what survived, so its tests are what survive here.
"""
from __future__ import annotations

import subprocess
import textwrap
from pathlib import Path

import pytest

from app.cognition.evolution.coral.grader import (
    _parse_failures, _public_symbols, check_api_preserved, check_compiles,
)
from app.cognition.evolution.coral.types import ScoreBundle
from app.cognition.evolution.repair_scope import is_patchable


# ── ScoreBundle: the ranking key ────────────────────────────────────────────


def test_unapplied_patch_scores_zero():
    assert ScoreBundle(applied=False).score == 0.0


def test_uncompilable_patch_scores_zero():
    b = ScoreBundle(applied=True, compiles=False)
    assert b.score == 0.0


def test_patch_that_does_not_fix_the_repro_is_capped():
    b = ScoreBundle(applied=True, compiles=True, repro_passed=False)
    assert b.score == 0.25
    assert not b.is_green


def test_missing_repro_test_can_never_reach_green():
    """No negative control means no evidence, so nothing is ready.

    Grading only on "the suite still passes" rewards an empty diff.
    """
    b = ScoreBundle(applied=True, compiles=True, repro_passed=False,
                    suite_ran=True, tests_passed=732)
    assert b.score < 1.0


def test_regression_beats_a_passing_repro():
    b = ScoreBundle(applied=True, compiles=True, repro_passed=True,
                    suite_ran=True, new_failures=["tests/unit/test_x.py::test_y"])
    assert b.regressed
    assert b.score == 0.60


def test_deleting_a_public_symbol_counts_as_a_regression():
    """The exact failure the old judge scored 90/100 for issue resolution."""
    b = ScoreBundle(applied=True, compiles=True, repro_passed=True, suite_ran=True,
                    api_removed=["app/collectors/yfinance_collector.py::collect_all"])
    assert b.regressed
    assert b.score == 0.60
    assert "DELETED" in b.summary()


def test_green_requires_repro_pass_and_no_regression():
    b = ScoreBundle(applied=True, compiles=True, repro_passed=True,
                    suite_ran=True, tests_passed=732, tests_failed=1,
                    baseline_failed=1)
    assert b.is_green
    assert b.score == 1.0


def test_preexisting_failure_does_not_block_green():
    """A repo carrying a known failure must not reject every patch forever."""
    b = ScoreBundle(applied=True, compiles=True, repro_passed=True, suite_ran=True,
                    tests_failed=1, baseline_failed=1, new_failures=[])
    assert b.is_green


def test_dead_suite_cannot_reach_green():
    """A passing repro with no suite verdict is not evidence of no regression.

    This is the collapsed-environment case found by fault injection on
    2026-08-09: a linked worktree without the venv errored 63 files at
    collection, the node-id diff came back empty, and the bundle scored 1.0.
    """
    b = ScoreBundle(applied=True, compiles=True, repro_passed=True,
                    suite_ran=False, tests_passed=0, new_failures=[])
    assert b.score == 0.60
    assert not b.is_green


# ── Scope gate ──────────────────────────────────────────────────────────────


def test_collector_is_in_scope():
    allowed, _ = is_patchable("app/collectors/yfinance_collector.py")
    assert allowed


@pytest.mark.parametrize("path", [
    "tests/unit/test_something.py",           # rewriting the evidence
    "app/db/migrations.py",                   # unrecoverable ALTER
    "app/cognition/evolution/coral/grader.py",  # the grading machinery itself
    "docker-compose.yml",                     # deploy surface
])
def test_protected_paths_are_refused(path):
    allowed, reason = is_patchable(path)
    assert not allowed, f"{path} should be out of scope"
    assert reason


# ── Static checks the grader runs before it spends a suite run ──────────────


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A throwaway git repo with one collector in it."""
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=tmp_path, check=True)
    target = tmp_path / "app" / "collectors"
    target.mkdir(parents=True)
    (target / "demo.py").write_text(
        textwrap.dedent("""\
            def collect_all():
                return fetch()


            def fetch():
                return 1
            """)
    )
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=tmp_path, check=True)
    return tmp_path


def test_public_symbols_ignores_private_names():
    src = "def public():\n    pass\n\n\ndef _private():\n    pass\n\n\nclass C:\n    pass\n"
    assert _public_symbols(src) == {"public", "C"}


def test_check_compiles_catches_a_broken_patch(repo: Path):
    (repo / "app/collectors/demo.py").write_text("def collect_all(:\n")
    ok, detail = check_compiles(repo, ["app/collectors/demo.py"])
    assert not ok
    assert "app/collectors/demo.py" in detail


def test_check_api_preserved_flags_a_deleted_function(repo: Path):
    (repo / "app/collectors/demo.py").write_text("def fetch():\n    return 2\n")
    removed = check_api_preserved(repo, ["app/collectors/demo.py"])
    assert removed == ["app/collectors/demo.py::collect_all"]


def test_check_api_preserved_allows_an_added_function(repo: Path):
    path = repo / "app/collectors/demo.py"
    path.write_text(path.read_text() + "\n\ndef extra():\n    return 3\n")
    assert check_api_preserved(repo, ["app/collectors/demo.py"]) == []


def test_check_api_preserved_sees_deletion_in_a_committed_ref(repo: Path):
    """Grading a committed ref: HEAD *is* the patch, so the base must be
    explicit or a deletion compares the patch against itself and vanishes."""
    base = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, check=True,
                          capture_output=True, text=True).stdout.strip()
    (repo / "app/collectors/demo.py").write_text("def fetch():\n    return 2\n")
    subprocess.run(["git", "commit", "-qam", "delete collect_all"], cwd=repo,
                   check=True)
    # Against worktree HEAD (the patch itself): the deletion is invisible.
    assert check_api_preserved(repo, ["app/collectors/demo.py"]) == []
    # Against the explicit base: caught.
    removed = check_api_preserved(repo, ["app/collectors/demo.py"], base_ref=base)
    assert removed == ["app/collectors/demo.py::collect_all"]


# ── pytest output parsing ───────────────────────────────────────────────────


def test_parse_failures_reads_the_short_summary():
    output = textwrap.dedent("""\
        .........F......                                        [100%]
        =========================== short test summary info ====
        FAILED tests/unit/test_a.py::test_one - AssertionError
        ERROR tests/unit/test_b.py::test_two
        1 failed, 732 passed, 5 skipped, 12 warnings in 50.57s
        """)
    failures, passed, failed = _parse_failures(output)
    assert failures == {"tests/unit/test_a.py::test_one", "tests/unit/test_b.py::test_two"}
    assert passed == 732
    assert failed == 1


def test_parse_failures_on_a_clean_run():
    failures, passed, failed = _parse_failures("733 passed in 48.2s\n")
    assert failures == set()
    assert (passed, failed) == (733, 0)


def test_parse_failures_reads_collection_errors():
    """A suite that dies at import must yield node ids, not just counts —
    counts with an empty failure set diff to 'no regressions'."""
    output = textwrap.dedent("""\
        =========================== short test summary info ====
        ERROR tests/unit/test_a.py - ModuleNotFoundError: No module named 'lazycat'
        ERROR tests/unit/test_b.py - ModuleNotFoundError: No module named 'lazycat'
        !!!!!!!!! Interrupted: 2 errors during collection !!!!!!!!!
        15 warnings, 2 errors in 7.57s
        """)
    failures, passed, failed = _parse_failures(output)
    assert failures == {"tests/unit/test_a.py", "tests/unit/test_b.py"}
    assert passed == 0
    assert failed == 2
