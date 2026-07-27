"""Extract a unified diff from a model response and apply it to a worktree.

Asking for a diff instead of a file is the single change that removes the old
council's dominant failure. Measured on this repo: 27 of the 30 repair targets
are larger than the 4,000-char slice the proposer was shown, and 11 are larger
than a 4096-token completion can emit at all, so 33 of 57 debates died on
literal mid-statement truncation (``ON CONFLICT (ticker, date,``). A diff for a
one-line fix is a few hundred tokens no matter how large the file is.

The cost of diffs is that models are bad at hunk arithmetic, so the apply path
is a ladder of increasingly forgiving strategies rather than a single
``git apply``. Every rung is still a real apply — none of them fall back to
"write the model's text over the file", which is what we are escaping.
"""
from __future__ import annotations

import logging
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from app.cognition.evolution.repair_scope import is_patchable

logger = logging.getLogger(__name__)

_FENCE_RE = re.compile(r"```(?:diff|patch|udiff)?\s*\n(.*?)```", re.DOTALL)
# The evidence excerpt is line-numbered ("  42| code"), and models echo that
# formatting back into the diff body often enough to be worth repairing.
_NUMBERED_RE = re.compile(r"^([ +-])\s*\d+\|\s?(.*)$")


class PatchError(RuntimeError):
    """The response contained no usable diff, or it would not apply."""


@dataclass
class AppliedPatch:
    diff: str
    files: list[str]
    strategy: str


def extract_diff(text: str) -> str:
    """Pull the unified diff out of a model response.

    Prefers a fenced block; falls back to the first ``diff --git``/``---`` line,
    because a model that ignored the fence instruction usually still emits a
    well-formed diff.
    """
    if not text:
        raise PatchError("empty response")

    candidates: list[str] = []
    for m in _FENCE_RE.finditer(text):
        block = m.group(1)
        if "@@" in block or block.lstrip().startswith("diff --git"):
            candidates.append(block)

    if not candidates:
        for marker in ("diff --git ", "--- a/", "--- "):
            idx = text.find(marker)
            if idx != -1 and "@@" in text[idx:]:
                candidates.append(text[idx:])
                break

    if not candidates:
        raise PatchError("no unified diff found in the response")

    diff = max(candidates, key=len)
    return _repair_diff_text(diff)


def _repair_diff_text(diff: str) -> str:
    """Fix the two formatting mistakes models reliably make."""
    lines = diff.splitlines()

    # 1. Line-number prefixes copied out of the evidence excerpt.
    body = [ln for ln in lines if ln[:1] in " +-"]
    numbered = [ln for ln in body if _NUMBERED_RE.match(ln)]
    if body and len(numbered) > len(body) // 2:
        logger.info(
            "[CORAL-PATCH] stripping line-number prefixes from %d/%d body lines",
            len(numbered), len(body),
        )
        lines = [
            (_NUMBERED_RE.sub(r"\1\2", ln) if _NUMBERED_RE.match(ln) else ln)
            for ln in lines
        ]

    # 2. A trailing newline is mandatory; git apply rejects the last hunk without
    #    it and the error ("corrupt patch") names the wrong cause.
    return "\n".join(lines).rstrip("\n") + "\n"


def diff_files(diff: str) -> list[str]:
    """Repo-relative paths a diff touches, taken from its +++ lines."""
    out: list[str] = []
    for line in diff.splitlines():
        if line.startswith("+++ "):
            path = line[4:].strip().split("\t")[0]
            if path == "/dev/null":
                continue
            if path.startswith(("a/", "b/")):
                path = path[2:]
            if path not in out:
                out.append(path)
    return out


def assert_diff_in_scope(diff: str) -> list[str]:
    """Every file the diff touches must be patchable. Deny wins.

    This is checked before the patch reaches a worktree, so an out-of-scope
    proposal costs nothing and is recorded as a refusal rather than a failure.
    """
    files = diff_files(diff)
    if not files:
        raise PatchError("diff names no target file")
    for rel in files:
        allowed, reason = is_patchable(rel)
        if not allowed:
            raise PatchError(f"out of repair scope: {rel} ({reason})")
    return files


# Ladder of apply strategies. Each is a real patch application; they differ only
# in how much slack they give the model's hunk headers and context.
_STRATEGIES: tuple[tuple[str, list[str]], ...] = (
    ("git-apply", ["git", "apply", "--whitespace=nowarn"]),
    ("git-apply-recount", ["git", "apply", "--recount", "--whitespace=nowarn"]),
    ("git-apply-3way", ["git", "apply", "--3way", "--whitespace=nowarn"]),
    ("git-apply-fuzzy", ["git", "apply", "--recount", "-C1", "--whitespace=nowarn"]),
    ("patch-fuzz", ["patch", "-p1", "--batch", "--fuzz=3", "--forward"]),
)


def apply_diff(worktree: Path, diff: str) -> AppliedPatch:
    """Apply ``diff`` inside ``worktree``. Raises ``PatchError`` if nothing works."""
    files = assert_diff_in_scope(diff)

    errors: list[str] = []
    for name, cmd in _STRATEGIES:
        res = subprocess.run(
            cmd, cwd=str(worktree), input=diff,
            capture_output=True, text=True, timeout=120,
        )
        if res.returncode == 0:
            if name != "git-apply":
                logger.info("[CORAL-PATCH] applied via %s", name)
            return AppliedPatch(diff=diff, files=files, strategy=name)
        errors.append(f"{name}: {(res.stderr or res.stdout).strip()[:200]}")

    raise PatchError("no apply strategy succeeded — " + " | ".join(errors))


def is_noop(worktree: Path) -> bool:
    """True when the worktree is unchanged.

    A grader that only checks "the suite still passes" scores an empty diff
    perfectly, so an unchanged tree has to be caught explicitly rather than
    rewarded for breaking nothing.
    """
    res = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=str(worktree), capture_output=True, text=True, timeout=60,
    )
    return not res.stdout.strip()
