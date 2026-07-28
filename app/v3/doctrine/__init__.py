"""Pinned analytical doctrine — method documents that ship in a system prompt.

A doctrine is a distilled analytical method held as a FILE IN THE REPO, not as
a row in `agent_skills`. That distinction is the entire reason this package
exists, and it is deliberate in both directions:

  - `agent_skills` is written by `autoresearch/skill_optimizer.py`, which
    issues REPLACE actions against a doc and rolls it back on outcome scores.
    Its own comments record the board's doc travelling 1146 -> 1812 chars
    across 20 accepted rewrites in five days. That is the right behaviour for a
    LEARNED skill and the wrong behaviour for a SOURCE DOCUMENT: a doctrine
    mined from a specific corpus stops being evidence about that corpus the
    moment a gradient walk edits it, and nobody can tell which sentences
    survived.
  - A file gets review, blame, diff and revert for free, and the optimizer
    cannot reach it — it only ever writes `agent_skills.skill_text`.

So the invariant is: doctrine changes only through a reviewed commit.

The loader is fail-silent by the same rule as `skill_loader.load_skill_prefix`:
an agent run must never block on a doctrine, because the agent still has the
precomputed valuation block and its own method prompt without one.
"""

from __future__ import annotations

import logging
from functools import lru_cache
from pathlib import Path

logger = logging.getLogger(__name__)

_DIR = Path(__file__).parent

# Doctrine rides the SYSTEM half of the prompt on every ticker of every cycle,
# so its size is a per-run tax, not a one-off. The ceiling is enforced rather
# than documented, because MAX_SKILL_CHARS taught the lesson next door: a limit
# the code does not check is a suggestion, and skill docs grew straight through
# theirs until the write path started rejecting them.
MAX_DOCTRINE_CHARS = 6000


@lru_cache(maxsize=8)
def load_doctrine(name: str) -> str:
    """Return the doctrine document `name`, or "" when it cannot be served.

    Cached for the process lifetime — these files change only on deploy, and
    re-reading one per agent run would add I/O to the hot path for no benefit.
    """
    try:
        path = (_DIR / f"{name}.md").resolve()
        # A doctrine name is a code constant, never user input; the containment
        # check exists so it stays that way if one ever becomes configurable.
        if _DIR.resolve() not in path.parents:
            raise ValueError(f"doctrine name escapes the package: {name!r}")
        text = path.read_text(encoding="utf-8").strip()
        if len(text) > MAX_DOCTRINE_CHARS:
            raise ValueError(
                f"doctrine {name!r} is {len(text)} chars, over the "
                f"{MAX_DOCTRINE_CHARS} ceiling"
            )
        return text
    except Exception as e:  # noqa: BLE001 — advisory, never blocks an agent
        logger.warning("[Doctrine] %s unavailable (%s: %s) — the agent will run "
                       "on its method prompt alone", name, type(e).__name__, e)
        return ""


def available() -> list[str]:
    """Doctrine names on disk. Used by tests and the promote step."""
    return sorted(p.stem for p in _DIR.glob("*.md"))
