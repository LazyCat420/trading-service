"""Disposable git worktrees — one per candidate patch.

CORAL gives every agent its own worktree so concurrent attempts cannot collide
and a bad edit is thrown away with the directory. The old deployer instead wrote
the proposal straight over the file in the live checkout and kept a ``.bak``
beside it; the only thing standing between a truncated rewrite and the source
was a size-ratio check bolted on at the end.

Grading in a worktree also removes the reason that check had to exist: a patch
that destroys a file destroys a copy, and the grader sees the wreckage as a
score of 0 rather than as a file the next cycle has to run.
"""
from __future__ import annotations

import logging
import shutil
import subprocess
import threading
import uuid
from contextlib import contextmanager
from pathlib import Path

logger = logging.getLogger(__name__)

# `git worktree add/remove` mutates shared repo metadata. Candidates are graded
# on threads in parallel, so serialise the bookkeeping — the expensive part (the
# test run) still overlaps.
_WORKTREE_LOCK = threading.Lock()

PROJECT_ROOT = Path(__file__).resolve().parents[4]

# Kept inside the repo (and gitignored) so a crashed run leaves something
# obvious to clean up rather than litter in /tmp with no owner.
WORKTREE_DIR = PROJECT_ROOT / ".evo-worktrees"

# Gitignored files a test run genuinely needs. Symlinked rather than copied:
# .env holds credentials and should exist in exactly one place.
_LINKED_FILES = (".env",)


class NotAGitCheckout(RuntimeError):
    """Raised when the repair loop is run somewhere it cannot grade.

    Notably the trading-service container: its image ships source without .git
    and without the git binary, which is why the loop is host-side.
    """


def git(*args: str, cwd: Path | None = None, check: bool = True) -> str:
    res = subprocess.run(
        ["git", *args],
        cwd=str(cwd or PROJECT_ROOT),
        capture_output=True,
        text=True,
        timeout=120,
    )
    if check and res.returncode != 0:
        raise RuntimeError(
            f"git {' '.join(args)} failed ({res.returncode}): "
            f"{res.stderr.strip() or res.stdout.strip()}"
        )
    return res.stdout.strip()


def assert_git_available() -> str:
    """Return the current HEAD sha, or explain why this host cannot grade."""
    if not (PROJECT_ROOT / ".git").exists():
        raise NotAGitCheckout(
            f"{PROJECT_ROOT} is not a git checkout — the repair loop grades by "
            "running the suite against a worktree, so it must run on a host "
            "clone, not inside the service image."
        )
    if shutil.which("git") is None:
        raise NotAGitCheckout("git is not installed on this host")
    return git("rev-parse", "HEAD")


@contextmanager
def attempt_worktree(label: str = ""):
    """Yield a fresh detached worktree at HEAD; always remove it afterwards.

    The worktree is detached on purpose — a candidate must never be able to move
    a branch that a human is using.
    """
    assert_git_available()
    WORKTREE_DIR.mkdir(exist_ok=True)
    slug = f"{label or 'attempt'}-{uuid.uuid4().hex[:8]}".replace("/", "_")
    path = WORKTREE_DIR / slug

    with _WORKTREE_LOCK:
        git("worktree", "add", "--detach", str(path), "HEAD")
    logger.info("[CORAL-WT] created %s", path)
    try:
        for name in _LINKED_FILES:
            src = PROJECT_ROOT / name
            if src.exists() and not (path / name).exists():
                (path / name).symlink_to(src)
        yield path
    finally:
        try:
            with _WORKTREE_LOCK:
                git("worktree", "remove", "--force", str(path))
        except Exception as e:  # noqa: BLE001 — cleanup must not mask the result
            logger.warning("[CORAL-WT] remove failed for %s: %s", path, e)
            shutil.rmtree(path, ignore_errors=True)
            try:
                with _WORKTREE_LOCK:
                    git("worktree", "prune")
            except Exception:
                pass
        logger.info("[CORAL-WT] removed %s", path)


def commit_and_label(path: Path, message: str, branch: str,
                     paths: list[str] | None = None) -> str:
    """Commit the worktree and point ``branch`` at it. Returns the new sha.

    The branch is created *before* the worktree is torn down. A detached commit
    in a removed worktree is unreferenced and a stray `git gc` is entitled to
    delete it, so labelling later would be a race against garbage collection.

    ``paths`` is explicit rather than ``git add -A``. The worktree is cut fresh
    from HEAD so in practice nothing else is dirty, but the previous incarnation
    of this loop committed with ``add -A`` from the live checkout and swept up
    whatever a human happened to have in progress.
    """
    if paths:
        git("add", "--", *paths, cwd=path)
    else:
        git("add", "-A", cwd=path)
    git("-c", "user.name=coral-evo",
        "-c", "user.email=coral-evo@localhost",
        "commit", "-m", message, cwd=path)
    sha = git("rev-parse", "HEAD", cwd=path)
    with _WORKTREE_LOCK:
        git("branch", "-f", branch, sha)
    return sha


def compare_url(branch: str, *, remote: str = "origin") -> str:
    origin = git("remote", "get-url", remote)
    if origin.endswith(".git"):
        origin = origin[:-4]
    if origin.startswith("git@github.com:"):
        origin = "https://github.com/" + origin[len("git@github.com:"):]
    return f"{origin}/compare/{branch}?expand=1"


def push_branch(branch: str, *, remote: str = "origin") -> str:
    """Push a local branch and return its compare URL.

    Pushing a branch is where automation stops. Nothing here merges, and nothing
    redeploys — a green suite says the patch did not break the tests we have, not
    that the trading behaviour it changes is correct.
    """
    git("push", remote, f"refs/heads/{branch}:refs/heads/{branch}")
    return compare_url(branch, remote=remote)
