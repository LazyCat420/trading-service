"""
Blast-radius limits for automated self-healing patches.

The self-healing watchdog generates source patches with an LLM debate council.
Left unbounded it could rewrite anything reachable from a traceback — including
its own machinery, the deploy scripts, the DB schema, or credentials handling.

The rule this module enforces: automated repair may touch the code that runs the
TRADING CYCLE, and nothing else. Everything about how the service is built,
deployed, migrated, or configured stays under human control, as does the repair
machinery itself.

Deny wins over allow. An unrecognised path is denied, not allowed — a mistake
here writes code to disk, so the default must be refusal.
"""
from __future__ import annotations

import logging
from pathlib import PurePosixPath

logger = logging.getLogger(__name__)


# Trading-cycle code: collectors feed it, cognition/v3/agents reason over it,
# trading executes it, services orchestrate it, autoresearch evaluates it.
ALLOWED_PREFIXES: tuple[str, ...] = (
    "app/collectors/",
    "app/cognition/",
    "app/v3/",
    "app/agents/",
    "app/services/",
    "app/trading/",
    "app/autoresearch/",
    "app/analytics/",
    "app/processors/",
    "app/scraper/",
)

# Checked BEFORE the allowlist. Several of these sit inside allowed prefixes and
# are carved back out deliberately.
DENIED_PREFIXES: tuple[str, ...] = (
    # The repair machinery must not rewrite itself — a bad patch here can
    # disable the very guard that would have caught the next bad patch.
    "app/cognition/evolution/",
    # Schema and migrations: a bad ALTER is not recoverable by redeploying.
    "app/db/",
    # Settings, secrets handling, model/context configuration.
    "app/config/",
    # Build, deploy, and dependency surfaces.
    "scripts/",
    "deploy",
    "Dockerfile",
    "docker-compose",
    "requirements",
    "entrypoint",
    ".github/",
    # Tests are the evidence that a fix worked. Letting the fixer edit them
    # lets it "pass" by rewriting the assertion.
    "tests/",
)

# Only source files the cycle actually executes.
ALLOWED_SUFFIXES: tuple[str, ...] = (".py", ".md")


def is_patchable(relative_path: str) -> tuple[bool, str]:
    """Return ``(allowed, reason)`` for an automated patch to ``relative_path``.

    ``relative_path`` is repo-relative and POSIX-style, e.g. ``app/v3/orchestrator.py``.
    """
    if not relative_path:
        return False, "empty path"

    raw = relative_path.replace("\\", "/")

    # Reject absolute paths and traversal BEFORE any normalisation. Stripping
    # first is unsafe: `lstrip("./")` removes any mix of '.' and '/', which
    # quietly turns "/app/v3/orchestrator.py" into a valid-looking relative path.
    # Callers must pass a repo-relative path; an absolute one is ambiguous
    # between the host checkout and the container's /app root.
    if raw.startswith("/"):
        return False, f"absolute path not accepted, pass a repo-relative path: {relative_path!r}"
    if ".." in PurePosixPath(raw).parts:
        return False, f"path escapes the repo root: {relative_path!r}"

    # Only now is it safe to drop a leading "./".
    if raw.startswith("./"):
        raw = raw[2:]

    if not raw.endswith(ALLOWED_SUFFIXES):
        return False, f"suffix not patchable (allowed: {', '.join(ALLOWED_SUFFIXES)})"

    for denied in DENIED_PREFIXES:
        if raw.startswith(denied):
            return False, f"protected path: matches deny rule {denied!r}"

    for allowed in ALLOWED_PREFIXES:
        if raw.startswith(allowed):
            return True, f"trading-cycle code under {allowed!r}"

    return False, "outside the trading-cycle code allowlist"


def assert_patchable(relative_path: str) -> None:
    """Raise ``PermissionError`` if ``relative_path`` may not be auto-patched."""
    allowed, reason = is_patchable(relative_path)
    if not allowed:
        raise PermissionError(
            f"Automated repair refused for {relative_path!r}: {reason}. "
            "Self-healing is limited to trading-cycle source; this change needs a human."
        )
