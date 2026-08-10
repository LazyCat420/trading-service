"""Path resolution that survives a git worktree.

WHY THIS EXISTS
---------------
`tool_schemas.json` is **gitignored** — it is a build artifact of
`scripts/build_tool_schemas.py`, not a tracked file. A `git worktree` therefore
never contains it, so every test that reads it *skipped*, quietly, and the
worktree suite was permanently weaker than the primary one. On 2026-08-10 that
was 47 tests in `test_tool_repair.py` and `test_multi_repo_audit.py` — including
the checks that guard the repair allow-list and the cross-repo catalog sync,
i.e. the ones that would have caught the two tools whose published schema had
drifted from their executor.

A skip with a plausible reason reads exactly like a pass. This resolves to the
primary checkout instead, so a worktree runs the same suite the primary does.

The resolution is DEV-ONLY on purpose. `app/tools/registry.py` still looks only
beside itself: production must never reach into a sibling checkout, and a
container with no catalog should fail loudly rather than borrow one.
"""

from __future__ import annotations

import os

_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(_TESTS_DIR)


def primary_checkout() -> str:
    """The main checkout for this repo — `REPO_ROOT` unless we are in a worktree.

    `git worktree add .worktrees/<name>` puts the tree two levels under the
    primary checkout, so `<root>/.worktrees/<name>` → `<root>`. Detected by the
    directory layout rather than by shelling out to git, so it costs nothing at
    import time and works with no git binary.
    """
    parent = os.path.dirname(REPO_ROOT)
    if os.path.basename(parent) == ".worktrees":
        return os.path.dirname(parent)
    return REPO_ROOT


def tool_schemas_path() -> str | None:
    """Absolute path to `tool_schemas.json`, or None if it exists nowhere.

    None means the artifact has genuinely never been built — run
    `python3 scripts/build_tool_schemas.py`. That is a real reason to skip;
    "I am in a worktree" is not.
    """
    for root in (REPO_ROOT, primary_checkout()):
        candidate = os.path.join(root, "tool_schemas.json")
        if os.path.exists(candidate):
            return candidate
    return None
