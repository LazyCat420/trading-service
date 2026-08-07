#!/usr/bin/env python3
"""Which whitelisted tools do agents actually call — and which calls bypass the whitelist?

    python scripts/tool_audit.py --days 7
    python scripts/tool_audit.py --days 30 --json /tmp/audit.json

## Why this script exists

Three separate manual analyses of this question on 2026-07-25 each reached a
DIFFERENT wrong answer, because each was a one-off SQL query:

1. First pass concluded "zero whitelisted tools are used by ANY agent" — an
   artifact of the ``mcp__lazy-tool-service__`` prefix. Prism renames tools on
   registration, so the telemetry name never equals the whitelist name. Any
   audit that does not normalize the prefix reports 100% non-compliance.
2. Second pass concluded ``discover_and_enable_tools`` was "the #2 tool in the
   system, 498 calls, 18% of all activity." True over 30 days, but it has been
   at ZERO since 2026-07-23. A 30-day window was reporting a fixed problem as
   live.
3. Third pass called ``get_sec_filings`` "broken at 15% success." It failed
   0/11 on 07-14 and has since recovered (7/25: 1/1, 7/23: 4/4).

The lesson is the same each time: **window the data, normalize the names, and
separate "never called" from "not called lately."** This script does all three
so the answer stops depending on who wrote the query.

## Reading the output

- ``used`` / ``never`` are computed against the agent's OWN whitelist.
- ``off-whitelist`` means the tool was called but is not on the whitelist. That
  is the enforcement question, not a usage question.
- ``per-run`` normalizes by agent invocations from ``v3_agent_telemetry``. Raw
  counts mislead: an agent that runs 6x cannot look busy next to one that runs
  349x, and pruning on raw counts would delete the rare agent's whole toolset.
- A tool that is dead in 7d but alive in 30d is flagged RECENTLY-QUIET, not
  dead. The last week was almost all HOLD decisions, so BUY/SELL-specific tools
  (``calculate_stop_loss``, ``calculate_risk_reward``) go quiet without being
  unused. **Prune on 30-day zero; use the 7-day window only to spot problems
  that have already been fixed.**
"""

from __future__ import annotations

import argparse
import importlib
import json
import os
import pkgutil
import sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Prism renames every tool it registers. Strip it before comparing to a
# whitelist or every tool looks non-compliant — failure mode 1 in the docstring
# above.
#
# This used to be a local `MCP_PREFIX = "mcp__lazy-tool-service__"`, matching
# exactly one spelling. The service was renamed to lazy-agent-service on
# 2026-08-07 and prism mints the prefix from ITS registration name, so a run
# after the flip would have normalized nothing and re-reported the same "zero
# whitelisted tools are used by any agent" that this script exists to prevent.
# An audit tool that can be wrong in the way it documents is worse than no audit.
from app.services.mcp_prefix import MCP_PREFIXES, strip_mcp_prefix  # noqa: E402

# Framework-provided tools that are never on an agent whitelist by design.
META_TOOLS = {
    "discover_and_enable_tools", "enable_tools", "search_tools", "think",
}


def normalize(tool: str) -> str:
    return strip_mcp_prefix(tool)


def load_whitelists() -> dict[str, list[str]]:
    """Read TOOL_WHITELIST straight from the V3 agent modules.

    These modules are the single source of truth: `prism_registration` reads
    `module.TOOL_WHITELIST` directly, and `app/agents/tool_whitelists.py`
    merges them at import (it is NOT a competing copy — do not hand-edit the
    dict for V3 agents, and do not delete the merge).
    """
    import app.v3.agents as pkg

    out: dict[str, list[str]] = {}
    for mod_info in pkgutil.iter_modules(pkg.__path__):
        try:
            module = importlib.import_module(f"app.v3.agents.{mod_info.name}")
        except Exception:
            continue
        name = getattr(module, "AGENT_NAME", None)
        wl = getattr(module, "TOOL_WHITELIST", None)
        if name and wl is not None:
            out[name] = list(wl)
    return out


def fetch(days: int) -> tuple[dict, dict, dict]:
    from app.db.connection import get_db

    calls: dict[str, dict[str, dict]] = defaultdict(lambda: defaultdict(
        lambda: {"n": 0, "ok": 0}))
    runs: dict[str, int] = {}
    daily: dict[str, dict] = defaultdict(lambda: defaultdict(int))

    with get_db() as db:
        for agent, tool, n, ok in db.execute(
            """SELECT agent_name, tool_name, count(*), count(*) FILTER (WHERE success)
               FROM agent_tool_telemetry
               WHERE created_at > now() - (%s || ' days')::interval
               GROUP BY 1, 2""", [days],
        ).fetchall():
            key = normalize(tool or "")
            cell = calls[agent][key]
            cell["n"] += n
            cell["ok"] += ok or 0

        for agent, n in db.execute(
            """SELECT agent_name, count(*) FROM v3_agent_telemetry
               WHERE created_at > now() - (%s || ' days')::interval
               GROUP BY 1""", [days],
        ).fetchall():
            runs[agent] = n

        for day, tool, n in db.execute(
            """SELECT created_at::date, tool_name, count(*)
               FROM agent_tool_telemetry
               WHERE created_at > now() - (%s || ' days')::interval
               GROUP BY 1, 2""", [days],
        ).fetchall():
            daily[str(day)][normalize(tool or "")] += n

    return calls, runs, daily


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--compare-days", type=int, default=30,
                    help="Longer window used to separate DEAD from RECENTLY-QUIET")
    ap.add_argument("--json", dest="json_out")
    args = ap.parse_args()

    wl = load_whitelists()
    calls, runs, daily = fetch(args.days)
    long_calls, long_runs, _ = fetch(args.compare_days)

    print("=" * 100)
    print(f"TOOL AUDIT — last {args.days}d (compared against {args.compare_days}d)")
    print("=" * 100)
    print(f"names normalized past {'/'.join(MCP_PREFIXES)}; "
          f"meta-tools shown separately\n")

    report = {}
    for agent in sorted(wl):
        w = set(wl[agent])
        u = calls.get(agent, {})
        lu = long_calls.get(agent, {})
        n_runs = runs.get(agent, 0)
        n_runs_long = long_runs.get(agent, 0)

        if not u and not lu:
            print(f"### {agent}  wl={len(w)}  runs={n_runs}  — NO TOOL CALLS in either window")
            if n_runs_long:
                print(f"    (it DID run {n_runs_long}x in {args.compare_days}d — "
                      f"zero tool calls is by design for this agent, not a gap)\n")
            report[agent] = {"runs": n_runs, "whitelist": sorted(w), "used": {},
                             "never": sorted(w), "off_whitelist": {}}
            continue

        used = {t: c for t, c in u.items() if t in w}
        off = {t: c for t, c in u.items() if t and t not in w and t not in META_TOOLS}
        meta = {t: c for t, c in u.items() if t in META_TOOLS}
        blank = u.get("", {}).get("n", 0)

        dead, quiet = [], []
        for t in sorted(w - set(used)):
            (quiet if lu.get(t, {}).get("n", 0) else dead).append(t)

        per_run = (sum(c["n"] for c in u.values()) / n_runs) if n_runs else 0
        print(f"### {agent}  wl={len(w)} used={len(used)} runs={n_runs} "
              f"calls/run={per_run:.1f}")
        if used:
            def _fmt(t, c):
                # Only show the success count when it differs — a partly
                # failing tool is a bug, not a usage signal, and burying it in
                # every line hides it.
                return f"{t}({c['n']})" if c["n"] == c["ok"] else f"{t}({c['n']}/{c['ok']}ok)"
            print("    USED:", ", ".join(
                _fmt(t, c) for t, c in sorted(used.items(), key=lambda kv: -kv[1]["n"])))
        if dead:
            print(f"    DEAD ({args.compare_days}d zero too) → SAFE TO PRUNE:", ", ".join(dead))
        if quiet:
            print(f"    RECENTLY-QUIET (used within {args.compare_days}d) → KEEP:", ", ".join(quiet))
        if off:
            print("    ⚠ OFF-WHITELIST (enforcement gap):", ", ".join(
                f"{t}({c['n']})" for t, c in sorted(off.items(), key=lambda kv: -kv[1]["n"])))
        if meta:
            print("    meta-tools:", ", ".join(f"{t}({c['n']})" for t, c in meta.items()))
        if blank:
            print(f"    ⚠ {blank} EMPTY-NAME calls")
        print()

        report[agent] = {
            "runs": n_runs, "calls_per_run": round(per_run, 2),
            "whitelist": sorted(w),
            "used": {t: c["n"] for t, c in used.items()},
            "dead": dead, "recently_quiet": quiet,
            "off_whitelist": {t: c["n"] for t, c in off.items()},
            "meta_tools": {t: c["n"] for t, c in meta.items()},
            "empty_name": blank,
        }

    # Trend: a tool at zero for several days running is fixed, not broken.
    print("=" * 100)
    print("DAILY TREND — meta-tools and failures (is a 'problem' current or already fixed?)")
    print("=" * 100)
    watch = ["discover_and_enable_tools", "enable_tools", "search_tools", "", "get_sec_filings"]
    print(f"{'date':<12}" + "".join(f"{(w or 'EMPTY'):>28}" for w in watch))
    for day in sorted(daily, reverse=True)[:14]:
        print(f"{day:<12}" + "".join(f"{daily[day].get(w, 0):>28}" for w in watch))

    if args.json_out:
        with open(args.json_out, "w") as fh:
            json.dump(report, fh, indent=2)
        print(f"\nwrote {args.json_out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
