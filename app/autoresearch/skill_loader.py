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
from app.db import mongo_query

logger = logging.getLogger(__name__)

_SKILL_HEADER = "## Agent Skill Guidance (SkillOpt)\n"
_CACHE_TTL_SEC = 900.0

# agent_name -> (prefix_text, version, fetched_monotonic). Misses are cached too,
# so a missing/broken table costs one round-trip per TTL window, not one per run.
_skill_cache: dict[str, tuple[str, int | None, float]] = {}


def load_skill_prefix(agent_name: str, bust_cache: bool = False) -> str:
    """Return the active skill doc for `agent_name` formatted as a
    system-prompt prefix, or "" when there is no skill yet (or on any error)."""
    return _load(agent_name, bust_cache)[0]


def active_skill_version(agent_name: str) -> int | None:
    """The version of the doc `load_skill_prefix` is currently serving.

    Reads the same cache entry the prompt was built from, so the recorded
    version is the one the agent actually ran under — not whatever is newest in
    the DB at the moment someone asks. Those differ: the optimizer can accept a
    new version mid-cycle while this process serves the cached older one for up
    to _CACHE_TTL_SEC.

    None means no skill doc (or a load failure), and is stored as NULL rather
    than being defaulted to a version number.
    """
    return _load(agent_name, False)[1]


def _load(agent_name: str, bust_cache: bool) -> tuple[str, int | None]:
    if not agent_name:
        return "", None
    cached = _skill_cache.get(agent_name)
    if cached and not bust_cache and (time.monotonic() - cached[2]) < _CACHE_TTL_SEC:
        return cached[0], cached[1]

    prefix = ""
    version: int | None = None
    try:

        row = mongo_query.find_row('agent_skills', {'agent_name': agent_name, 'status': 'active'}, ['skill_text', 'version'], sort=[('version', -1)])
        text = (row[0] or "").strip() if row else ""
        if text:
            prefix = f"{_SKILL_HEADER}{text}\n\n"
            try:
                version = int(row[1]) if row[1] is not None else None
            except (TypeError, ValueError):
                version = None
    except Exception as e:  # noqa: BLE001 — advisory context, never blocks an agent
        logger.debug("[SkillOpt] skill load failed for %s: %s", agent_name, e)
    _skill_cache[agent_name] = (prefix, version, time.monotonic())
    return prefix, version


def active_skill_versions() -> dict[str, int]:
    """{agent_name: version} for every target agent currently serving a doc.

    Stamped onto decision_outcomes so a later analysis can ask which version
    governed a trade. Agents with no doc are omitted rather than recorded as
    version 0 — absent and "version zero" are different claims.
    """
    out: dict[str, int] = {}
    try:
        from app.autoresearch.skill_optimizer import TARGET_AGENTS

        for name in TARGET_AGENTS:
            v = active_skill_version(name)
            if v is not None:
                out[name] = v
    except Exception as e:  # noqa: BLE001 — telemetry, never blocks a cycle
        logger.debug("[SkillOpt] version snapshot failed: %s", e)
    return out


def invalidate_skill_cache(agent_name: str | None = None) -> None:
    """Drop cached skills so the next load re-reads the DB (this process only)."""
    if agent_name is None:
        _skill_cache.clear()
    else:
        _skill_cache.pop(agent_name, None)
