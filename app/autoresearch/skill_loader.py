"""
SkillOpt inference-time loader — serves each V3 agent's learned skill doc as a
system-prompt prefix.

Design constraints:
- Zero added latency on the hot path: module-level cache; the DB is hit only on
  cold start, TTL expiry, or explicit invalidation.
- Fail-silent: any error returns "" — an agent run must never block on skills.
- The V3 system prompt must stay byte-identical between skill mutations so
  vLLM prefix caching keeps working; the prefix only changes when the
  optimizer accepts an edit (at most once per autoresearch run).
- TTL backstop: autoresearch runs in cycle_main, but the API server is a
  separate process that invalidate_skill_cache() cannot reach — the TTL keeps
  any other process from serving stale skills forever.
"""

from __future__ import annotations

import logging
import time

logger = logging.getLogger(__name__)

_SKILL_HEADER = "## Agent Skill Guidance (SkillOpt)\n"
_CACHE_TTL_SEC = 900.0

# agent_name -> (prefix_text, fetched_monotonic). Misses are cached too, so a
# missing/broken table costs one round-trip per TTL window, not one per agent run.
_skill_cache: dict[str, tuple[str, float]] = {}


def load_skill_prefix(agent_name: str, bust_cache: bool = False) -> str:
    """Return the active skill doc for `agent_name` formatted as a
    system-prompt prefix, or "" when there is no skill yet (or on any error)."""
    if not agent_name:
        return ""
    cached = _skill_cache.get(agent_name)
    if cached and not bust_cache and (time.monotonic() - cached[1]) < _CACHE_TTL_SEC:
        return cached[0]

    prefix = ""
    try:
        from app.db.connection import get_db

        with get_db() as db:
            row = db.execute(
                "SELECT skill_text FROM agent_skills "
                "WHERE agent_name = %s AND status = 'active' "
                "ORDER BY version DESC LIMIT 1",
                [agent_name],
            ).fetchone()
        text = (row[0] or "").strip() if row else ""
        if text:
            prefix = f"{_SKILL_HEADER}{text}\n\n"
    except Exception as e:  # noqa: BLE001 — advisory context, never blocks an agent
        logger.debug("[SkillOpt] skill load failed for %s: %s", agent_name, e)
    _skill_cache[agent_name] = (prefix, time.monotonic())
    return prefix


def invalidate_skill_cache(agent_name: str | None = None) -> None:
    """Drop cached skills so the next load re-reads the DB (this process only)."""
    if agent_name is None:
        _skill_cache.clear()
    else:
        _skill_cache.pop(agent_name, None)
