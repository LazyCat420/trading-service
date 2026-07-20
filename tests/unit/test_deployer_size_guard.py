"""
Guard against the repair loop truncating the file it was asked to fix.

The loop asks a model for a COMPLETE rewrite while showing it a capped view of
the file (target_map cut at 8000 chars). For any larger target the "complete"
rewrite covers only the prefix the model saw — and the deployer wrote that
straight over the real source. 17 of 30 mapped repair targets were over the cap.
"""
import pytest

from app.cognition.evolution.deployer import (
    _MIN_REWRITE_SIZE_RATIO,
    _check_size_regression,
)


@pytest.fixture
def big_file(tmp_path):
    p = tmp_path / "pipeline_service.py"
    p.write_text("\n".join(f"line_{i} = {i}" for i in range(2000)))
    return p


def test_truncated_rewrite_is_refused(big_file):
    """The core case: the model saw 8000 chars of a 76k file and 'rewrote' it."""
    original = big_file.read_text()
    truncated_rewrite = original[:8000]

    reason = _check_size_regression(big_file, truncated_rewrite)
    assert reason is not None
    assert "truncated view" in reason


def test_faithful_rewrite_is_allowed(big_file):
    original = big_file.read_text()
    # A real fix: same file, one line changed.
    fixed = original.replace("line_5 = 5", "line_5 = 500")

    assert _check_size_regression(big_file, fixed) is None


def test_moderate_shrink_is_allowed(big_file):
    """Deleting some dead code is legitimate; collapsing the file is not."""
    original = big_file.read_text()
    shrunk = original[: int(len(original) * 0.8)]

    assert _check_size_regression(big_file, shrunk) is None


def test_shrink_just_below_the_floor_is_refused(big_file):
    original = big_file.read_text()
    shrunk = original[: int(len(original) * (_MIN_REWRITE_SIZE_RATIO - 0.05))]

    reason = _check_size_regression(big_file, shrunk)
    assert reason is not None
    assert "floor" in reason


@pytest.mark.parametrize("marker", [
    "\n# ... [TRUNCATED — full file is 76065 chars] ...",
    "\n# ... rest of the file unchanged ...",
    "\n# (rest of file omitted)",
])
def test_truncation_markers_are_refused_regardless_of_size(big_file, marker):
    """A marker copied into the output proves the model saw a shortened view."""
    original = big_file.read_text()
    with_marker = original + marker

    reason = _check_size_regression(big_file, with_marker)
    assert reason is not None
    assert "truncation marker" in reason


def test_empty_original_is_not_blocked(tmp_path):
    """A new/empty file has nothing to lose."""
    p = tmp_path / "new.py"
    p.write_text("")
    assert _check_size_regression(p, "def something():\n    return 1\n") is None


def test_unreadable_file_refuses_rather_than_assuming_safe(tmp_path):
    missing = tmp_path / "gone.py"
    reason = _check_size_regression(missing, "whatever")
    assert reason is not None
    assert "cannot read" in reason
