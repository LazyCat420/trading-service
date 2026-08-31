"""`scripts/audit-loop.py` audits the LIVE cycle against the LIVE whitelists.

Four separate ways this script answered nothing while printing a clean bill of
health. All four measured on 2026-08-30, against both stores:

1. **It read the frozen archive.** `psycopg.connect(DATABASE_URL)` against a
   Postgres that stopped being written at the 2026-08-19 cutover. Run today it
   announced `Cycle ID: cycle-v3-1787179210 / Last Activity: 2026-08-19
   22:44:57` — eleven days stale, and nothing about the output said so.

2. **It matched agents against a column that no longer names them.** Of the
   1,824 `tool_usage_stats` rows written since the cutover, 1,815 carry
   `agent_name='unknown'` and 9 carry `''`. Zero name an agent, so
   `if agent in DOMAIN_BOUNDARIES` matched nothing and the audit printed "All
   agents stayed within their defined tool boundaries" with an empty
   denominator. `app/tools/registry.py` already documents that column as a
   phantom and points at `agent_tool_telemetry`, which had 142 fully
   attributed calls for the same cycle.

3. **Its boundaries were a hand-copied snapshot that had drifted BOTH ways.**
   For the three agents it covered: 24 tools listed as allowed that the live
   whitelists no longer grant (each one hides a real breach — `search_web`,
   the one genuine breach in the audited cycle, was among them), and 29 live
   grants missing from the copy (each one invents a breach). The count still
   looked plausible, which is why nobody caught it.

4. **It compared namespaced tool names to bare whitelist entries.** 102 of the
   142 calls in that cycle arrive as `mcp__lazy-agent-service__*` (the other
   40 are bare). Comparing raw would report all 102 as breaches — the artifact
   that produced a false "zero whitelisted tools are used by any agent"
   reading on 2026-07-25.

And one the FIRST port introduced, which is why the latest-cycle tests below
read the way they do:

5. **Merging the second log merged a namespace that is not a cycle.**
   `app/v3/challenger.py:76` runs the paired-challenger A/B under
   `challenger-<cycle_id>`: 1,096 rows over 136 ids in `agent_tool_telemetry`,
   0 in `tool_usage_stats`, 0 in `shared_desk`, and it fires AFTER the
   champion's decision, so its group is the FRESHEST. Simulated at +10 min
   after each of the 40 most recent cycles, the unguarded merge named a
   desk-less shadow id 27 times (the original SQL 3, the fixed resolver 0),
   and the script then printed a two-section clean bill of health and exited
   0. The first version of this suite asserted that behaviour was correct.

Each test below fails against `git show 77e6dc3:scripts/audit-loop.py`; the four
latest-cycle tests also fail against the first port.
"""
from __future__ import annotations

import importlib.util
import pathlib
import re
from unittest.mock import patch

import pytest

REPO = pathlib.Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts" / "audit-loop.py"

# The dict that used to live in the script. A historical constant, kept so the
# tests can assert the boundaries are no longer this.
FROZEN_2026_07 = {
    "v3_junior_analyst": {
        "get_finnhub_news", "search_web", "get_market_data",
        "search_internal_database", "post_finding", "create_team", "scrape_url",
        "read_url", "emit_structured_output",
    },
}


@pytest.fixture(scope="module")
def audit():
    """The script, loaded by path — its filename has a hyphen in it.

    `load_dotenv` is stubbed out: importing a script must not push the
    production `.env` into the test process's environment.
    """
    spec = importlib.util.spec_from_file_location("audit_loop_under_test", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    with patch("dotenv.load_dotenv"):
        spec.loader.exec_module(mod)
    return mod


def test_the_script_has_no_postgres_coupling():
    """The archive answers, so an import left behind does not fail — it lies."""
    src = SCRIPT.read_text(encoding="utf-8")
    hits = [
        f"{i}: {line.strip()}"
        for i, line in enumerate(src.splitlines(), 1)
        if re.search(r"psycopg|DATABASE_URL|pg_connection|dbname=", line)
        and "Postgres" not in line
    ]
    assert not hits, hits


def test_boundaries_resolve_through_the_live_whitelist_module(audit):
    """MECHANISM: the grant comes from `app.agents.tool_whitelists`, at run
    time. Stubbed so the test keeps passing when a whitelist is legitimately
    edited — what is pinned is where the answer comes from, not its content."""
    import app.agents.tool_whitelists as tw

    granted = ["mcp__lazy-agent-service__get_market_data", "whiteboard_read"]
    with patch.dict(tw.AGENT_TOOL_WHITELISTS, {"v3_probe_agent": granted}, clear=False), \
         patch.object(tw, "get_agent_enabled_tool_names", return_value=granted):
        resolved = audit.domain_boundaries({"v3_probe_agent"})

    # Namespaced grants come back bare, or nothing an agent actually called
    # would ever match them.
    assert resolved == {"v3_probe_agent": {"get_market_data", "whiteboard_read"}}


def test_boundaries_are_not_the_frozen_copy_and_cover_every_whitelisted_agent(audit):
    """CONTENT: resolved from the live module, so the drift is gone and the
    coverage is every agent that has a whitelist rather than three."""
    from app.agents.tool_whitelists import AGENT_TOOL_WHITELISTS

    everyone = set(AGENT_TOOL_WHITELISTS)
    resolved = audit.domain_boundaries(everyone)

    assert set(resolved) == everyone, (
        "every agent with a whitelist must be audited; the frozen dict covered "
        f"3 of {len(everyone)}"
    )
    junior = resolved["v3_junior_analyst"]
    stale = FROZEN_2026_07["v3_junior_analyst"] - junior
    invented = junior - FROZEN_2026_07["v3_junior_analyst"]
    assert junior != FROZEN_2026_07["v3_junior_analyst"], (
        "the boundaries are the frozen 2026-07 snapshot again"
    )
    assert stale and invented, (
        "the frozen copy drifted in BOTH directions and this pins that it is "
        f"no longer in use (stale-allowed={sorted(stale)}, missing={sorted(invented)})"
    )


def test_an_agent_with_no_whitelist_is_reported_not_convicted(audit):
    """`_resolve_tool_names` answers [] for an unknown agent. Auditing against
    an empty grant turns every call it made into a breach."""
    resolved = audit.domain_boundaries({"contradiction_shadow", "not_an_agent"})
    assert resolved == {}


def test_namespaced_and_framework_tools_are_not_breaches(audit):
    """A prism-namespaced call to a granted tool is compliant; so is a
    framework-injected meta tool, which is on no whitelist by design."""
    boundaries = {"v3_junior_analyst": {"get_market_data"}}
    calls = [
        ("v3_junior_analyst", "get_market_data", "AAPL", False, ""),
        # strip_mcp_prefix runs in load_tool_calls, so this is what breaches()
        # sees for `mcp__lazy-agent-service__get_market_data`.
        ("v3_junior_analyst", "get_market_data", "MSFT", False, ""),
        ("v3_junior_analyst", "think", "AAPL", False, ""),
        ("v3_junior_analyst", "emit_structured_output", "AAPL", False, ""),
    ]
    assert audit.breaches(calls, boundaries) == []


def test_a_real_breach_is_counted_and_a_blocked_attempt_is_marked(audit):
    boundaries = {"v3_junior_analyst": {"get_market_data"}}
    calls = [
        ("v3_junior_analyst", "search_web", "AAPL", False, ""),
        ("v3_junior_analyst", "search_web", "MSFT", False, ""),
        ("v3_junior_analyst", "write_file", "AAPL", False, "POLICY_DENIED"),
        # An agent with no whitelist on record is not audited, so its calls
        # are not breaches.
        ("contradiction_shadow", "search_web", "AAPL", False, ""),
    ]
    got = audit.breaches(calls, boundaries)
    assert ("v3_junior_analyst", "search_web", 2, 0, False) in got
    assert ("v3_junior_analyst", "write_file", 1, 1, True) in got, (
        "a forbidden tool must be labelled as such, and a denial that HELD "
        "must not be filed as an executed breach"
    )
    assert len(got) == 2, got


def test_mcp_prefixes_are_stripped_when_calls_are_loaded(audit):
    """The strip happens at the read seam, so every consumer sees bare names."""
    from app.db import mongo_query

    rows = [("v3_junior_analyst", "mcp__lazy-agent-service__get_market_data",
             "AAPL", False, "")]
    with patch.object(mongo_query, "find_rows", return_value=rows) as fr:
        calls = audit.load_tool_calls("cycle-1")

    assert fr.call_args[0][0] == "agent_tool_telemetry", (
        "attribution lives in agent_tool_telemetry; tool_usage_stats.agent_name "
        "is 'unknown' on every row written since the cutover"
    )
    assert calls == [("v3_junior_analyst", "get_market_data", "AAPL", False, "")]


def test_desk_data_is_parsed_whether_it_is_text_or_a_document(audit):
    """Text since the cutover, an embedded document before it. The pre-port
    code called `.get()` on whatever came back, which is an AttributeError on
    the 274 text-shaped desks."""
    doc = {"agent_telemetry": [{"agent_name": "v3_junior_analyst"}]}
    assert audit.desk_data(doc) == doc
    assert audit.desk_data('{"agent_telemetry": [{"agent_name": "v3_junior_analyst"}]}') == doc
    assert audit.desk_data(None) == {}
    assert audit.desk_data("not json") == {}


def _fake_logs(groups, evidenced):
    """(aggregate, exists, seen) driving `latest_cycle` off `groups`.

    `groups` is {collection: [(cycle_id, last_activity), ...]}; `evidenced` is
    the set of ids some pipeline collection carries. `seen` records the
    pipelines so the shape of the query can be asserted too.
    """
    seen: dict[str, list] = {}

    def fake_aggregate(collection, pipeline, **_kw):
        seen[collection] = pipeline
        return [{"_id": cid, "last_activity": when}
                for cid, when in groups.get(collection, [])]

    def fake_exists(_collection, query):
        return query["cycle_id"] in evidenced

    return fake_aggregate, fake_exists, seen


def test_latest_cycle_skips_unattributed_rows(audit):
    """7,122 `tool_usage_stats` rows carry `cycle_id=''` and they are the
    NEWEST rows in the collection, so an unfiltered `GROUP BY cycle_id ORDER BY
    max(called_at) DESC LIMIT 1` resolves to '' and every later section reports
    on a cycle that never ran."""
    from datetime import datetime

    from app.db import mongo_query, mongo_store

    agg, ex, seen = _fake_logs(
        {"agent_tool_telemetry": [("cycle-NEW", datetime(2026, 8, 27, 4, 58))],
         "tool_usage_stats": [("cycle-OLD", datetime(2026, 8, 27, 4, 55))]},
        evidenced={"cycle-NEW", "cycle-OLD"},
    )
    with patch.object(mongo_store, "aggregate", side_effect=agg), \
         patch.object(mongo_query, "exists", side_effect=ex):
        got = audit.latest_cycle()

    # Both logs are read: 272 desk-bearing cycles wrote agent telemetry and no
    # tool_usage_stats row at all, so a single-log pick names a stale cycle.
    assert set(seen) == {"agent_tool_telemetry", "tool_usage_stats"}
    assert got == ("cycle-NEW", datetime(2026, 8, 27, 4, 58), "agent_tool_telemetry")
    for collection, pipeline in seen.items():
        assert pipeline[0]["$match"] == {"cycle_id": {"$nin": [None, ""]}}, collection
        # The sort must name a field the pipeline itself creates. Sorting on a
        # name nothing produces is not an error in Mongo — it returns an
        # arbitrary group, i.e. an arbitrary cycle, silently.
        sort_key = next(iter(pipeline[2]["$sort"]))
        assert sort_key in pipeline[1]["$group"], (collection, pipeline)
        # More than one group per log, or a rejected candidate has nothing to
        # fall through to and `latest_cycle` answers None on a live database.
        assert pipeline[3]["$limit"] > 1, (collection, pipeline)


def test_a_fresher_challenger_group_does_not_become_the_audited_cycle(audit):
    """THE REGRESSION THIS SUITE ONCE PINNED.

    `agent_tool_telemetry` carries `challenger-<cycle_id>`
    (app/v3/challenger.py:76) and `tool_usage_stats` and `shared_desk` carry 0
    such rows. The challenger runs AFTER the champion's decision, on a desk
    copy that is never saved, so its group is FRESHER than its parent's — in 6
    of the 7 most recent real cycles. Audit it and the script has no desk, no
    agent health and no data gaps to report, and says so by printing a
    two-section green.

    The freshest group here is the challenger, and the answer must still be the
    parent cycle, carrying the challenger's timestamp (it IS evidence the
    parent just finished)."""
    from datetime import datetime

    from app.db import mongo_query, mongo_store

    fresh, parent = datetime(2026, 8, 27, 1, 33, 7), datetime(2026, 8, 27, 1, 33, 6)
    agg, ex, _seen = _fake_logs(
        {"agent_tool_telemetry": [("challenger-cycle-X", fresh)],
         "tool_usage_stats": [("cycle-X", parent)]},
        evidenced={"cycle-X"},          # the challenger id is in nothing
    )
    with patch.object(mongo_store, "aggregate", side_effect=agg), \
         patch.object(mongo_query, "exists", side_effect=ex):
        got = audit.latest_cycle()

    assert got[0] == "cycle-X", (
        "the paired-challenger A/B namespace is not a pipeline cycle; auditing "
        "it drops Agent Health and Data Gaps and still exits 0"
    )
    assert got[1] == fresh, (
        "the challenger firing is the freshest evidence its parent cycle ran — "
        "fold the group onto the parent, do not discard it"
    )


def test_a_shadow_namespace_with_no_parent_is_skipped_out_loud(audit, capsys):
    """`scripts/self_consistency_bench.py:255` mints `sc-<hex>` and there are
    `bench-exls-*` ids too — no prefix to fold, so the resolver has to fall
    through to the next candidate. `sc-e33ade05` really was the freshest merged
    group for four minutes on 2026-08-20."""
    from datetime import datetime

    from app.db import mongo_query, mongo_store

    agg, ex, _seen = _fake_logs(
        {"agent_tool_telemetry": [("sc-e33ade05", datetime(2026, 8, 20, 7, 34)),
                                  ("cycle-real", datetime(2026, 8, 20, 7, 30))],
         "tool_usage_stats": [("cycle-real", datetime(2026, 8, 20, 7, 29))]},
        evidenced={"cycle-real"},
    )
    with patch.object(mongo_store, "aggregate", side_effect=agg), \
         patch.object(mongo_query, "exists", side_effect=ex):
        got = audit.latest_cycle()

    assert got[0] == "cycle-real"
    out = capsys.readouterr().out
    assert "sc-e33ade05" in out and "not a pipeline cycle" in out, (
        "a skipped candidate has to be visible: 'not the audited cycle' that "
        "nothing prints is how the wrong cycle got audited in the first place"
    )


def test_a_cycle_is_evidenced_not_pattern_matched(audit):
    """`is_pipeline_cycle` asks the database, in a fixed order, and stops at
    the first hit. An allowlist by evidence, not a blocklist by prefix — a
    private list of shadow prefixes drifts exactly the way DOMAIN_BOUNDARIES
    did, and the next harness to mint a namespace walks straight through it."""
    from app.db import mongo_query

    assert audit.PIPELINE_EVIDENCE[0] == "pipeline_events", (
        "the cycle's own event log is indexed on cycle_id and lands a minute "
        "before the first tool call and 33 minutes before the first desk — a "
        "cycle that started seconds ago must still be auditable"
    )
    assert "shared_desk" in audit.PIPELINE_EVIDENCE
    assert "tool_usage_stats" in audit.PIPELINE_EVIDENCE, (
        "the log the original SQL resolved the cycle from"
    )

    asked = []

    def fake_exists(collection, query):
        asked.append(collection)
        return collection == "shared_desk"

    with patch.object(mongo_query, "exists", side_effect=fake_exists):
        assert audit.is_pipeline_cycle("cycle-X") is True
        assert asked == list(audit.PIPELINE_EVIDENCE[:asked.index("shared_desk") + 1])
        assert audit.is_pipeline_cycle("") is False, "no query for an empty id"

    with patch.object(mongo_query, "exists", return_value=False):
        assert audit.is_pipeline_cycle("challenger-cycle-X") is False


def test_the_challenger_alias_folds_onto_its_parent(audit):
    assert audit.parent_cycle_id("challenger-cycle-v3-1") == "cycle-v3-1"
    assert audit.parent_cycle_id("cycle-v3-1") == "cycle-v3-1"
    assert audit.parent_cycle_id("sc-e33ade05") == "sc-e33ade05"
    assert audit.parent_cycle_id("challenger-challenger-cycle-v3-1") == "cycle-v3-1"


def test_an_override_naming_a_shadow_namespace_warns_and_exits_nonzero(audit, capsys):
    """`audit-loop.py challenger-cycle-v3-1787793608` used to print
    "All 1 audited agents stayed within their defined tool boundaries." off two
    calls, omit Agent Health and Data Gaps entirely, and EXIT 0. The exit-2 net
    could not fire because `calls` was non-empty."""
    from app.db import mongo_query

    calls = [("v3_decision_synthesizer", "think", "AAPL", False, "")]
    with patch.object(mongo_query, "exists", return_value=False), \
         patch.object(audit, "load_desks", return_value=[]), \
         patch.object(audit, "load_tool_calls", return_value=calls), \
         patch.object(audit, "load_dispatch_calls", return_value=[]):
        rc = audit.audit_latest_cycle("challenger-cycle-v3-1787793608")

    out = capsys.readouterr().out
    assert rc == 2, "a fragment of a shadow namespace is not a clean audit"
    assert "is NOT a pipeline cycle" in out
    assert "not an audit of a cycle" in out


def test_the_loop_limit_is_per_run_not_per_cycle(audit):
    """One ticker is one agent run. Six healthy 8-call runs sum to 48 and would
    trip a cycle-wide limit of 15 on a perfectly normal cycle — the live cycle
    has v3_junior_analyst at 41 calls over 6 runs, max 8."""
    healthy = [("v3_junior_analyst", "get_market_data", t, False, "")
               for t in "ABCDEF" for _ in range(8)]
    assert audit.loop_stats(healthy) == [("v3_junior_analyst", 48, 6, "A", 8)]
    assert 8 <= audit.LOOP_LIMIT < 48

    looping = healthy + [("v3_quant_analyst", "run_equation", "A", False, "")] * 16
    stats = dict((a, worst) for a, _tot, _runs, _t, worst in audit.loop_stats(looping))
    assert stats["v3_quant_analyst"] > audit.LOOP_LIMIT
    assert stats["v3_junior_analyst"] <= audit.LOOP_LIMIT


def test_a_cycle_that_cannot_be_resolved_exits_nonzero(audit, capsys):
    """An audit that answered nothing used to exit 0, which is indistinguishable
    from an audit that found nothing wrong."""
    with patch.object(audit, "latest_cycle", return_value=None):
        rc = audit.audit_latest_cycle(None)
    out = capsys.readouterr().out
    assert rc == 2
    assert "No pipeline cycles found" in out
    assert "stayed within" not in out


def test_a_cycle_with_nothing_in_it_exits_nonzero_too(audit, capsys):
    """A named cycle with no desk and no attributed call answered nothing
    either — and a typo'd cycle id is the easy way to get there."""
    from app.db import mongo_query

    with patch.object(mongo_query, "exists", return_value=True), \
         patch.object(audit, "load_desks", return_value=[]), \
         patch.object(audit, "load_tool_calls", return_value=[]), \
         patch.object(audit, "load_dispatch_calls", return_value=[]):
        rc = audit.audit_latest_cycle("cycle-typo")
    out = capsys.readouterr().out
    assert rc == 2
    assert "nothing to audit" in out
    assert "stayed within" not in out


def test_the_dispatch_calls_the_domain_audit_could_not_see_are_counted(audit, capsys):
    """`agent_tool_telemetry` is where attribution lives, but it is NOT a
    superset of `tool_usage_stats`: for the audited cycle the dispatch log holds
    17 calls the agent-loop hook never recorded (get_market_data x8,
    lazy_web_search x8, scrape_url x1 — 117 vs 142, differing both ways). The
    port's own thesis is that a green needs its denominator, so the remainder
    has to be out of scope VISIBLY rather than invisibly."""
    from app.db import mongo_query

    calls = [("v3_junior_analyst", "get_market_data", "AAPL", False, "")]
    dispatched = ["get_market_data", "get_market_data", "lazy_web_search"]

    assert audit.unattributed(dispatched, calls) == {
        "get_market_data": 1, "lazy_web_search": 1,
    }, "a multiset difference — two dispatches against one attributed leaves one"

    with patch.object(mongo_query, "exists", return_value=True), \
         patch.object(audit, "load_desks", return_value=[]), \
         patch.object(audit, "load_tool_calls", return_value=calls), \
         patch.object(audit, "load_dispatch_calls", return_value=dispatched):
        rc = audit.audit_latest_cycle("cycle-X")

    out = capsys.readouterr().out
    assert rc == 0
    assert "3 dispatch-level call(s) logged" in out
    assert "2 with no attributed counterpart" in out
    assert "get_market_data x1" in out and "lazy_web_search x1" in out


def test_dispatch_calls_are_read_from_tool_usage_stats_with_prefixes_stripped(audit):
    from app.db import mongo_query

    rows = [("mcp__lazy-agent-service__get_market_data",), ("scrape_url",)]
    with patch.object(mongo_query, "find_rows", return_value=rows) as fr:
        got = audit.load_dispatch_calls("cycle-1")

    assert fr.call_args[0][0] == "tool_usage_stats"
    assert got == ["get_market_data", "scrape_url"], (
        "the two logs are compared by tool name, so both sides must be bare or "
        "every namespaced dispatch reads as unattributed"
    )


def test_a_blank_tool_name_is_not_counted_as_checked(audit, capsys):
    """`breaches()` skips a blank tool name, so counting it as 'checked'
    inflates the denominator that makes the green trustworthy.
    `app/v3/tool_telemetry._canary_check` treats an empty name as a malformed
    dispatch worth a warning — 175 landed on 2026-07-13."""
    from app.db import mongo_query

    calls = [("v3_junior_analyst", "get_market_data", "AAPL", False, ""),
             ("v3_junior_analyst", "", "AAPL", False, "")]
    with patch.object(mongo_query, "exists", return_value=True), \
         patch.object(audit, "load_desks", return_value=[]), \
         patch.object(audit, "load_tool_calls", return_value=calls), \
         patch.object(audit, "load_dispatch_calls", return_value=[]), \
         patch.object(audit, "domain_boundaries",
                      return_value={"v3_junior_analyst": {"get_market_data"}}):
        rc = audit.audit_latest_cycle("cycle-X")

    out = capsys.readouterr().out
    assert rc == 0
    assert "1 attributed call(s) checked" in out, (
        f"the blank tool name was counted as checked:\n{out}"
    )
    assert "EMPTY tool name" in out, "and it must be reported, not just dropped"
