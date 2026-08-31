#!/usr/bin/env python3
"""Which whitelisted tools do agents actually call — and which calls bypass the whitelist?

    python scripts/tool_audit.py --days 7
    python scripts/tool_audit.py --days 30 --json /tmp/audit.json

Exit 0 when the window contained tool calls to audit. Exit 2 when it did not —
see "An empty window is not a clean audit" below.

READS MONGODB. Ported off the relational archive 2026-08-30. That store
froze at the 2026-08-19 cutover, and this script reached it through the
migration package's shared connection helper, whose DSN accessor had itself
been removed from the settings object — so every run since then died with an
AttributeError before sending a single statement. Loud, but no more
informative than a wrong answer.

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

## Which log answers this question, and why the other one cannot

`agent_tool_telemetry`, and only that one. The question is per-agent, so the
audit needs a log that NAMES the agent. Measured 2026-08-30 over the last 30
days:

    agent_tool_telemetry   12,943 rows, 12 distinct agent names, 0 unattributed
    tool_usage_stats        8,163 rows — 8,139 say 'unknown', 23 say '', 1 says
                            'test_agent'. ZERO name a v3 agent.

`app/tools/registry.py` says so outright: the long-standing
`agent_name='unknown'` there is a phantom, and attribution belongs in
`agent_tool_telemetry`, which is written by the post-call hook INSIDE the
agent loop where the agent name is in scope. So the pre-port choice of table
was right — but it is right for a reason worth writing down, because the
sibling port of `scripts/audit-loop.py` found the same column had silently
decayed from 100% attributed in June to 0.6% by late July.

That choice has a price, and the audit now prints it rather than hiding it:
**`agent_tool_telemetry` is NOT a superset of `tool_usage_stats`.** Over the
108 cycle_ids both logs carried in the last 30 days, the dispatch-level log
holds 762 calls the agent-loop hook never recorded (`whiteboard_read` x634,
`get_market_data` x32, `lazy_web_search` x27, ...). Those calls cannot be
attributed to an agent from either log, so they cannot be checked against a
whitelist by anything. A green whose denominator is invisible is exactly the
failure this script exists to prevent, so the remainder is reported.

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

## calls/run had a numerator the denominator did not cover

The two halves of that ratio come from two different collections, and they do
not describe the same population. `app/v3/challenger.py:76` runs a paired A/B
of the decision agent under a `challenger-<cycle_id>` namespace. Those tool
calls land in `agent_tool_telemetry`; the challenger's runs land in
`v3_agent_telemetry` NOWHERE. Measured 2026-08-30:

    window   challenger calls in agent_tool_telemetry   in v3_agent_telemetry
    7d              132  (9 distinct ids)                        0
    30d           1,091  (135 distinct ids)                      0

For `v3_decision_synthesizer` that is 1,097 of its 1,875 calls over 30 days —
58% of the numerator sitting above a denominator that excludes it, reporting
5.1 calls/run where the pipeline figure is 2.1. The docstring above says this
number decides what gets pruned, so a 2.4x error in it is decision-grade.

The split is resolved by EVIDENCE, not by a list of shadow prefixes: a call is
counted in the ratio when `v3_agent_telemetry` — the collection that supplies
the denominator — recorded a run under that same `cycle_id`. A prefix list is
a blocklist, it drifts, and the next harness to mint a namespace walks through
it. It also under-counts today: over 30 days the evidence test finds 1,141
uncovered calls where a `^cycle-` regex finds 1,127, because 14 of them ran
under a `cycle-*` id that logged no run at all (the rest are `challenger-*`
1,091, `bench-*` 30, `sc-*` 6).

Shadow calls stay in USED / DEAD / OFF-WHITELIST. The challenger runs the real
decision agent under the real whitelist, so its calls are that agent's genuine
tool usage and an off-whitelist call it makes is a genuine enforcement gap;
pruning a tool only the challenger reaches would break the challenger. They
are excluded from calls/run alone, which is the only number the missing runs
corrupt.

## An empty window is not a clean audit

`agent_tool_telemetry` has 0 rows on 2026-08-28, -29, -30 and -31, so
`--days 3` today selects nothing. The pre-port script would have printed
"NO TOOL CALLS in either window" for all 13 agents and exited 0 — a full page
of green produced by an empty denominator. A window with no calls in it is now
exit 2: the audit did not answer, it just had nothing to read.

## The JSON

`--json` writes one key per agent, unchanged, plus one reserved key
`_unaudited` carrying the two-log remainder described above. No agent name
begins with an underscore.
"""

from __future__ import annotations

import argparse
import importlib
import json
import os
import pkgutil
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.db import mongo_query, mongo_store  # noqa: E402

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


def _cutoff(days: int) -> datetime:
    """The SQL was `created_at > now() - (%s || ' days')::interval`.

    `now()` was the DATABASE clock; this is the client's. Both are UTC and the
    two agreed exactly over the 2026-08-01..08-19 archive window (see the
    parity numbers in the port notes), so the window is the same window.
    """
    return datetime.now(timezone.utc) - timedelta(days=days)


def load_whitelists() -> tuple[dict[str, list[str]], list[str]]:
    """Read TOOL_WHITELIST straight from the V3 agent modules.

    These modules are the single source of truth: `prism_registration` reads
    `module.TOOL_WHITELIST` directly, and `app/agents/tool_whitelists.py`
    merges them at import (it is NOT a competing copy — do not hand-edit the
    dict for V3 agents, and do not delete the merge).

    Returns the whitelists AND the modules that would not import. A module that
    raises drops its agent out of the report entirely, which does not look like
    an error — it looks like an agent that does not exist. All 13 import today;
    the list is returned so that the day one stops, the audit says so instead
    of quietly auditing 12.
    """
    import app.v3.agents as pkg

    out: dict[str, list[str]] = {}
    failed: list[str] = []
    for mod_info in pkgutil.iter_modules(pkg.__path__):
        try:
            module = importlib.import_module(f"app.v3.agents.{mod_info.name}")
        except Exception as exc:  # noqa: BLE001 - reported, not swallowed
            failed.append(f"{mod_info.name} ({type(exc).__name__})")
            continue
        name = getattr(module, "AGENT_NAME", None)
        wl = getattr(module, "TOOL_WHITELIST", None)
        if name and wl is not None:
            out[name] = list(wl)
    return out, failed


def pipeline_cycle_ids() -> set[str]:
    """Every cycle_id `v3_agent_telemetry` recorded a run under, all time.

    This is the evidence set for the calls/run split, and it is read from the
    collection that SUPPLIES the denominator — so the numerator can only ever
    count calls the denominator could have counted a run for.

    All time rather than the window, deliberately: a cycle whose tool calls
    land inside the window while its first `v3_agent_telemetry` row sits just
    outside it would otherwise be misread as a shadow namespace. The collection
    is small (8,787 documents, 430 distinct ids) so the whole-history distinct
    is cheap.
    """
    return {c for c in mongo_store.distinct_values("v3_agent_telemetry", "cycle_id") if c}


def fetch(days: int, evidenced: set[str]) -> tuple[dict, dict, dict]:
    """The three statements this script used to send to Postgres.

    Table names, never resolved collection names: every `mongo_store` /
    `mongo_query` helper calls `collection_for()` internally exactly once.
    """
    cut = _cutoff(days)

    # SELECT agent_name, tool_name, count(*), count(*) FILTER (WHERE success)
    #   FROM agent_tool_telemetry WHERE created_at > ... GROUP BY 1, 2
    #
    # `count(*) FILTER (WHERE success)` has no equivalent in the group_rows
    # aggregate vocabulary, so this is the documented `$sum`/`$cond` form. NOT
    # `{"$sum": "$success"}`: Mongo's `$sum` ignores non-numeric input, so
    # summing a boolean column returns 0 for every group — silently, and it
    # would read as "every tool call failed".
    #
    # `shadow` rides along in the same pass; see the calls/run section of the
    # module docstring.
    calls: dict[str, dict[str, dict]] = defaultdict(lambda: defaultdict(
        lambda: {"n": 0, "ok": 0, "shadow": 0}))
    shadow_expr = ({"$sum": {"$cond": [{"$in": ["$cycle_id", sorted(evidenced)]}, 0, 1]}}
                   if evidenced else {"$sum": 0})
    for doc in mongo_store.aggregate("agent_tool_telemetry", [
        {"$match": {"created_at": {"$gt": cut}}},
        {"$group": {
            "_id": {"agent": "$agent_name", "tool": "$tool_name"},
            "n": {"$sum": 1},
            "ok": {"$sum": {"$cond": ["$success", 1, 0]}},
            "shadow": shadow_expr,
        }},
    ]):
        key = normalize(doc["_id"].get("tool") or "")
        cell = calls[doc["_id"].get("agent")][key]
        cell["n"] += doc["n"]
        cell["ok"] += doc["ok"] or 0
        cell["shadow"] += doc["shadow"] or 0

    # SELECT agent_name, count(*) FROM v3_agent_telemetry
    #   WHERE created_at > ... GROUP BY 1
    runs: dict[str, int] = {}
    for agent, n in mongo_query.group_rows(
        "v3_agent_telemetry", {"created_at": {"$gt": cut}},
        ["agent_name"], [("count", None)],
        [("key", "agent_name"), ("agg", 0)],
    ):
        runs[agent] = n

    # SELECT created_at::date, tool_name, count(*) FROM agent_tool_telemetry
    #   WHERE created_at > ... GROUP BY 1, 2
    #
    # A GROUP BY over an EXPRESSION, which `group_rows` cannot express and
    # `sql_to_mongo.translate` refuses outright ("Unsupported GROUP BY over an
    # expression") — so it is written as a pipeline. `$dateToString` buckets in
    # UTC; `::date` bucketed in the session timezone. They are the same
    # timezone here, checked rather than assumed: over 2026-08-01..08-19 the
    # two produced identical 308 day/tool groups totalling 10,259 rows.
    daily: dict[str, dict] = defaultdict(lambda: defaultdict(int))
    for doc in mongo_store.aggregate("agent_tool_telemetry", [
        {"$match": {"created_at": {"$gt": cut}}},
        {"$group": {
            "_id": {"day": {"$dateToString": {"format": "%Y-%m-%d", "date": "$created_at"}},
                    "tool": "$tool_name"},
            "n": {"$sum": 1},
        }},
    ]):
        daily[doc["_id"]["day"]][normalize(doc["_id"].get("tool") or "")] += doc["n"]

    return calls, runs, daily


def unrecorded_calls(days: int) -> dict:
    """The calls `tool_usage_stats` has that `agent_tool_telemetry` does not.

    Compared per (cycle_id, normalized tool name) and only over the cycle_ids
    BOTH logs carry: the two are written at different seams and cover different
    populations, so a raw total difference would be dominated by cycles one log
    simply never saw (`challenger-*`, `bench-*` on one side, blank cycle_ids on
    the other) rather than by calls that went unattributed.

    Both collections are small (33k and 18k documents all-time) and the
    projection is two fields, so this is a cheap second pass rather than a
    reason to skip the measurement.
    """
    cut = _cutoff(days)

    att: Counter = Counter()
    for d in mongo_store.find_docs(
            "agent_tool_telemetry", {"created_at": {"$gt": cut}},
            projection={"cycle_id": 1, "tool_name": 1, "_id": 0}):
        att[(d.get("cycle_id"), normalize(d.get("tool_name") or ""))] += 1

    tus: Counter = Counter()
    named = 0
    for d in mongo_store.find_docs(
            "tool_usage_stats", {"called_at": {"$gt": cut}},
            projection={"cycle_id": 1, "tool_name": 1, "agent_name": 1, "_id": 0}):
        tus[(d.get("cycle_id"), normalize(d.get("tool_name") or ""))] += 1
        if (d.get("agent_name") or "") not in ("", "unknown"):
            named += 1

    shared = {c for c, _ in att} & {c for c, _ in tus}
    missing: Counter = Counter()
    for (cycle, tool), n in tus.items():
        if cycle in shared:
            gap = n - att.get((cycle, tool), 0)
            if gap > 0:
                missing[tool] += gap

    return {
        "window_days": days,
        "shared_cycle_ids": len(shared),
        "agent_tool_telemetry_calls": sum(n for (c, _), n in att.items() if c in shared),
        "tool_usage_stats_calls": sum(n for (c, _), n in tus.items() if c in shared),
        "unattributable_calls": sum(missing.values()),
        "top_unattributable": missing.most_common(8),
        "tool_usage_stats_rows": sum(tus.values()),
        "tool_usage_stats_rows_naming_an_agent": named,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--compare-days", type=int, default=30,
                    help="Longer window used to separate DEAD from RECENTLY-QUIET")
    ap.add_argument("--json", dest="json_out")
    args = ap.parse_args()

    wl, wl_failed = load_whitelists()
    evidenced = pipeline_cycle_ids()
    calls, runs, daily = fetch(args.days, evidenced)
    long_calls, long_runs, _ = fetch(args.compare_days, evidenced)

    total_calls = sum(c["n"] for a in calls.values() for c in a.values())
    total_long = sum(c["n"] for a in long_calls.values() for c in a.values())

    print("=" * 100)
    print(f"TOOL AUDIT — last {args.days}d (compared against {args.compare_days}d)")
    print("=" * 100)
    print(f"names normalized past {'/'.join(MCP_PREFIXES)}; "
          f"meta-tools shown separately")
    print(f"source: agent_tool_telemetry (MongoDB) — {total_calls} calls in "
          f"{args.days}d, {total_long} in {args.compare_days}d\n")
    if wl_failed:
        print(f"⚠ {len(wl_failed)} agent module(s) did not import and are NOT audited: "
              f"{', '.join(wl_failed)}\n")

    report: dict = {}
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
                      f"zero tool calls is by design for this agent, not a gap)")
            print()
            report[agent] = {"runs": n_runs, "whitelist": sorted(w), "used": {},
                             "never": sorted(w), "off_whitelist": {}, "shadow_calls": 0}
            continue

        used = {t: c for t, c in u.items() if t in w}
        off = {t: c for t, c in u.items() if t and t not in w and t not in META_TOOLS}
        meta = {t: c for t, c in u.items() if t in META_TOOLS}
        blank = u.get("", {}).get("n", 0)

        dead, quiet = [], []
        for t in sorted(w - set(used)):
            (quiet if lu.get(t, {}).get("n", 0) else dead).append(t)

        n_all = sum(c["n"] for c in u.values())
        # Numerator and denominator must cover the same population: only calls
        # made under a cycle_id `v3_agent_telemetry` logged a run for can be
        # divided by that run count. See the module docstring.
        n_shadow = sum(c["shadow"] for c in u.values())
        per_run = ((n_all - n_shadow) / n_runs) if n_runs else 0
        print(f"### {agent}  wl={len(w)} used={len(used)} runs={n_runs} "
              f"calls/run={per_run:.1f}")
        if n_shadow:
            print(f"    ⚠ {n_shadow} of {n_all} calls ran under a cycle_id with no "
                  f"v3_agent_telemetry run (challenger/bench A-B namespaces):")
            print(f"      counted in USED and OFF-WHITELIST, excluded from calls/run, "
                  f"whose denominator does not cover them.")
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
            "shadow_calls": n_shadow,
        }

    # Trend: a tool at zero for several days running is fixed, not broken.
    print("=" * 100)
    print("DAILY TREND — meta-tools and failures (is a 'problem' current or already fixed?)")
    print("=" * 100)
    watch = ["discover_and_enable_tools", "enable_tools", "search_tools", "", "get_sec_filings"]
    print(f"{'date':<12}" + "".join(f"{(w or 'EMPTY'):>28}" for w in watch))
    for day in sorted(daily, reverse=True)[:14]:
        print(f"{day:<12}" + "".join(f"{daily[day].get(w, 0):>28}" for w in watch))

    # The remainder between the two tool logs. Printed, not dropped: everything
    # above is measured against agent_tool_telemetry, and these are the calls
    # that log never saw — so no whitelist check above covers them.
    rem = unrecorded_calls(args.days)
    print()
    print("=" * 100)
    print("CALLS THIS AUDIT CANNOT SEE — the remainder between the two tool logs")
    print("=" * 100)
    print(f"over the {rem['shared_cycle_ids']} cycle_id(s) both logs carry in the last "
          f"{args.days}d:")
    print(f"    agent_tool_telemetry  {rem['agent_tool_telemetry_calls']:6}  (audited above)")
    print(f"    tool_usage_stats      {rem['tool_usage_stats_calls']:6}")
    print(f"    ⚠ {rem['unattributable_calls']} call(s) only tool_usage_stats recorded — "
          f"NOT checked against any whitelist")
    if rem["top_unattributable"]:
        print("      " + ", ".join(f"{t}({n})" for t, n in rem["top_unattributable"]))
    print(f"    and tool_usage_stats cannot attribute them: "
          f"{rem['tool_usage_stats_rows_naming_an_agent']} of "
          f"{rem['tool_usage_stats_rows']} rows in this window name an agent")

    if args.json_out:
        with open(args.json_out, "w") as fh:
            json.dump({**report, "_unaudited": rem}, fh, indent=2)
        print(f"\nwrote {args.json_out}")

    if not total_calls:
        # Every agent printed "NO TOOL CALLS" and every whitelist looked
        # perfectly complied-with, off an empty denominator. That is not a
        # clean audit, it is an audit that did not run.
        print(f"\nNOTHING TO AUDIT: agent_tool_telemetry has no calls in the last "
              f"{args.days}d ({total_long} in {args.compare_days}d). "
              f"Widen --days; the report above measured nothing.")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
