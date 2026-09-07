"""Serve code-reviewed methods, with a bounded cache and static fallback.

Unreviewed database text cannot become a system instruction. Store outages use
the reviewed baseline and record health; an emergency switch bypasses the cache.
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
    system-prompt prefix, or "" for unknown roles or when serving is disabled."""
    return _load(agent_name, bust_cache)[0]


def active_skill_version(agent_name: str) -> int | None:
    """The version of the doc `load_skill_prefix` is currently serving.

    Reads the same cache entry the prompt was built from, so the recorded
    version is the one the agent actually ran under — not whatever is newest in
    the DB at the moment someone asks. Those differ: the optimizer can accept a
    new version mid-cycle while this process serves the cached older one for up
    to _CACHE_TTL_SEC.

    None means no delivered skill, and is stored as NULL rather
    than being defaulted to a version number.
    """
    return _load(agent_name, False)[1]


def _load(agent_name: str, bust_cache: bool) -> tuple[str, int | None]:
    from app.services.learning.policy import enabled
    if not agent_name or not enabled("skills"):
        return "", None
    cached = _skill_cache.get(agent_name)
    if cached and not bust_cache and (time.monotonic() - cached[2]) < _CACHE_TTL_SEC:
        return cached[0], cached[1]

    from app.services.learning.policy import BASELINES, BASELINE_VERSION, enabled, skill_allowed
    prefix = ""
    version: int | None = None
    if not enabled("skills"):
        return "", None
    text = BASELINES.get(agent_name, "")
    version = BASELINE_VERSION if text else None
    state = "reviewed_baseline"
    try:
        row = mongo_query.find_row('agent_skills', {'agent_name': agent_name, 'status': 'active'},
                                   ['skill_text', 'version'], sort=[('version', -1)])
        if row and row[0] and skill_allowed(agent_name, row[0]):
            text, version = row[0].strip(), int(row[1])
            state = "active"
        elif row and row[0]:
            state = "unreviewed_excluded"
            logger.warning("[SkillOpt] %s: excluded unreviewed skill v%s; serving reviewed methods", agent_name, row[1])
    except Exception as exc:
        state = "store_unavailable_baseline"
        logger.warning("[SkillOpt] %s skill store unavailable: %s", agent_name, exc)
    if text:
        prefix = f"{_SKILL_HEADER}{text}\n\n"
    from app.services.learning.health import record
    record(f"skills:{agent_name}", state, version=version)
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
