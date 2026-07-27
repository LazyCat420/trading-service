"""Tests for the CORAL repair loop.

The parts worth testing here are the ones that decide whether a bad patch gets
through: the score ladder, the diff extractor, the scope gate, and the two
checks that catch what a syntax check cannot (an empty diff, a deleted
function). Everything that needs a live vLLM box or a full suite run is
exercised by the runner, not here.
"""
from __future__ import annotations

import subprocess
import textwrap
from pathlib import Path

import pytest

from app.cognition.evolution.coral.grader import (
    _parse_failures, _public_symbols, check_api_preserved, check_compiles,
)
from app.cognition.evolution.coral.patcher import (
    PatchError, apply_diff, assert_diff_in_scope, diff_files, extract_diff,
    is_noop,
)
from app.cognition.evolution.coral.repro import _forbidden_calls
from app.cognition.evolution.coral.types import ScoreBundle


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
    """No negative control means no evidence, so nothing is pushable.

    A loop graded only on "the suite still passes" rewards an empty diff.
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
    """The repo has a known failing test; requiring all-green rejects everything."""
    b = ScoreBundle(applied=True, compiles=True, repro_passed=True, suite_ran=True,
                    tests_failed=1, baseline_failed=1, new_failures=[])
    assert b.is_green


# ── Diff extraction ─────────────────────────────────────────────────────────


_GOOD_DIFF = """--- a/app/collectors/yfinance_collector.py
+++ b/app/collectors/yfinance_collector.py
@@ -1,3 +1,3 @@
 def f():
-    return 1
+    return 2
"""


def test_extracts_a_fenced_diff():
    text = f"Here is the fix:\n\n```diff\n{_GOOD_DIFF}```\n\nThat should do it."
    assert "@@" in extract_diff(text)


def test_extracts_an_unfenced_diff():
    assert "@@" in extract_diff("I'll change it:\n\n" + _GOOD_DIFF)


def test_no_diff_raises():
    with pytest.raises(PatchError):
        extract_diff("I think the problem is in the retry logic. You should fix it.")


def test_prose_about_a_patch_is_not_a_patch():
    with pytest.raises(PatchError):
        extract_diff("```python\ndef f():\n    return 2\n```")


def test_strips_line_number_prefixes_copied_from_the_evidence():
    """`render_evidence` numbers its excerpt; models paste the numbers back."""
    numbered = (
        "--- a/app/collectors/x.py\n"
        "+++ b/app/collectors/x.py\n"
        "@@ -1,3 +1,3 @@\n"
        " 41| def f():\n"
        "- 42|     return 1\n"
        "+ 42|     return 2\n"
    )
    out = extract_diff(f"```diff\n{numbered}```")
    assert "41|" not in out
    assert " def f():" in out
    assert "+    return 2" in out


def test_diff_files_reads_the_plus_header():
    assert diff_files(_GOOD_DIFF) == ["app/collectors/yfinance_collector.py"]


# ── Scope gate ──────────────────────────────────────────────────────────────


def test_collector_is_in_scope():
    assert assert_diff_in_scope(_GOOD_DIFF) == [
        "app/collectors/yfinance_collector.py"
    ]


@pytest.mark.parametrize("path", [
    "tests/unit/test_something.py",      # rewriting the evidence
    "app/db/migrations.py",              # unrecoverable ALTER
    "app/cognition/evolution/loop.py",   # the repair machinery itself
    "docker-compose.yml",                # deploy surface
])
def test_protected_paths_are_refused(path):
    diff = _GOOD_DIFF.replace("app/collectors/yfinance_collector.py", path)
    with pytest.raises(PatchError, match="out of repair scope"):
        assert_diff_in_scope(diff)


# ── Apply, against a real git repo ──────────────────────────────────────────


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


def test_apply_diff_changes_the_file(repo: Path):
    diff = (
        "--- a/app/collectors/demo.py\n"
        "+++ b/app/collectors/demo.py\n"
        "@@ -3,4 +3,4 @@\n"
        "\n"
        "\n"
        " def fetch():\n"
        "-    return 1\n"
        "+    return 2\n"
    )
    applied = apply_diff(repo, diff)
    assert applied.files == ["app/collectors/demo.py"]
    assert "return 2" in (repo / "app/collectors/demo.py").read_text()
    assert not is_noop(repo)


def test_a_diff_with_wrong_hunk_counts_still_applies(repo: Path):
    """Models get @@ arithmetic wrong constantly; --recount is the whole point."""
    diff = (
        "--- a/app/collectors/demo.py\n"
        "+++ b/app/collectors/demo.py\n"
        "@@ -99,99 +99,99 @@\n"
        " def fetch():\n"
        "-    return 1\n"
        "+    return 2\n"
    )
    applied = apply_diff(repo, diff)
    assert "return 2" in (repo / "app/collectors/demo.py").read_text()
    assert applied.strategy != "git-apply"


def test_a_diff_whose_context_is_invented_is_refused(repo: Path):
    diff = (
        "--- a/app/collectors/demo.py\n"
        "+++ b/app/collectors/demo.py\n"
        "@@ -1,3 +1,3 @@\n"
        " def totally_different_function():\n"
        "-    was_never_here = 1\n"
        "+    was_never_here = 2\n"
    )
    with pytest.raises(PatchError, match="no apply strategy succeeded"):
        apply_diff(repo, diff)


def test_unchanged_tree_is_a_noop(repo: Path):
    assert is_noop(repo)


# ── Static checks the grader runs before it spends a suite run ──────────────


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


# ── Reproduction-test hygiene ───────────────────────────────────────────────


@pytest.mark.parametrize("snippet,expected", [
    ("with get_db() as db:\n    pass", "database"),
    ("r = httpx.get('http://10.0.0.16')", "network"),
    ("import time\ntime.sleep(5)", "sleep"),
])
def test_repro_tests_reaching_live_dependencies_are_rejected(snippet, expected):
    assert expected in _forbidden_calls(snippet)


def test_a_hermetic_repro_test_is_accepted():
    code = "def test_x():\n    from app.util import f\n    assert f(None) == 0\n"
    assert _forbidden_calls(code) == []
