"""`scripts/skill_scorecard.py` must read `agent_skills` from Mongo, in order.

WHY THIS FILE EXISTS
--------------------
The skill scorecard is the only view of what the skill loop believes about each
agent — which version is serving, how many resolved decisions it governs, and
whether it regressed against its predecessor. Every number below the version
column already came from Mongo (`app/autoresearch/scorecard.py` was converted
with `app/`), but the version numbers themselves did not: `_versions()` went
through `scripts.migration.pg_connection.get_db`, whose pool reaches the frozen
archive, so the script could only ever report the versions as they stood at the
2026-08-19 cutover — or die on the way there. `scripts/gate_zero_pg.py` counted
**4 couplings** in the pre-port file: `connection_import` at line 30,
`get_db_call` at 45, and `execute_call` at 47 and 53.

Four things are pinned here. The first three were RED before the port:

  1. the file has no Postgres coupling at all (4 findings -> 0);

  2. `--history` comes back ASCENDING and NUMERICALLY. `main()` treats
     `versions[-1]` as the version to run the regression test on and every
     earlier entry as history, so order is not cosmetic: a port that dropped
     `sort=[("version", 1)]` and took Mongo's natural order would compare
     whichever version happened to be inserted last against its predecessor,
     and a port that sorted the version as a string would put v10 before v2 and
     hand `regression_verdict` v9 on a 26-version agent;

  3. the active branch filters on `status == "active"` and, when more than one
     row claims it, resolves to the HIGHEST version. The SQL had no ORDER BY
     and took whatever the heap returned first; Mongo's natural order is a
     different arbitrary order, so the port pins it the way the two app-side
     readers of the same question already do (`skill_loader._load`,
     `skill_optimizer._load_skill`, both ported from
     `... status = 'active' ORDER BY version DESC LIMIT 1`).
     The write path archives-then-inserts as two un-transacted operations, so
     two active rows is a state this can land in. Measured on the live store
     2026-08-30: all 7 agents have exactly one active row, so this pin changes
     nothing about today's output — it decides what happens the day one of
     those two writes lands without the other;

  4. (regression guard) every store call names the POSTGRES TABLE
     `agent_skills`, never `collection_for("agent_skills")` — see
     `tests/unit/test_no_double_collection_resolution.py` for the defect.

The reads are STUBBED, not live. The version numbers this script prints move
every time SkillOpt promotes a doc, and a test asserting today's 26 fails
tomorrow for no defect. The live cross-check is kept as an explicit probe at
the bottom, skipped unless TRADING_BOT_LIVE_AUDIT=1.
"""

from __future__ import annotations

import ast
import sys
from datetime import datetime
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from scripts import skill_scorecard as ss  # noqa: E402
from scripts.gate_zero_pg import scan  # noqa: E402

SRC = (REPO / "scripts" / "skill_scorecard.py").read_text(encoding="utf-8")


# ── 1. the coupling is gone ────────────────────────────────────────────────

def test_the_skill_scorecard_has_no_postgres_coupling():
    """RED before the port: 4 findings — connection_import at line 30,
    get_db_call at 45, execute_call at 47 and 53."""
    result = scan(REPO, targets=("scripts/skill_scorecard.py",))
    assert result["errors"] == [], result["errors"]
    assert result["total"] == 0, (
        "skill_scorecard.py still reads Postgres: "
        + "; ".join(f"{f['kind']} at line {f['line']}" for f in result["findings"]))


def test_the_scan_still_fails_on_the_pre_port_source(tmp_path):
    """NEGATIVE CONTROL, and the receipt for the claim above.

    A scan that reports zero because it looked at nothing satisfies the
    assertion above just as happily. This feeds it the pre-port `_versions()`
    verbatim and requires the same four findings back, so "4 -> 0" is measured
    rather than remembered.
    """
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "skill_scorecard.py").write_text(
        "from scripts.migration.pg_connection import get_db\n"
        "\n"
        "def _versions(agent, history):\n"
        "    with get_db() as db:\n"
        "        if history:\n"
        "            rows = db.execute(\n"
        "                'SELECT version FROM agent_skills WHERE agent_name = %s '\n"
        "                'ORDER BY version', [agent]).fetchall()\n"
        "            return [r[0] for r in rows]\n"
        "        row = db.execute(\n"
        "            'SELECT version FROM agent_skills '\n"
        "            \"WHERE agent_name = %s AND status = 'active'\", [agent]).fetchone()\n"
        "        return [row[0]] if row else []\n",
        encoding="utf-8")
    control = scan(tmp_path, targets=("scripts/skill_scorecard.py",))
    kinds = sorted(f["kind"] for f in control["findings"])
    assert kinds == ["connection_import", "execute_call", "execute_call",
                     "get_db_call"], kinds


# ── a filter/sort evaluator, so the stub judges the QUERY ──────────────────
#
# The stubbed store applies the real filter and the real sort to its fixtures
# instead of ignoring them. A stub that hands its documents back regardless
# cannot tell `{"status": "active"}` from no filter at all, and one that
# ignores `sort=` reports the fixture order — which is exactly the order an
# unsorted port would get from the server and call correct.

_MISSING = object()


def _match(doc: dict, query: dict) -> bool:
    for field, cond in query.items():
        val = doc.get(field, _MISSING)
        if isinstance(cond, dict):  # pragma: no cover - none used by this file
            raise AssertionError(f"operator filter {cond!r} not modelled")
        if val is _MISSING or val != cond:
            return False
    return True


def _project(doc: dict, projection: dict | None) -> dict:
    if not projection:
        return dict(doc)
    return {k: doc[k] for k, v in projection.items() if v and k != "_id" and k in doc}


# Deliberately scrambled, and 2/10 are adjacent on purpose: insertion order is
# neither ascending nor descending, so a port with no `sort=` fails test 2, and
# a lexicographic sort puts 10 before 2 and fails it differently.
_SKILLS = [
    {"agent_name": "v3_bear_agent", "version": 10, "status": "archived",
     "created_at": datetime(2026, 8, 10)},
    {"agent_name": "v3_bear_agent", "version": 2, "status": "archived",
     "created_at": datetime(2026, 7, 22)},
    {"agent_name": "v3_bear_agent", "version": 11, "status": "active",
     "created_at": datetime(2026, 8, 12)},
    {"agent_name": "v3_bear_agent", "version": 1, "status": "archived",
     "created_at": datetime(2026, 7, 20)},
    {"agent_name": "v3_bear_agent", "version": 3, "status": "archived",
     "created_at": datetime(2026, 7, 25)},
    # a different agent's rows must never leak into the answer
    {"agent_name": "v3_bull_agent", "version": 7, "status": "active",
     "created_at": datetime(2026, 8, 11)},
]


def _forbidden(name):
    def _raise(*_a, **_k):
        raise AssertionError(f"the scorecard is read-only; it called {name}()")
    return _raise


@pytest.fixture
def store(monkeypatch):
    """Stub `mongo_store.find_docs`, recording every (query, sort) it is given."""
    from app.db import mongo_store

    seen: list[tuple[str, dict, list | None, int]] = []
    data = {"agent_skills": _SKILLS}

    def fake_find_docs(collection, query, sort=None, projection=None,
                       limit=0, session=None):
        seen.append((collection, dict(query), sort, limit))
        assert collection in data, f"unexpected collection {collection!r}"
        rows = [_project(d, projection) for d in data[collection]
                if _match(d, query)]
        if sort:
            for field, direction in reversed(sort):
                rows.sort(key=lambda r: r.get(field), reverse=direction < 0)
        # LIMIT is applied AFTER the sort, as the server does. The other order
        # would let `find_row(..., sort=...)` look correct while actually
        # returning the first natural-order document.
        return rows[:limit] if limit else rows

    monkeypatch.setattr(mongo_store, "find_docs", fake_find_docs)
    for name in ("insert_docs", "upsert_doc", "update_docs", "delete_docs"):
        monkeypatch.setattr(mongo_store, name, _forbidden(name), raising=False)
    return seen


# ── 2. --history: ascending, numeric, one agent ────────────────────────────

def test_history_comes_back_ascending_and_numerically(store):
    """RED with no `sort=` (returns [10, 2, 11, 1, 3], the fixture order) and
    RED for a string sort (`[1, 10, 11, 2, 3]`, which hands `main()` v3 as the
    newest version of an 11-version agent)."""
    assert ss._versions("v3_bear_agent", True) == [1, 2, 3, 10, 11]

    collection, query, sort, _limit = store[0]
    assert collection == "agent_skills"
    assert query == {"agent_name": "v3_bear_agent"}
    assert sort == [("version", 1)], (
        "ORDER BY version must be pushed into the query; sorting afterwards in "
        "Python is equivalent only until someone adds a limit")


def test_history_does_not_leak_another_agents_versions(store):
    assert 7 not in ss._versions("v3_bear_agent", True)
    assert ss._versions("v3_bull_agent", True) == [7]


# ── 3. the active branch ───────────────────────────────────────────────────

def test_the_active_branch_filters_on_status_and_returns_one_version(store):
    """The default mode is "the version serving now", not "the newest row"."""
    assert ss._versions("v3_bear_agent", False) == [11]

    _collection, query, _sort, limit = store[0]
    assert query == {"agent_name": "v3_bear_agent", "status": "active"}, (
        "without the status filter this reports an archived version as active")
    assert limit == 1


def test_two_active_rows_resolve_to_the_newest(store, monkeypatch):
    """RED for a port that omits the sort on the active branch.

    `skill_optimizer._save_skill()` archives the current active row and inserts
    the new one as two separate, un-transacted Mongo operations. Land the insert without the
    update and two rows claim `status = 'active'`; Mongo's natural order then
    hands back whichever it likes, and reporting v11 as active on an agent
    already serving v12 makes the scorecard silently a version stale.
    """
    _SKILLS.append({"agent_name": "v3_bear_agent", "version": 12,
                    "status": "active", "created_at": datetime(2026, 8, 13)})
    try:
        assert ss._versions("v3_bear_agent", False) == [12]
        assert store[0][2] == [("version", -1)]
    finally:
        _SKILLS.pop()


def test_an_agent_with_no_skill_row_returns_no_versions(store):
    """The UNCOVERED path. `v3_valuation_analyst` runs off a pinned doctrine
    file rather than an `agent_skills` row, so this is a real caller, not a
    hypothetical: `--agent v3_valuation_analyst` must print UNCOVERED, not
    raise on `row[0]`."""
    assert ss._versions("v3_valuation_analyst", False) == []
    assert ss._versions("v3_valuation_analyst", True) == []


# ── the ordering contract the ascending sort exists for ────────────────────

def test_main_regression_tests_the_newest_version_and_scores_the_rest(monkeypatch, capsys):
    """`main()` is what makes the sort load-bearing, so pin it here rather than
    trusting the comment: `regression_verdict` gets the LAST element and
    `build_scorecard` every earlier one.

    GREEN before and after the port, by design — it stubs `_versions` and so
    says nothing about which store answered. It is the other half of test 2:
    without it, "ascending" is an assertion about a sort nobody has shown
    matters."""
    from app.autoresearch.scorecard import VersionScorecard

    monkeypatch.setattr(ss, "_versions", lambda agent, history: [1, 2, 3])
    monkeypatch.setattr(ss, "TARGET_AGENTS", {"v3_bear_agent": "bear"})

    regressed: list[int] = []
    scored: list[int] = []

    def fake_regression(agent, v):
        regressed.append(v)
        return VersionScorecard(agent_name=agent, version=v)

    def fake_build(agent, v):
        scored.append(v)
        return VersionScorecard(agent_name=agent, version=v)

    monkeypatch.setattr(ss, "regression_verdict", fake_regression)
    monkeypatch.setattr(ss, "build_scorecard", fake_build)
    monkeypatch.setattr(sys, "argv", ["skill_scorecard.py", "--history"])

    assert ss.main() == 0
    assert regressed == [3], "the regression test must run on the newest version"
    assert scored == [1, 2]
    assert "v3_bear_agent" in capsys.readouterr().out


# ── 4. regression guard: one collection resolution, table names only ───────

def test_every_store_call_names_a_postgres_table_not_a_resolved_collection():
    """`mongo_store._coll` resolves the name exactly once. Handing it
    `collection_for(t)` resolves it twice — a no-op only while renames are off,
    and a silent second collection the day they are on."""
    tree = ast.parse(SRC)
    literals = []
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id in {"mongo_store", "mongo_query"}
                and node.args):
            arg = node.args[0]
            assert isinstance(arg, ast.Constant) and isinstance(arg.value, str), (
                f"line {node.lineno}: the collection argument must be a literal "
                "Postgres table name")
            assert "collection_for" not in ast.unparse(arg)
            literals.append(arg.value)
    assert literals == ["agent_skills", "agent_skills"], literals


# ── live cross-check (opt-in) ──────────────────────────────────────────────

@pytest.mark.real_mongo
def test_live_every_target_agent_has_versions_and_the_active_one_is_the_newest():
    """A port that compiles, runs and returns `[]` is the failure this effort
    exists to catch. Opt-in: `TRADING_BOT_LIVE_AUDIT=1`."""
    import os
    if not os.environ.get("TRADING_BOT_LIVE_AUDIT"):
        pytest.skip("live audit — set TRADING_BOT_LIVE_AUDIT=1")

    for agent in ss.TARGET_AGENTS:
        history = ss._versions(agent, True)
        assert history, f"{agent} has no versions in agent_skills"
        assert history == sorted(history), f"{agent} history is not ascending"
        assert history == list(range(1, len(history) + 1)), (
            f"{agent} versions are not contiguous from 1: {history}")
        assert ss._versions(agent, False) == [history[-1]], (
            f"{agent}: the active row is not the newest version")
