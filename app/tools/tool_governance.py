"""Where the tool catalog is, and what is allowed to be missing from it.

WHY THIS FILE EXISTS. On 2026-08-07 an audit found `get_parameters` and
`propose_parameter_change` had been registered, whitelisted for three agents,
and published on both prism personas since 07-18 — and never built into
`tool_schemas.json`. Twenty days, zero calls, nothing red. The defect is not
the missing pair; it is that **an agent can be advertised a capability it
cannot exercise and nothing fails.**

`tests/unit/test_tool_catalog_invariants.py` closes that class. This module
holds the two things the test must not invent for itself:

  * `catalog_path()` — where the built catalog actually is, resolved the same
    way at test time as at boot;
  * `INTENTIONALLY_UNADVERTISED` — the exceptions, each with a written reason.

**An empty exception list is the goal, not a defect.** It is empty today
(measured 2026-08-08: 56 trading-scoped schemas, 56 registered handlers, a
clean 1:1). Adding an entry is a deliberate act that a reviewer can see, which
is the whole difference between this and the warning line in
`tool_whitelists.get_agent_tools` that logged the parameter-tool gap
harmlessly for twenty days.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Dict, List

#: Registered handlers that are deliberately absent from the built catalog.
#: `{tool_name: why}`. A reason is required — an entry without one is a
#: silenced test, and silencing without a reason is what this file prevents.
INTENTIONALLY_UNADVERTISED: Dict[str, str] = {}

#: Whitelisted names that resolve to no schema on purpose. Prism's dynamic
#: discovery meta-tools land here if they are ever added to a static whitelist:
#: they are prism-local and correctly absent from this catalog. Empty today —
#: `get_agent_enabled_tool_names` merges them at call time instead.
WHITELISTED_WITHOUT_A_SCHEMA: Dict[str, str] = {}

_REPO_ROOT = Path(__file__).resolve().parents[2]


def catalog_path() -> Path | None:
    """The built catalog this service reads, or None if there isn't one.

    Order matters and mirrors reality rather than preference:

    1. ``TOOL_CATALOG_PATH`` — so the invariant test can be pointed at an
       older catalog and *proven* to go red. A guard that has never failed is
       not known to work, and this is what makes that check reproducible
       instead of a manual checkout somebody did once.
    2. ``trading-service/tool_schemas.json`` — what `app/tools/registry.py`
       loads at boot. Gitignored, so a fresh worktree has none.
    3. ``lazy-agent-service/tool_schemas.json`` — the repo that BUILDS it and
       the only tracked copy. Note the direction: this is the source, and the
       local file is a deployed copy of it.

    The sibling search walks UP rather than looking only at the immediate
    parent, because a git worktree sits at ``sun/.worktrees/<name>`` and its
    parent is ``.worktrees/``, not ``sun/``. A single-level lookup skips the
    whole test file from a worktree and reports it as "no catalog found" —
    which is a skip, and a skipped invariant is indistinguishable from a
    passing one in a summary line. This is the same shape that already stops
    trading-client's suite running from a worktree.
    """
    override = os.environ.get("TOOL_CATALOG_PATH")
    if override:
        p = Path(override)
        return p if p.exists() else None

    if (local := _REPO_ROOT / "tool_schemas.json").exists():
        return local

    # The tool-server repo was renamed lazy-tool-service -> lazy-agent-service;
    # accept either so a checkout predating the rename still resolves.
    for ancestor in (_REPO_ROOT.parent, *_REPO_ROOT.parents[1:3]):
        for repo in ("lazy-agent-service", "lazy-tool-service"):
            candidate = ancestor / repo / "tool_schemas.json"
            if candidate.exists():
                return candidate
    return None


def load_catalog() -> List[dict]:
    """Every schema in the built catalog, ecosystem-wide and unfiltered."""
    path = catalog_path()
    if path is None:
        return []
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, list):
        raise ValueError(f"{path} is not a list of schemas")
    return data


def catalog_names() -> set[str]:
    return {t["name"] for t in load_catalog() if t.get("name")}
