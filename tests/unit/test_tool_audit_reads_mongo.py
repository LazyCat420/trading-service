"""`scripts/tool_audit.py` answers "which whitelisted tools do agents call, and
which calls bypass the whitelist?" from MongoDB, and its two halves describe
the same population.

Three things were wrong, all measured on 2026-08-30 against both live stores.

1. **It read a store that stopped answering.** The three statements went to the
   frozen relational archive through the migration package's shared connection
   helper, whose DSN accessor had been removed from the settings object — so
   every run since the 2026-08-19 cutover died with an AttributeError before
   sending a statement. Loud, but no more informative than a wrong answer.

2. **calls/run had a numerator its denominator did not cover.** The ratio
   divides `agent_tool_telemetry` calls by `v3_agent_telemetry` runs.
   `app/v3/challenger.py:76` runs a paired A/B of the decision agent under
   `challenger-<cycle_id>`: those tool calls are in the first collection and
   the runs are in the second NOWHERE — 1,091 calls over 135 ids in 30 days,
   0 runs; 132 over 9 ids in 7 days, 0 runs. For `v3_decision_synthesizer`
   that is 1,097 of 1,875 calls (58%), reporting 5.1 calls/run where the
   pipeline figure is 2.1. The script's own docstring says that number decides
   what gets pruned.

3. **An empty window read as a clean audit.** `agent_tool_telemetry` has no
   rows on 2026-08-28..31, so `--days 3` selects nothing; every agent printed
   "NO TOOL CALLS", every whitelist looked complied-with, exit 0.

And one trap the port had to walk past rather than into. Postgres counted
successes with `count(*) FILTER (WHERE success)`. Measured over the same 1,389
live rows, the three candidate Mongo spellings do not agree:

    $sum: 1                                        1389   (all calls)
    $sum: {$cond: ["$success", 1, 0]}              1253   <- the FILTER
    $sum: "$success"                                  0   <- silent zero
    $sum: {$cond: [{$eq:["$success",None]},0,1]}   1389   <- counts failures

`$sum` over a boolean returns 0 because Mongo's `$sum` ignores non-numeric
input, and `group_rows`' `("count", col)` — which is the middle spelling's
inverse — counts False as a success. Both wrong answers are silent, and one of
them would have read as "every tool call in the system failed".

Every test below is RED against `git show 77e6dc3:scripts/tool_audit.py`, and the
three that pin behaviour rather than absence were each re-checked against a
deliberately broken copy of the PORTED script (see the docstrings).
"""
from __future__ import annotations

import ast
import datetime as dt
import json
import pathlib
import re
import sys
from unittest.mock import patch

import pytest

REPO = pathlib.Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts" / "tool_audit.py"

if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))


# ---------------------------------------------------------------------------
# A deliberately small stand-in for the aggregation engine.
#
# It implements ONLY the `$match` + `$group` shapes this script emits, and it
# is here because the autouse `block_production_mongo` fixture forbids the real
# client. The one behaviour it must reproduce faithfully is the one that makes
# the FILTER trap silent — `$sum` over a non-numeric value contributes nothing
# — and `test_the_stand_in_reproduces_the_silent_zero` pins it against the live
# numbers quoted in the module docstring, so a wrong harness cannot manufacture
# a passing probe.
# ---------------------------------------------------------------------------

def _evidence_source(path: pathlib.Path) -> list[tuple[str, str]]:
    """The (collection, field) pairs `pipeline_cycle_ids` asks for distinct
    values of. The mechanism has to be a lookup in the collection that supplies
    the DENOMINATOR, not a pattern match on the id."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == "pipeline_cycle_ids")
    return [tuple(a.value for a in n.args[:2])
            for n in ast.walk(fn)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
            and n.func.attr == "distinct_values"
            and all(isinstance(a, ast.Constant) for a in n.args[:2])]


def _truthy(v) -> bool:
    """Mongo's boolean coercion: missing, null, false and 0 are false."""
    return not (v is None or v is False or v == 0)


def _eval(expr, doc):
    if isinstance(expr, str) and expr.startswith("$"):
        return doc.get(expr[1:])
    if isinstance(expr, dict):
        if "$cond" in expr:
            cond, then, other = expr["$cond"]
            return _eval(then, doc) if _truthy(_eval(cond, doc)) else _eval(other, doc)
        if "$in" in expr:
            needle, hay = expr["$in"]
            return _eval(needle, doc) in hay
        if "$eq" in expr:
            a, b = expr["$eq"]
            return _eval(a, doc) == _eval(b, doc)
        if "$dateToString" in expr:
            spec = expr["$dateToString"]
            return _eval(spec["date"], doc).strftime(spec["format"])
    return expr


def _sum(arg, docs) -> int:
    total = 0
    for d in docs:
        v = _eval(arg, d)
        # A BSON bool is NOT a number to `$sum`, and Python's bool subclasses
        # int — so this check is the whole fidelity of the stand-in. Drop it
        # and `{"$sum": "$success"}` starts returning the right answer here
        # while returning 0 against the real server.
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            continue
        total += v
    return total


def _matches(doc, query) -> bool:
    for field, cond in query.items():
        v = doc.get(field)
        if isinstance(cond, dict):
            if "$gt" in cond and not (v is not None and v > cond["$gt"]):
                return False
        elif v != cond:
            return False
    return True


class FakeMongo:
    """An in-memory store keyed by POSTGRES TABLE NAME, as the real API is."""

    def __init__(self, **tables):
        self.tables = {k: list(v) for k, v in tables.items()}
        self.seen_pipelines: list[tuple[str, list]] = []

    def aggregate(self, collection, pipeline, session=None):
        self.seen_pipelines.append((collection, pipeline))
        docs = self.tables.get(collection, [])
        out = None
        for stage in pipeline:
            if "$match" in stage:
                docs = [d for d in docs if _matches(d, stage["$match"])]
            elif "$group" in stage:
                spec = dict(stage["$group"])
                key_spec = spec.pop("_id")
                groups: dict = {}
                for d in docs:
                    if key_spec is None:
                        k = None
                    else:
                        k = tuple(sorted((kk, _eval(vv, d)) for kk, vv in key_spec.items()))
                    groups.setdefault(k, []).append(d)
                out = []
                for k, members in groups.items():
                    row = {"_id": None if k is None else dict(k)}
                    for name, acc in spec.items():
                        assert "$sum" in acc, f"stand-in supports $sum only, got {acc}"
                        row[name] = _sum(acc["$sum"], members)
                    out.append(row)
            else:
                raise AssertionError(f"stand-in does not implement {stage}")
        return out if out is not None else [dict(d) for d in docs]

    def find_docs(self, collection, query, sort=None, projection=None, limit=0, session=None):
        return [dict(d) for d in self.tables.get(collection, []) if _matches(d, query)]

    def distinct_values(self, collection, field, query=None):
        return sorted({d.get(field) for d in self.tables.get(collection, [])
                       if d.get(field) is not None})


@pytest.fixture
def audit():
    import scripts.tool_audit as mod
    return mod


@pytest.fixture
def wired(audit):
    """Install the stand-in behind both helper modules the script reads through."""
    def _install(fake):
        from app.db import mongo_query, mongo_store
        return patch.multiple(
            mongo_store,
            aggregate=fake.aggregate,
            find_docs=fake.find_docs,
            distinct_values=fake.distinct_values,
        ), patch.object(mongo_query, "mongo_store", fake)
    return _install


NOW = dt.datetime.now(dt.timezone.utc)
RECENT = NOW - dt.timedelta(days=1)


def _call(agent, tool, cycle, success=True, when=RECENT):
    return {"agent_name": agent, "tool_name": tool, "cycle_id": cycle,
            "success": success, "created_at": when}


def _run(agent, cycle, when=RECENT):
    return {"agent_name": agent, "cycle_id": cycle, "created_at": when}


# ---------------------------------------------------------------------------


def test_the_script_has_no_postgres_coupling():
    """The archive still answers reads, so a leftover import does not fail
    loudly forever — it waits to be repaired and then lies."""
    hits = [f"{i}: {ln.strip()}"
            for i, ln in enumerate(SCRIPT.read_text(encoding="utf-8").splitlines(), 1)
            if re.search(r"psycopg|DATABASE_URL|pg_connection|dbname=|postgres", ln)]
    assert not hits, hits


def test_the_stand_in_reproduces_the_silent_zero(audit):
    """HARNESS CONTROL. Measured on the live collection over the same 1,389
    rows: `$sum: 1` = 1389, `$sum: {$cond:["$success",1,0]}` = 1253,
    `$sum: "$success"` = 0. If the stand-in did not reproduce the third, every
    assertion below about `ok` would pass against the broken spelling too."""
    docs = [{"success": True}, {"success": True}, {"success": False}]
    assert _sum(1, docs) == 3
    assert _sum({"$cond": ["$success", 1, 0]}, docs) == 2
    assert _sum("$success", docs) == 0, "the trap must survive into the harness"


def test_ok_counts_successes_not_calls_and_not_zero(audit, wired):
    """`count(*) FILTER (WHERE success)`.

    Broken-copy check: replacing the accumulator with `{"$sum": "$success"}`
    makes this assert 0 == 2; replacing it with group_rows' `("count",
    "success")` spelling makes it assert 3 == 2."""
    fake = FakeMongo(
        agent_tool_telemetry=[
            _call("v3_bull_agent", "get_market_data", "cycle-1", success=True),
            _call("v3_bull_agent", "get_market_data", "cycle-1", success=True),
            _call("v3_bull_agent", "get_market_data", "cycle-1", success=False),
        ],
        v3_agent_telemetry=[_run("v3_bull_agent", "cycle-1")],
    )
    p1, p2 = wired(fake)
    with p1, p2:
        calls, _, _ = audit.fetch(7, audit.pipeline_cycle_ids())
    cell = calls["v3_bull_agent"]["get_market_data"]
    assert (cell["n"], cell["ok"]) == (3, 2)


def test_calls_per_run_excludes_calls_the_denominator_has_no_run_for(
        audit, wired, tmp_path):
    """THE HEADLINE FIX. Six of ten calls run under a cycle_id
    `v3_agent_telemetry` never recorded a run for, over two runs.

    The honest ratio is 4/2 = 2.0. Counting every call gives 10/2 = 5.0 — the
    same 2.4x shape the live challenger namespace produces for
    `v3_decision_synthesizer` (5.1 vs 2.1 over 30 days).

    Asserted on the number the script REPORTS, not on one recomputed here. The
    first version of this test checked `(n - shadow) / runs` itself, and so
    stayed green against a copy whose `main()` had been reverted to
    `n_all / n_runs` — it was testing its own arithmetic. Broken-copy check:
    that same reversion now reads 5.0."""
    fake = FakeMongo(
        agent_tool_telemetry=(
            [_call("v3_decision_synthesizer", "whiteboard_read", "cycle-1")] * 4
            + [_call("v3_decision_synthesizer", "whiteboard_read", "challenger-cycle-1")] * 6
        ),
        v3_agent_telemetry=[_run("v3_decision_synthesizer", "cycle-1")] * 2,
    )
    out = tmp_path / "audit.json"
    p1, p2 = wired(fake)
    # The whitelist is stubbed so the test pins the ratio, not today's grant.
    with p1, p2, \
         patch.object(audit, "load_whitelists",
                      return_value=({"v3_decision_synthesizer": ["whiteboard_read"]}, [])), \
         patch.object(sys, "argv",
                      ["tool_audit.py", "--days", "7", "--json", str(out)]):
        assert audit.main() == 0

    reported = json.loads(out.read_text())["v3_decision_synthesizer"]
    assert reported["used"] == {"whiteboard_read": 10}, "every call is still counted"
    assert reported["shadow_calls"] == 6, "the six with no run behind them are marked"
    assert reported["runs"] == 2
    assert reported["calls_per_run"] == 2.0, (
        "calls/run divided a numerator the denominator does not cover")


def test_shadow_calls_are_still_audited_for_usage_and_enforcement(audit, wired):
    """The challenger runs the real agent under the real whitelist, so its
    calls are that agent's genuine usage and an off-whitelist one is a genuine
    breach. Only the ratio is affected. Dropping them instead would report a
    tool as DEAD and invite pruning something the challenger depends on."""
    fake = FakeMongo(
        agent_tool_telemetry=[
            _call("v3_decision_synthesizer", "whiteboard_read", "challenger-cycle-1"),
            _call("v3_decision_synthesizer", "execute_javascript", "challenger-cycle-1"),
        ],
        v3_agent_telemetry=[_run("v3_decision_synthesizer", "cycle-1")],
    )
    p1, p2 = wired(fake)
    with p1, p2:
        calls, _, _ = audit.fetch(7, audit.pipeline_cycle_ids())
    tools = calls["v3_decision_synthesizer"]
    assert tools["whiteboard_read"]["n"] == 1, "a shadow call is not dropped"
    assert tools["execute_javascript"]["n"] == 1, "nor is a shadow breach"


def test_the_split_is_evidence_from_the_denominator_not_a_prefix_list(audit, wired):
    """A blocklist of shadow prefixes drifts, and the next harness to mint a
    namespace walks through it. The test is membership in the collection that
    SUPPLIES the denominator, so an unknown namespace is handled without being
    named — and a `cycle-*` id that logged no run is caught too (14 such calls
    in the live 30-day window, which a `^cycle-` regex counts as evidenced)."""
    assert _evidence_source(SCRIPT) == [("v3_agent_telemetry", "cycle_id")], (
        "the evidence set must be read from the collection that supplies the "
        "calls/run denominator")
    tree = ast.parse(SCRIPT.read_text(encoding="utf-8"))
    assert not [n for n in ast.walk(tree)
                if isinstance(n, ast.Constant) and n.value == "$regex"], (
        "a cycle_id pattern match is a blocklist by another name")

    fake = FakeMongo(
        agent_tool_telemetry=[
            _call("v3_junior_analyst", "scrape_url", "brand-new-harness-42"),
            _call("v3_junior_analyst", "scrape_url", "cycle-orphan"),
            _call("v3_junior_analyst", "scrape_url", "cycle-1"),
        ],
        v3_agent_telemetry=[_run("v3_junior_analyst", "cycle-1")],
    )
    p1, p2 = wired(fake)
    with p1, p2:
        calls, _, _ = audit.fetch(7, audit.pipeline_cycle_ids())
    assert calls["v3_junior_analyst"]["scrape_url"]["shadow"] == 2, (
        "an unnamed namespace AND a run-less cycle-* id are both uncovered")


def test_the_daily_bucket_is_the_calendar_day_of_the_call(audit, wired):
    """`created_at::date`. Verified against the archive before the port: the
    UTC `$dateToString` bucket and the session-timezone `::date` bucket
    produced identical 308 day/tool groups over 2026-08-01..08-19."""
    day = NOW - dt.timedelta(days=2)
    fake = FakeMongo(
        agent_tool_telemetry=[
            _call("v3_bull_agent", "get_sec_filings", "cycle-1", when=day),
            _call("v3_bull_agent", "get_sec_filings", "cycle-1", when=day),
        ],
        v3_agent_telemetry=[_run("v3_bull_agent", "cycle-1")],
    )
    p1, p2 = wired(fake)
    with p1, p2:
        _, _, daily = audit.fetch(7, audit.pipeline_cycle_ids())
    assert daily[day.strftime("%Y-%m-%d")]["get_sec_filings"] == 2


def test_a_window_with_no_calls_exits_2(audit):
    """Every agent prints "NO TOOL CALLS", every whitelist looks complied-with,
    and nothing measured anything. `--days 3` does exactly this on live data
    today. Broken-copy check: returning 0 unconditionally makes this read 0."""
    empty = {}
    with patch.object(audit, "pipeline_cycle_ids", return_value=set()), \
         patch.object(audit, "fetch", return_value=(empty, empty, empty)), \
         patch.object(audit, "unrecorded_calls", return_value={
             "window_days": 3, "shared_cycle_ids": 0,
             "agent_tool_telemetry_calls": 0, "tool_usage_stats_calls": 0,
             "unattributable_calls": 0, "top_unattributable": [],
             "tool_usage_stats_rows": 0, "tool_usage_stats_rows_naming_an_agent": 0}), \
         patch.object(sys, "argv", ["tool_audit.py", "--days", "3"]):
        assert audit.main() == 2


def test_an_agent_module_that_will_not_import_is_named_not_swallowed(audit):
    """A raising module drops its agent out of the report entirely, which does
    not look like an error — it looks like an agent that does not exist, and
    its whitelist goes unaudited with nothing on screen to say so."""
    import importlib

    real = importlib.import_module

    def boom(name, *a, **k):
        if name.startswith("app.v3.agents."):
            raise RuntimeError("simulated")
        return real(name, *a, **k)

    with patch.object(audit.importlib, "import_module", side_effect=boom):
        wl, failed = audit.load_whitelists()
    assert wl == {}
    assert failed and all("RuntimeError" in f for f in failed)
