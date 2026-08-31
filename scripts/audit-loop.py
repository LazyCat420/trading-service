#!/usr/bin/env python3
"""Audit the most recent pipeline cycle: agent health, data gaps, tool-domain
breaches and runaway tool loops.

    python scripts/audit-loop.py                # the most recent cycle
    python scripts/audit-loop.py <cycle_id>     # a named cycle

Exit 0 when the audit answered something. Exit 2 when it could not — no cycle
to audit, or a named cycle with neither a desk nor an attributed tool call.
That case used to exit 0 and read exactly like a clean bill of health.

READS MONGODB. Ported off Postgres 2026-08-30; Postgres froze at the
2026-08-19 cutover, so the pre-port script printed 19 August as "the most
recent cycle" and was never wrong-looking about it.

WHAT CHANGED, AND WHY EACH CHANGE WAS NOT OPTIONAL
--------------------------------------------------
1. ATTRIBUTION MOVED TO `agent_tool_telemetry`.
   The domain audit needs to know WHICH AGENT called a tool.
   `tool_usage_stats.agent_name` does not answer that any more: of the 1,824
   rows written since the cutover, 1,815 say 'unknown' and 9 say ''. Zero
   name an agent. The pre-port script matched those names against
   DOMAIN_BOUNDARIES, matched nothing, and printed "All agents stayed within
   their defined tool boundaries" — a green with an empty denominator.
   `app/tools/registry.py` says so outright ("the long-standing
   agent_name='unknown' here is a phantom for this table's actual consumers
   ... Attribution work belongs in agent_tool_telemetry"), and
   HANDOFF_tool_attribution_2026-07-29.md traces the decay week by week
   (100% named in June -> 0.6% by late July). `agent_tool_telemetry` is
   written by the post-call hook INSIDE the agent loop, where the agent name
   is in scope: 142 fully attributed calls for the same cycle where
   `tool_usage_stats` had 117 anonymous ones.

2. THE FROZEN `DOMAIN_BOUNDARIES` DICT IS GONE.
   It was a hand-copied snapshot of three agents' whitelists and it had
   drifted BOTH ways, which is why its count still looked plausible:

       agent                    stale-allowed   missing (would false-alarm)
       v3_junior_analyst              6                    10
       v3_fundamental_analyst         8                    12
       v3_quant_analyst              10                     7

   A stale-allowed entry (`search_web`, `post_finding`, `execute_python`)
   makes a real breach invisible; a missing one (`whiteboard_write`,
   `lazy_web_search`) makes a compliant call read as a breach. It also
   covered 3 of the 20 agents that have a whitelist. Boundaries now resolve
   through `app.agents.tool_whitelists`, the same lookup the harness and the
   Prism registration use, so the audit measures the grant the agent actually
   ran with.

3. MCP PREFIXES ARE STRIPPED BEFORE COMPARING.
   Prism serves these tools as `mcp__lazy-agent-service__get_market_data`.
   102 of the 142 calls in the sample cycle arrive namespaced (all of them
   `mcp__lazy-agent-service__`; the other 40 are bare). Comparing the raw
   name to a whitelist reports every one of those 102 as a breach — the exact
   artifact that produced a false "zero whitelisted tools are used by any
   agent" reading on 2026-07-25 (see app/services/mcp_prefix.py).

4. EVERY DESK IN THE CYCLE IS READ, NOT ONE.
   `shared_desk` holds one row PER TICKER: the sample cycle has 6.
   `fetchone()` audited an arbitrary one of them and silently dropped the
   health and data gaps of the rest.

5. `desk_data` IS PARSED FOR BOTH SHAPES.
   It is JSON TEXT for every desk written since the cutover (`save_desk`
   does `json.dumps`) and an embedded document for the 1,762 written before
   it. A reader that assumes either shape loses the other half — and a Mongo
   projection cannot descend into a string, so the loss is silent.

6. THE ARTIFACT LIST IS DISCOVERED, NOT HARD-CODED.
   The old five-name list predates `valuation_report`, which reports data
   gaps (24 of them across 5 of the 60 most recent desks) that nothing was
   printing. Any desk artifact carrying `data_gaps` is now reported, so the
   next artifact type cannot go silently unaudited.

7. "THE LATEST CYCLE" MUST NAME A PIPELINE CYCLE, NOT ANY FRESH GROUP.
   Two different degenerate groups win on recency and neither is a cycle:

   a. BLANK IDS. 7,122 `tool_usage_stats` rows carry `cycle_id = ''` (tool
      calls made outside a pipeline run) and they are the FRESHEST rows in
      the collection. `GROUP BY cycle_id ORDER BY max(called_at) DESC LIMIT
      1` therefore resolves to '' on live data. Excluded by the `$nin`.

   b. SHADOW NAMESPACES. Merging the second log (see 1) also merges ids that
      `tool_usage_stats` has never carried. `app/v3/challenger.py:76` runs
      the paired-challenger A/B of the decision agent under
      `challenger-<cycle_id>`: 1,096 rows across 136 distinct ids in
      `agent_tool_telemetry`, 0 in `tool_usage_stats`, 0 in `shared_desk`.
      It fires AFTER the champion's decision, on a stripped desk COPY that
      is never saved, so its group is FRESHER than its parent cycle's — in 6
      of the 7 most recent real cycles, and 19 of the 40 newest merged
      groups have no desk at all. `scripts/self_consistency_bench.py:255`
      adds a second family (`sc-<hex>`), and there are `bench-*` ids too.
      Audit one of those and the script prints "No SharedDesk data available
      for this cycle yet.", drops Agent Health and Data Gaps entirely,
      reports "All 1 audited agents stayed within their defined tool
      boundaries." off two calls, and exits 0. Simulated at +10 min after
      each of the 8 most recent cycles, an unguarded merge names a desk-less
      shadow id in 5 of 8 runs.

   So a candidate is resolved in three steps: fold `challenger-X` back onto
   its parent X (the challenger firing IS evidence X just ran, and throwing
   the group away would discard that), then require the id to be EVIDENCED
   as a pipeline cycle — a `pipeline_events` row, a `tool_usage_stats` row
   (the log the SQL used) or a desk — then take the freshest survivor.
   Anything skipped is PRINTED, because "not the audited cycle" has to be
   visible. Measured: after folding, the ONLY ids in `agent_tool_telemetry`
   that no evidence collection carries are `sc-e33ade05`, four `bench-exls-*`
   and `test_cycle` — all harnesses, and `sc-e33ade05` was itself the
   freshest merged group for four minutes on 2026-08-20.

   Note what the rule is NOT: "must have a desk". 188 desk-less `cycle-*`
   ids are in the logs and 185 of them are in `tool_usage_stats`, i.e. the
   original SQL could and did name them; a cycle that is still running has
   not written a desk yet, and "catch running cycles" is what the SQL's own
   comment said it was for. `pipeline_events` is checked first precisely
   because it is the EARLIEST artifact — for the sample cycle its first row
   precedes the first tool call by a minute and the first desk by 33 — so a
   cycle that started seconds ago is auditable rather than skipped.

   Note also what it is not: a list of shadow prefixes. That is a blocklist,
   it drifts the way DOMAIN_BOUNDARIES did (see 2), and the next harness to
   mint a namespace walks through it.

8. AGENT_ERROR IS FLAGGED, NOT ONLY TIMED_OUT.
   'TIMED_OUT' appears in 0 of 8,787 telemetry rows; the live failure
   outcome is 'AGENT_ERROR' (520 rows). Watching only for TIMED_OUT is
   watching a value the pipeline stopped emitting.

9. THE CALLS THE DOMAIN AUDIT COULD NOT SEE ARE COUNTED.
   `agent_tool_telemetry` is where attribution lives (see 1) but it is NOT a
   superset of `tool_usage_stats`: for the sample cycle the dispatch-level
   log holds 17 calls the agent-loop hook never recorded (get_market_data
   x8, lazy_web_search x8, scrape_url x1 — 117 vs 142, and the difference
   runs both ways). A green whose denominator is invisible is the thing this
   port exists to stop, so the remainder is reported rather than dropped.
   Calls with a blank tool name are excluded from the checked count for the
   same reason: `app/v3/tool_telemetry._canary_check` treats an empty tool
   name as a malformed dispatch worth a warning (175 landed on 2026-07-13),
   and `breaches()` already skips them — counting them as "checked" inflates
   the number that makes the green trustworthy.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dotenv import load_dotenv  # noqa: E402

load_dotenv()

from app.db import mongo_query, mongo_store  # noqa: E402
from app.services.mcp_prefix import strip_mcp_prefix  # noqa: E402

# Framework-injected tools. Imported, not re-listed: prism force-adds the
# CORE_AGENTIC set to every custom agent, so `think` / `emit_structured_output`
# are on no whitelist by design and a private copy of the exemption list drifts
# the same way DOMAIN_BOUNDARIES did. The runtime canary in
# app/v3/tool_telemetry.py exempts exactly this set; a post-hoc audit that
# exempts a different one disagrees with the alarm it is supposed to confirm.
from app.v3.tool_telemetry import _FORBIDDEN, _META_TOOLS  # noqa: E402

#: Non-SUCCESS outcomes that mean the agent did not deliver. Checked against
#: the live vocabulary (v3_agent_telemetry, 8,787 rows): SUCCESS 8,239,
#: AGENT_ERROR 520, '?' 24, DATA_GAP 3, SKIPPED 1, TIMED_OUT 0.
FAILED_OUTCOMES = ("TIMED_OUT", "AGENT_ERROR")

#: Tool calls per agent PER RUN above which a loop is suspect. Unchanged from
#: the original — but applied per run rather than per cycle, because a cycle
#: covers N tickers and the same agent runs once per ticker: summing 6 healthy
#: 8-call runs to 48 fires an "infinite loop" alarm on a normal cycle.
LOOP_LIMIT = 15

#: Display names for the desk artifacts, for the ones that have one. Anything
#: else carrying `data_gaps` is printed under its raw key rather than skipped.
ARTIFACT_LABELS = {
    "desk_note": "Junior Analyst",
    "fundamental_report": "Fundamental",
    "quant_report": "Quant",
    "valuation_report": "Valuation",
    "delta_report": "Delta",
    "bull_argument": "Bull",
    "bear_rebuttal": "Bear",
    "bull_defense": "Bull Defense",
}

#: The two tool-call logs, with their timestamp field. Both are written during
#: a run, so either can be the freshest evidence that a cycle is alive — and in
#: the sample cycle the telemetry log was 3 minutes AHEAD of tool_usage_stats.
#: The merge is not cosmetic: 272 desk-bearing cycles logged agent telemetry and
#: NO tool_usage_stats row at all, so a single-log pick can name a stale cycle
#: while a newer one sits in the other log.
CALL_LOGS = (("agent_tool_telemetry", "created_at"), ("tool_usage_stats", "called_at"))

#: `app/v3/challenger.py:76` runs the paired challenger under
#: `challenger-<cycle_id>`. It is the SAME pipeline cycle seen from the A/B
#: harness, so its activity is folded onto the parent id rather than dropped.
CHALLENGER_PREFIX = "challenger-"

#: Groups pulled per log before filtering. `$limit: 1` cannot survive a reject:
#: if the freshest group is a shadow namespace there has to be a next one to
#: fall through to. 25 per log folds to 25 distinct candidates today, spanning
#: 7.3 days — a week of cycles between the freshest group and the oldest.
CANDIDATE_GROUPS = 25

#: Collections that only a real pipeline run writes, cheapest first.
#: `pipeline_events` is the cycle's own event log — indexed on `cycle_id`, and
#: its first row lands 33 minutes before the cycle's first desk and a minute
#: before its first tool call, so a cycle that started seconds ago already
#: qualifies. `tool_usage_stats` is the dispatch-level log the original SQL
#: resolved the cycle from, `shared_desk` the desk the cycle saves; between them
#: they cover the 105 ids `pipeline_events` predates. A shadow namespace appears
#: in NONE of the three: folded back onto their parents, the only ids in
#: `agent_tool_telemetry` that no member evidences are `sc-e33ade05`, four
#: `bench-exls-*` and `test_cycle` — every one of them a harness, not a cycle.
PIPELINE_EVIDENCE = ("pipeline_events", "tool_usage_stats", "shared_desk")


def parent_cycle_id(cycle_id: str) -> str:
    """`challenger-cycle-v3-1` -> `cycle-v3-1`; anything else unchanged.

    The challenger is not a cycle of its own — it is the A/B replica of one
    cycle's decision agent, run on a desk copy that is never saved. Folding it
    onto its parent keeps its timestamp (it is the freshest evidence the parent
    just finished) without ever naming it as the thing to audit. Looped rather
    than stripped once so a `challenger-challenger-` id cannot slip through.
    """
    while cycle_id.startswith(CHALLENGER_PREFIX):
        cycle_id = cycle_id[len(CHALLENGER_PREFIX):]
    return cycle_id


def is_pipeline_cycle(cycle_id: str) -> bool:
    """True when some pipeline artifact bears this cycle id.

    Deliberately NOT "has a desk": a running cycle writes its events and its
    tool calls long before it saves one, and 185 of the 188 desk-less `cycle-*`
    ids in the logs are in `tool_usage_stats`, i.e. the original SQL named them
    and printed "No SharedDesk data available for this cycle yet." This only
    rejects ids that no cycle event, no dispatch log and no desk has ever
    recorded — the shadow namespaces (`challenger-*` before folding, `sc-*`,
    `bench-*`).

    An allowlist by EVIDENCE, not a blocklist by prefix: a private list of
    shadow prefixes drifts exactly the way DOMAIN_BOUNDARIES did, and the next
    harness to invent a namespace would walk straight through it.
    """
    if not cycle_id:
        return False
    return any(mongo_query.exists(c, {"cycle_id": cycle_id})
               for c in PIPELINE_EVIDENCE)


def cycle_candidates(limit: int = CANDIDATE_GROUPS) -> list[tuple[str, datetime, str]]:
    """(cycle_id, last_activity, source) for both logs, freshest first.

    `mongo_query.group_rows` can express the aggregate, but ORDER BY on a
    grouped query has to name the aggregate's INTERNAL output key (`a0`). If
    that name ever changes, the sort degrades to a no-op on a missing field —
    Mongo does not error, it just returns an arbitrary group, and this would
    pick an arbitrary cycle while looking exactly as correct as it does now. A
    raw pipeline names the field itself.
    """
    best: dict[str, tuple[datetime, str]] = {}
    for collection, ts_field in CALL_LOGS:
        rows = mongo_store.aggregate(collection, [
            # `cycle_id IS NOT NULL AND cycle_id <> ''`. $nin also excludes
            # documents with no cycle_id at all, which is the same
            # "unattributed" the SQL meant. Without it the '' group — tool
            # calls made outside any pipeline run — wins on recency and the
            # whole audit reports on a cycle that never ran.
            {"$match": {"cycle_id": {"$nin": [None, ""]}}},
            {"$group": {"_id": "$cycle_id", "last_activity": {"$max": f"${ts_field}"}}},
            {"$sort": {"last_activity": -1}},
            {"$limit": limit},
        ])
        for row in rows:
            when = row.get("last_activity")
            if when is None:
                continue
            cycle_id = parent_cycle_id(row["_id"])
            if cycle_id not in best or when > best[cycle_id][0]:
                best[cycle_id] = (when, collection)
    return sorted(((c, w, s) for c, (w, s) in best.items()),
                  key=lambda cand: cand[1], reverse=True)


def latest_cycle() -> tuple[str, datetime, str] | None:
    """The most recent PIPELINE cycle that logged a tool call.

    (cycle_id, when, source), or None if no candidate group names one. Groups
    that are not pipeline cycles are skipped OUT LOUD: silently auditing a
    shadow A/B namespace is how this printed a clean bill of health for a run
    that has no desk, no agent health and no data gaps to report.
    """
    for cycle_id, when, source in cycle_candidates():
        if is_pipeline_cycle(cycle_id):
            return cycle_id, when, source
        print(f" (skipping {cycle_id!r}, active {when} — not a pipeline cycle: "
              f"no row in {', '.join(PIPELINE_EVIDENCE)} carries that id)")
    return None


def desk_data(raw) -> dict:
    """`shared_desk.desk_data` as a dict, whichever shape it is stored in.

    TEXT since the cutover (`save_desk` writes `json.dumps(...)`), an embedded
    document for every desk written before it. 274 of the 2,036 desks are text
    today and that share only grows.
    """
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except Exception:  # noqa: BLE001 — one corrupt desk is not an outage
            return {}
    return raw or {}


def load_desks(cycle_id: str) -> list[tuple[str, str, dict]]:
    """Every desk in the cycle as (ticker, phase, desk_data)."""
    rows = mongo_query.find_rows(
        "shared_desk", {"cycle_id": cycle_id},
        ["ticker", "phase", "desk_data"], sort=[("created_at", 1)])
    return [(t or "?", p or "?", desk_data(d)) for t, p, d in rows]


def load_tool_calls(cycle_id: str) -> list[tuple[str, str, str, bool, str]]:
    """Attributed tool calls for the cycle, tool names already un-namespaced.

    (agent_name, bare_tool_name, ticker, was_blocked, error_message)
    """
    rows = mongo_query.find_rows(
        "agent_tool_telemetry", {"cycle_id": cycle_id},
        ["agent_name", "tool_name", "ticker", "was_blocked", "error_message"])
    return [(a or "", strip_mcp_prefix(t or ""), tk or "?", bool(b), e or "")
            for a, t, tk, b, e in rows]


def load_dispatch_calls(cycle_id: str) -> list[str]:
    """Bare tool names from `tool_usage_stats` — the dispatch-level log.

    Not attribution (`agent_name` is 'unknown' on every row written since the
    cutover, which is why the domain audit reads the telemetry log instead),
    but it IS the count of what actually ran. The two logs are not nested:
    117 dispatch rows vs 142 telemetry rows for the sample cycle, differing in
    both directions.
    """
    rows = mongo_query.find_rows(
        "tool_usage_stats", {"cycle_id": cycle_id}, ["tool_name"])
    return [strip_mcp_prefix(t or "") for (t,) in rows]


def unattributed(dispatched: list[str], calls) -> Counter[str]:
    """Dispatch-level calls with no attributed counterpart, by tool name.

    A multiset difference, so eight `get_market_data` dispatches against six
    attributed ones leaves two — the point is how many calls the domain audit
    could not see, not which tool names are missing entirely.
    """
    return Counter(dispatched) - Counter(t for _a, t, _tk, _b, _e in calls)


def domain_boundaries(agents) -> dict[str, set[str]]:
    """{agent: the tools it was granted}, for the agents that HAVE a whitelist.

    An agent with no whitelist on record is left OUT rather than given an empty
    one: `_resolve_tool_names` answers [] for an unknown agent, and auditing
    against that turns every call it made into a breach. The caller reports the
    skipped names, so "no breaches" always arrives with its denominator.
    """
    from app.agents.tool_whitelists import (
        AGENT_TOOL_WHITELISTS, get_agent_enabled_tool_names,
    )

    out: dict[str, set[str]] = {}
    for agent in agents:
        # Membership first: calling the resolver for an unknown name logs an
        # ERROR by design. A persona-store override for an agent that IS in the
        # dict still wins, because the resolver checks the store first.
        if agent not in AGENT_TOOL_WHITELISTS:
            continue
        try:
            granted = get_agent_enabled_tool_names(agent)
        except Exception as e:  # noqa: BLE001
            print(f"   [!] could not resolve boundaries for {agent}: {e}")
            continue
        out[agent] = {strip_mcp_prefix(t) for t in granted}
    return out


def breaches(calls, boundaries) -> list[tuple[str, str, int, int, bool]]:
    """Off-whitelist (agent, tool) pairs: (agent, tool, calls, blocked, forbidden).

    `blocked` counts the ones stopped before they dispatched — an ATTEMPT that
    a DENY policy refused is drift in the prompt, not a breach of the sandbox,
    and reporting the two as one number is how 14 held denials were once filed
    as 14 policy failures.
    """
    seen: dict[tuple[str, str], list[int]] = defaultdict(lambda: [0, 0])
    for agent, tool, _ticker, was_blocked, error in calls:
        if agent not in boundaries or not tool:
            continue
        if tool in _META_TOOLS or tool in boundaries[agent]:
            continue
        cell = seen[(agent, tool)]
        cell[0] += 1
        if was_blocked or "POLICY_DENIED" in error:
            cell[1] += 1
    return sorted(
        (a, t, n, blocked, t in _FORBIDDEN)
        for (a, t), (n, blocked) in seen.items()
    )


def loop_stats(calls) -> list[tuple[str, int, int, str, int]]:
    """Per agent: (agent, total calls, runs, busiest ticker, calls in that run).

    Grouped by TICKER because one ticker is one agent run: the pipeline runs
    each research agent once per ticker in the cycle, so a cycle-wide sum grows
    with the watchlist. Six healthy 8-call runs sum to 48 and trip a limit of
    15 that was written to catch a single agent looping forever.
    """
    per_run: Counter[tuple[str, str]] = Counter()
    for agent, _tool, ticker, _blocked, _error in calls:
        if agent:
            per_run[(agent, ticker)] += 1
    out = []
    for agent in sorted({a for a, _t in per_run}):
        runs = {t: n for (a, t), n in per_run.items() if a == agent}
        worst_ticker, worst = max(runs.items(), key=lambda kv: kv[1])
        out.append((agent, sum(runs.values()), len(runs), worst_ticker, worst))
    return out


def audit_latest_cycle(cycle_id_override: str | None = None) -> int:
    print(f"\n[{datetime.now(timezone.utc).isoformat()}] Starting Pipeline Audit")
    print("=" * 60)

    # 1. Which cycle
    named_a_cycle = True
    if cycle_id_override:
        cycle_id = cycle_id_override
        print(f"Cycle ID (override): {cycle_id}")
        # An override skips the resolver, so it needs the same guard: pointing
        # this at `challenger-<id>` by hand produced a two-section run that
        # exited 0 and read as a clean bill of health.
        named_a_cycle = is_pipeline_cycle(cycle_id)
        if not named_a_cycle:
            folded = parent_cycle_id(cycle_id)
            print(f"   [!] {cycle_id!r} is NOT a pipeline cycle — no row in "
                  f"{', '.join(PIPELINE_EVIDENCE)} carries that id. Whatever "
                  f"prints below is a fragment, not an audit of a cycle.")
            if folded != cycle_id and is_pipeline_cycle(folded):
                print(f"   [!] It is the paired-challenger A/B namespace of "
                      f"{folded!r} (app/v3/challenger.py) — audit that instead.")
    else:
        found = latest_cycle()
        if not found:
            print("No pipeline cycles found in the tool-call logs.")
            return 2
        cycle_id, last_activity, source = found
        print(f"Cycle ID: {cycle_id}")
        print(f"Last Activity: {last_activity} (from {source})")

    # 2. Every desk in the cycle: agent health, then data gaps
    desks = load_desks(cycle_id)
    if not desks:
        print("\nNo SharedDesk data available for this cycle yet.")
    else:
        print(f"\n--- Agent Health & Timeouts --- ({len(desks)} desk(s))")
        outcomes: Counter[str] = Counter()
        runs = 0
        for ticker, _phase, desk in desks:
            telemetry = desk.get("agent_telemetry") or []
            if not telemetry:
                print(f" - [{ticker}] No agent telemetry found.")
                continue
            for entry in telemetry:
                agent = entry.get("agent_name")
                outcome = entry.get("outcome")
                ms = entry.get("elapsed_ms") or 0
                runs += 1
                outcomes[outcome or "?"] += 1
                print(f" - [{ticker}] {agent}: {outcome} ({ms / 1000:.1f}s)")
                if outcome == "TIMED_OUT":
                    print(f"   [!] CRITICAL: {agent} timed out!")
                elif outcome in FAILED_OUTCOMES:
                    print(f"   [!] CRITICAL: {agent} did not complete ({outcome})")
        # The denominator, so a quiet section cannot be read as a healthy one.
        print(f" {runs} agent run(s): "
              + ", ".join(f"{n} {o}" for o, n in outcomes.most_common()))

        print("\n--- Data Gaps ---")
        total_gaps = 0
        for ticker, _phase, desk in desks:
            # Labelled artifacts first, in their documented order (the order
            # the pre-port script printed), then anything else the desk
            # carries — so a new artifact type appears at the end instead of
            # not at all, and the output stays stable between runs.
            keys = [k for k in ARTIFACT_LABELS if k in desk]
            keys += [k for k in desk if k not in ARTIFACT_LABELS]
            for key in keys:
                artifact = desk.get(key)
                if not isinstance(artifact, dict):
                    continue
                gaps = artifact.get("data_gaps") or []
                if not gaps:
                    continue
                total_gaps += len(gaps)
                label = ARTIFACT_LABELS.get(key, key)
                print(f" - [{ticker}] {label} reported {len(gaps)} data gap(s):")
                for gap in gaps:
                    print(f"     > {gap}")
        if total_gaps == 0:
            print(" No data gaps reported by any desk artifact.")

    # 3. Tool domain auditing
    print("\n--- Tool Domain Auditing ---")
    calls = load_tool_calls(cycle_id)
    if not calls:
        # NOT "compliant". No attributed call was recorded for this cycle, so
        # the question was not answered — say which, or the next reader will
        # take the silence for a pass.
        print("   [!] No attributed tool calls recorded for this cycle "
              "(agent_tool_telemetry) — nothing to audit.")
        boundaries: dict[str, set[str]] = {}
    else:
        agents = {a for a, _t, _tk, _b, _e in calls if a}
        boundaries = domain_boundaries(agents)
        violations = breaches(calls, boundaries)
        for agent, tool, count, blocked, forbidden in violations:
            kind = "FORBIDDEN TOOL" if forbidden else "BOUNDARY BREACH"
            note = f", {blocked} blocked before dispatch" if blocked else ""
            print(f"   [!] {kind}: {agent} called '{tool}' ({count} times{note})")
        # `breaches()` skips a blank tool name, so counting one as "checked"
        # inflates the denominator that makes the green trustworthy. The runtime
        # canary treats an empty name as a malformed dispatch, so it is reported
        # rather than dropped.
        audited = sum(1 for a, t, _tk, _b, _e in calls if a in boundaries and t)
        nameless = sum(1 for a, t, _tk, _b, _e in calls if a in boundaries and not t)
        meta = sum(1 for a, t, _tk, _b, _e in calls
                   if a in boundaries and t in _META_TOOLS)
        if not violations:
            print(f"All {len(boundaries)} audited agents stayed within their "
                  f"defined tool boundaries.")
        print(f" {audited} attributed call(s) checked against "
              f"{len(boundaries)} whitelist(s); {meta} meta-tool call(s) exempt.")
        if nameless:
            print(f"   [!] {nameless} attributed call(s) carry an EMPTY tool "
                  f"name — malformed dispatch, not checked against any "
                  f"whitelist.")
        unaudited = sorted(agents - set(boundaries))
        if unaudited:
            print(f" NOT audited — no whitelist on record: {', '.join(unaudited)}")

    # The other log's denominator. agent_tool_telemetry is where attribution
    # lives, but it is not a superset of tool_usage_stats: a tool call that
    # never reaches the agent-loop hook is out of the domain audit's scope, and
    # this is the difference between out of scope VISIBLY and out of scope
    # silently.
    dispatched = load_dispatch_calls(cycle_id)
    missing = unattributed(dispatched, calls)
    if missing:
        detail = ", ".join(f"{t or '(no tool name)'} x{n}"
                           for t, n in missing.most_common())
        print(f" {len(dispatched)} dispatch-level call(s) logged "
              f"(tool_usage_stats); {sum(missing.values())} with no attributed "
              f"counterpart, so OUT OF SCOPE above: {detail}")
    elif dispatched:
        print(f" {len(dispatched)} dispatch-level call(s) logged "
              f"(tool_usage_stats); every one has an attributed counterpart.")

    # 4. Loop constraints. Per agent PER TICKER: one ticker is one agent run,
    # and an infinite loop happens inside a run, not across a cycle.
    print("\n--- Loop Constraints ---")
    stats = loop_stats(calls)
    if not stats:
        print(" No attributed tool calls to measure.")
    for agent, total, n_runs, worst_ticker, worst in stats:
        if worst > LOOP_LIMIT:
            print(f"   [!] EXCESSIVE LOOPS: {agent} made {worst} tool calls "
                  f"in one run ({worst_ticker})!")
        else:
            print(f" - {agent}: {total} tool calls over {n_runs} run(s), "
                  f"max {worst} per run (within limits)")

    print("=" * 60)
    # An audit with no desk AND no attributed call answered nothing — and so
    # did an "audit" of an id that names no pipeline run, however many tool
    # calls its shadow namespace logged. Exiting 0 in either case makes "I
    # could not read anything" indistinguishable from "I found nothing wrong",
    # which is the failure this whole port exists to catch.
    if not named_a_cycle:
        return 2
    return 0 if (desks or calls) else 2


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Audit the most recent pipeline cycle (reads MongoDB).")
    parser.add_argument(
        "cycle_id", nargs="?", default=None,
        help="audit this cycle instead of the most recent one")
    args = parser.parse_args()
    return audit_latest_cycle(args.cycle_id)


if __name__ == "__main__":
    sys.exit(main())
