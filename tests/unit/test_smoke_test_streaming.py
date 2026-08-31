"""`smoke_test_streaming.py` must read the store the cycles are written to —
and must stop scoring a subsystem that was deleted.

WHY EACH TEST HERE WOULD HAVE BEEN RED BEFORE THE 2026-08-30 PORT
------------------------------------------------------------------
  test_the_script_has_no_postgres_coupling
        the pre-port file's line 46 was
        `from scripts.migration.pg_connection import get_db`, and all three
        reads went through it.
  test_the_detector_sees_a_planted_coupling
        the negative control for the test above: the same detector run over
        that exact line has to hit, so a detector that quietly stopped matching
        cannot pass the file by default.
  test_the_event_count_names_the_table_it_reads
  test_the_event_page_is_ordered_by_a_total_key
  test_the_analysis_read_asks_for_the_columns_it_unpacks
        the pre-port script made no Mongo call at all, so these stubs record
        ZERO calls; it opened a pooled archive cursor instead. Since 2026-08-28
        that cursor could not even be opened — the field it resolved the DSN
        through was removed from `Settings` — so all three reads raised
        AttributeError and the run ended at "Failed to start cycle".
  test_a_subdocument_payload_is_decoded
  test_the_old_decode_really_does_fail_on_the_live_shape
        `result_json` is a TEXT column in the archive and a SUBDOCUMENT in
        Mongo. The pre-port line was `r = json.loads(row[1])`, which raises
        TypeError on a dict; the blanket `except Exception` printed
        "(parse error)" for EVERY row of a live cycle. The second test is the
        negative control that pins that.
  test_a_null_payload_is_an_absence_not_a_parse_error
        `json.loads(None)` raises too, so the pre-port code also called the
        3,130 rows of 5,290 that store no payload a parse failure.
  test_the_retired_milestones_do_not_decide_the_verdict
  test_a_cycle_with_no_analysis_start_is_not_a_pass
        `critical_checks` was ["pre_push", "parallel_tracks", "early_analysis",
        "fast_start"], every one of them keyed on a step name nothing emits, so
        the pre-port verdict was PARTIAL or FAIL for any input whatsoever and
        `🟢 PASS` was unreachable.
  test_every_retired_step_name_still_has_no_emitter
  test_the_live_milestone_still_has_an_emitter
        the guard on the judgement above, run against `app/` rather than
        against a note: if the streaming pipeline ever comes back, or if
        `v3_start_` is renamed the way `v2_start_` was, this fails instead of
        the script silently reporting "(not observed)" forever.
  test_events_written_after_the_terminal_status_are_still_scored
        the pre-port loop broke out of the poll the moment the status reached
        `done` and never read again, so the tail of every cycle went unscored —
        on a fast cycle that is the milestone itself.
"""
from __future__ import annotations

import ast
import json
import pathlib
import re
import sys
import types
from datetime import datetime, timedelta

import pytest

import scripts.smoke_test_streaming as sts


# ── the static half ────────────────────────────────────────────────────────

_PG = re.compile(r"psycopg|DATABASE_URL|pg_connection|dbname=|postgres", re.I)


def _strip_prose(src: str) -> str:
    """Drop comments and docstrings, the way `pg_script_inventory` does.

    The ported file EXPLAINS at length which store it left and what the frozen
    archive held. A detector that fires on that explanation is one the next
    person deletes, so the couplings are looked for in code only.
    """
    tree = ast.parse(src)
    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                             ast.AsyncFunctionDef)):
            doc = ast.get_docstring(node, clean=False)
            if doc:
                docstrings.add(doc)
    out = "\n".join(line.split("#", 1)[0] for line in src.splitlines())
    for doc in docstrings:
        out = out.replace(doc, "")
    return out


def _couplings(src: str) -> list[str]:
    tree = ast.parse(src)
    hits = []
    for node in ast.walk(tree):
        names = []
        if isinstance(node, ast.Import):
            names = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module:
            names = [node.module]
        for name in names:
            if name.startswith("psycopg") or "pg_connection" in name:
                hits.append(f"line {node.lineno}: imports {name}")
    for i, line in enumerate(_strip_prose(src).splitlines(), start=1):
        if _PG.search(line):
            hits.append(f"line {i}: {line.strip()}")
    return hits


def _source() -> str:
    """The source of the module under test — a mutation harness can point
    `scripts.smoke_test_streaming` at a different file, and this follows it
    rather than re-deriving the path from the repo layout."""
    return pathlib.Path(sts.__file__).read_text(encoding="utf-8")


def test_the_script_has_no_postgres_coupling():
    assert _couplings(_source()) == []


def test_the_detector_sees_a_planted_coupling():
    """NEGATIVE CONTROL. The pre-port import and read, verbatim."""
    planted = (
        '"""A docstring naming psycopg and the old DSN setting: prose."""\n'
        "import json\n"
        "from scripts.migration.pg_connection import get_db\n"
        "def f(cycle_id):\n"
        "    with get_db() as db:\n"
        "        return db.execute(\n"
        "            'SELECT ticker, result_json FROM analysis_results "
        "WHERE cycle_id = %s', [cycle_id]).fetchall()\n"
    )
    hits = _couplings(planted)
    assert any("pg_connection" in h for h in hits), hits


def test_prose_about_the_old_store_is_not_a_coupling():
    """The other direction: the detector must not fire on the explanation."""
    assert _couplings(
        '"""It used to read postgres through pg_connection/DATABASE_URL."""\n'
        "# psycopg is gone\n"
        "x = 1\n"
    ) == []


# ── the read seam ──────────────────────────────────────────────────────────

T0 = datetime(2026, 8, 30, 7, 15, 45, 584000)


def _event(step, offset_s, phase="analyzing", detail="d", status="ok", ms=None):
    return (phase, step, detail, status, ms, T0 + timedelta(seconds=offset_s))


@pytest.fixture
def store(monkeypatch):
    """Stand-ins for the three reads, recording how they were called.

    `block_production_mongo` (autouse, tests/conftest.py) already fails a test
    that reaches the real client; these replace the reads so the scoring can be
    driven deterministically.
    """
    state = {"count": 0, "events": [], "results": [], "calls": []}

    def count(collection, query=None):
        state["calls"].append(("count", collection, query))
        return state["count"]

    def aggregate(collection, pipeline, session=None):
        state["calls"].append(("aggregate", collection, pipeline))
        skip = next(s["$skip"] for s in pipeline if "$skip" in s)
        project = next(s["$project"] for s in pipeline if "$project" in s)
        cols = [c for c in project if c != "_id"]
        return [dict(zip(cols, ev)) for ev in state["events"][skip:]]

    def find_rows(collection, query, columns, sort=None, limit=0):
        state["calls"].append(("find_rows", collection, query, tuple(columns)))
        return list(state["results"])

    monkeypatch.setattr(sts.mongo_query, "count", count)
    monkeypatch.setattr(sts.mongo_store, "aggregate", aggregate)
    monkeypatch.setattr(sts.mongo_query, "find_rows", find_rows)
    return state


def test_the_event_count_names_the_table_it_reads(store):
    store["count"] = 7

    assert sts.count_events("cycle-x") == 7

    kind, collection, query = store["calls"][0]
    assert kind == "count"
    # A POSTGRES TABLE NAME. Every helper resolves it to a collection exactly
    # once; `collection_for(...)` here would resolve it twice — see
    # tests/unit/test_no_double_collection_resolution.py.
    assert collection == "pipeline_events"
    assert query == {"cycle_id": "cycle-x"}


def test_the_event_page_is_ordered_by_a_total_key(store):
    """`ORDER BY timestamp ASC OFFSET n` needs an order the sort defines.

    Microsecond timestamps made that a total order in the archive — 0
    collisions inside a cycle over 190,775 rows. At BSON's millisecond
    resolution the same events collide 2,522 times, up to 14 at once, and a
    `$skip` into a group the sort does not order is a page boundary that can
    drop an event.
    """
    store["events"] = [_event("a", 0), _event("b", 1), _event("c", 2)]

    rows = sts.read_events_after("cycle-x", 1)

    kind, collection, pipeline = store["calls"][0]
    assert kind == "aggregate" and collection == "pipeline_events"
    stages = {k: v for stage in pipeline for k, v in stage.items()}
    assert stages["$match"] == {"cycle_id": "cycle-x"}
    assert stages["$sort"] == {"timestamp": 1, "_id": 1}, (
        "a sort on timestamp alone leaves tied events unordered, so the page "
        "boundary can lose one"
    )
    # OFFSET is done by the store, not by slicing everything into this process:
    # the largest cycle in the archive holds 15,566 events and the loop re-reads
    # every two seconds.
    assert stages["$skip"] == 1
    assert [r[1] for r in rows] == ["b", "c"]
    assert all(len(r) == len(sts.EVENT_COLUMNS) for r in rows)


def test_the_analysis_read_asks_for_the_columns_it_unpacks(store):
    store["results"] = [("AAPL", {"action": "HOLD"})]

    assert sts.read_analysis_results("cycle-x") == [("AAPL", {"action": "HOLD"})]

    kind, collection, query, columns = store["calls"][0]
    assert kind == "find_rows" and collection == "analysis_results"
    assert query == {"cycle_id": "cycle-x"}
    assert columns == ("ticker", "result_json")


# ── the payload shape ──────────────────────────────────────────────────────

def test_a_subdocument_payload_is_decoded():
    assert sts.decode_result({"action": "HOLD", "confidence": 65}) == {
        "action": "HOLD", "confidence": 65}


def test_the_old_decode_really_does_fail_on_the_live_shape():
    """NEGATIVE CONTROL for the test above — the pre-port line was
    `r = json.loads(row[1])`, and this is what it does to a live row."""
    with pytest.raises(TypeError):
        json.loads({"action": "HOLD"})
    with pytest.raises(TypeError):
        json.loads(None)


def test_a_text_payload_still_decodes():
    """The archive's shape, so replaying a pre-cutover cycle still works."""
    assert sts.decode_result('{"action": "SELL"}') == {"action": "SELL"}


def test_an_unreadable_payload_is_none_not_an_exception():
    assert sts.decode_result("{not json") is None
    assert sts.decode_result("[1, 2]") is None      # valid JSON, wrong shape


def test_a_null_payload_is_an_absence_not_a_parse_error(capsys):
    """3,130 of 5,290 rows store no payload; that is not a failure to read one."""
    sts.report("cycle-x", [_event("v3_start_AAPL", 0)],
               [("AAPL", None)], "replay", 1.0)

    out = capsys.readouterr().out
    assert "(no result payload stored)" in out
    assert "parse error" not in out


def test_a_decoded_payload_is_printed_from_the_subdocument(capsys):
    sts.report("cycle-x", [_event("v3_start_AAPL", 0)],
               [("AAPL", {"action": "HOLD", "confidence": 65,
                          "total_time_s": 369.6, "total_tokens": 12345})],
               "replay", 1.0)

    assert "AAPL: HOLD @ 65% (369.6s, 12,345 tokens)" in capsys.readouterr().out


# ── the dead half ──────────────────────────────────────────────────────────

# The step literal each retired milestone matches. Kept here rather than read
# off the matchers so the two can disagree and be caught.
_RETIRED_STEPS = {
    "watchlist_prepush": "watchlist_prepush",
    "first_worker_got": "worker_got_",
    "track_a_start": "track_a_start",
    "track_b_start": "track_b_start",
    "parallel_start": "parallel_start",
    "first_dedup": "worker_dedup_",
    "collection_complete": "collection_complete",
    "pipeline_done": "pipeline_done",
}

REPO = pathlib.Path(sts.__file__).resolve().parents[1]


def test_the_retired_set_is_the_one_the_script_declares():
    """Set equality, both directions — never a length. An allowlist can drift
    both ways and keep its count."""
    declared = {m.key for m in sts.MILESTONES if m.state == sts.RETIRED}
    assert declared == set(_RETIRED_STEPS)
    assert {m.key for m in sts.MILESTONES if m.state == sts.LIVE} == {
        "first_analysis_start"}


def test_every_retired_step_name_still_has_no_emitter():
    """If the streaming pipeline ever comes back, un-retire its milestone.

    Measured 2026-08-30: none of these nine names appears anywhere under
    `app/`. Five of them were emitted from `app/cycle/**`, deleted whole on
    2026-06-25 (0cedef3); the three `track_*`/`parallel_start` names appear in
    no commit outside the script itself.
    """
    sources = [p.read_text(encoding="utf-8")
               for p in (REPO / "app").rglob("*.py")
               if "__pycache__" not in p.parts]
    still_emitted = {key: step for key, step in _RETIRED_STEPS.items()
                     if any(step in src for src in sources)}
    assert still_emitted == {}, (
        f"a retired milestone has an emitter again: {still_emitted} — "
        "move it back to LIVE and put it back in CRITICAL_CHECKS"
    )


def test_the_live_milestone_still_has_an_emitter():
    """The other direction, so a rename cannot make this silently report
    "(not observed)" forever, which is exactly what happened to `v2_start_`."""
    sources = [p.read_text(encoding="utf-8")
               for p in (REPO / "app").rglob("*.py")
               if "__pycache__" not in p.parts]
    assert any("v3_start_" in src for src in sources), (
        "nothing under app/ emits v3_start_<TICKER> any more; re-point "
        "MILESTONES['first_analysis_start'] at whatever replaced it"
    )


def test_the_retired_milestones_do_not_decide_the_verdict(capsys):
    """A cycle that starts analysis promptly and produces a decision PASSES.

    Under the pre-port `critical_checks` this input scored 0 of 4 and could
    only print PARTIAL, because all four names were dead.
    """
    ok = sts.report("cycle-x",
                    [_event("cycle_trigger", 0, phase="starting"),
                     _event("v3_start_AAPL", 0.2),
                     _event("v3_done_AAPL", 20)],
                    [("AAPL", {"action": "HOLD", "confidence": 65})],
                    "replay", 20.0)

    out = capsys.readouterr().out
    assert ok is True
    assert "🟢 PASS" in out
    assert "Fast start: analysis pipeline started at 0.2s" in out
    assert "Watchlist Prepush" in out and "retired" in out
    assert sts.CRITICAL_CHECKS == ("fast_start",)


def test_a_cycle_with_no_analysis_start_is_not_a_pass(capsys):
    ok = sts.report("cycle-x", [_event("cycle_trigger", 0, phase="starting")],
                    [("AAPL", {"action": "HOLD"})], "replay", 1.0)

    assert ok is False
    assert "🟡 PARTIAL" in capsys.readouterr().out


def test_the_milestone_clock_is_the_events_own(capsys):
    """Measured between two event timestamps, not against the poller's clock:
    the old number was quantised to the 2-second poll and mixed two machines'
    clocks."""
    found = sts.collect_milestones(
        [_event("cycle_trigger", 0), _event("v3_start_AAPL", 12.5)])

    assert found["first_analysis_start"] == pytest.approx(12.5)
    assert found["watchlist_prepush"] is None


# ── the live loop's tail ───────────────────────────────────────────────────

async def test_events_written_after_the_terminal_status_are_still_scored(
        monkeypatch, capsys):
    """The status is written before the last events land.

    The pre-port loop broke out of the poll on `done` and never read again, so
    the tail of every cycle went unscored — and on a cycle this fast the tail
    IS the milestone.
    """
    events = [_event("cycle_trigger", 0, phase="starting"),
              _event("v3_start_AAPL", 0.4)]   # arrives after status == done

    # `visible` is how many of them the store has flushed. The second one
    # lands AFTER the poll that saw `done`, which is the case under test.
    visible = {"n": 1, "count_calls": 0}

    def count_events(cycle_id):
        visible["count_calls"] += 1
        answer = visible["n"]
        if visible["count_calls"] == 2:
            visible["n"] = 2
        return answer

    monkeypatch.setattr(sts, "count_events", count_events)
    monkeypatch.setattr(sts, "read_events_after",
                        lambda cid, seen=0: events[seen:visible["n"]])
    monkeypatch.setattr(sts, "read_analysis_results",
                        lambda cid: [("AAPL", {"action": "HOLD", "confidence": 65})])
    monkeypatch.setattr(sts, "POLL_INTERVAL_SECONDS", 0)

    # The first read is the stuck-cycle check, which must NOT fire.
    statuses = iter(["idle", "running", "done"])

    class PipelineService:
        _state: dict = {}

        @classmethod
        def get_current_state(cls, summary_only=False):
            return {"status": next(statuses, "done")}

        @classmethod
        def save_state(cls):
            raise AssertionError("the smoke test must not write state here")

        @classmethod
        async def start_cycle(cls, **kwargs):
            return {"cycle_id": "cycle-x"}

    module = types.ModuleType("app.services.pipeline_service")
    module.PipelineService = PipelineService
    monkeypatch.setitem(sys.modules, "app.services.pipeline_service", module)

    ok = await sts.run_streaming_test("AAPL", timeout=30, verbose=False)

    out = capsys.readouterr().out
    assert ok is True, out
    assert "Fast start: analysis pipeline started at 0.4s" in out
