"""`scripts/verify_audit_phases.py` verifies a LIVE cycle, from the live store.

WHAT WAS WRONG, MEASURED 2026-08-30 AGAINST BOTH STORES
-------------------------------------------------------
1. **It did not run at all.** Both reads went through the migration connection
   pool, and since the archive DSN setting was dropped on 2026-08-28 that
   raises `AttributeError: 'Settings' object has no attribute 'DATABASE_URL'`
   on the first statement. `python scripts/verify_audit_phases.py` produced a
   traceback and nothing else.

2. **Even when it ran it read the frozen archive.** SQL's newest
   `shared_desk` cycle is `cycle-v3-1787179210` (2026-08-19 22:44:43) and
   always will be; Mongo's is `cycle-v3-1788074145` (2026-08-30 07:21:55),
   with 201 desk rows written after the cutover that the archive has never
   seen. A script whose entire premise is "check the fix against a REAL cycle"
   was checking an eleven-day-old one.

3. **Half of it had already moved.** `app.v3.reconciliation` reads Mongo, and
   did before this port. So the shared_desk-vs-trade_results check compared a
   frozen record against a live one and would have blamed the writer for the
   store mismatch.

4. **`desk_data` arrives in two shapes and the recent one is a string.** Of
   the 2,036 documents in the collection, 1,762 (the backfill) hold it as a
   subdocument and 274 (everything the live writer has produced) hold it as
   JSON TEXT. `desks[t].get("cycle_metadata")` on a string raises
   `AttributeError: 'str' object has no attribute 'get'`, so a port that
   dropped the normalisation would crash on exactly the cycles the script
   exists to check and pass on every archive cycle a test happened to use.

5. **76 of 2,036 desks carry no `created_at` at all** — the Postgres
   `DEFAULT now()` did not survive the cutover
   (`scripts/mongo_default_gaps.py --all`). Mongo sorts a missing field below
   every date, so `sort=[("created_at", -1)]` can never select one: a cycle
   written without a stamp is invisible to `_newest_cycle()` and the script
   would verify an older cycle and print a clean grid. Today those 76 are four
   hand-run cycles from 2026-08-18, all older than the newest stamped cycle,
   so the pick stands — and `_stamp_note()` now says so instead of leaving it
   assumed.

HOW THESE ARE KNOWN TO BE RED
-----------------------------
Eleven of the thirteen tests below fail against
`git show 77e6dc3:scripts/verify_audit_phases.py` — no `_as_desk`, no
`_newest_cycle`, no `_stamp_note`, and `_desks` goes through the pool and never
touches `mongo_query`, so the recorders see zero calls.

The two that do NOT go red there are the empty-store pair, and the reason is
worth writing down: conftest's autouse `patch_get_db` hands the old code a mock
cursor whose `fetchall()` is empty, so it takes the same early-exit branch and
prints the same line. They are here for trap 7 — an empty read must exit 1, not
print a clean grid — not as evidence of the port.

Because "the old file explodes" is a weak reason for a test to be red, each was
also run against deliberately broken copies of the NEW code — `_as_desk`
returning its argument unchanged, `_desks` sorting DESC, `_desks` handing
`mongo_query` an already-resolved collection name, `_newest_cycle` sorting
ASC / on `updated_at` / dropping the stamp column, `_stamp_note` returning "",
the stamp-less probe matching on `created_at: null` instead of
`$exists: False`, and the empty-read early exit deleted. All nine are caught.

The resolved-name mutant was NOT caught by the first version of this file:
asserting `collection == "shared_desk"` passes for a caller that already called
`collection_for`, because that function is the identity today. It is caught now
by switching the map on in the test.
"""
from __future__ import annotations

import io
import json
import re
import sys
from contextlib import redirect_stdout, redirect_stderr
from datetime import datetime
from pathlib import Path

import pytest

from scripts import verify_audit_phases as vap

SOURCE = Path(vap.__file__).read_text(encoding="utf-8")

# The exact grep the port is measured by.
_ARCHIVE_COUPLING = re.compile(r"psycopg|DATABASE_URL|pg_connection|dbname=|postgres")


@pytest.fixture(autouse=True)
def _fresh_results():
    """`_results` is module-global; a leftover row would leak between tests."""
    vap._results.clear()
    yield
    vap._results.clear()


def test_the_script_has_no_archive_coupling():
    hits = [f"{n}: {line.strip()}"
            for n, line in enumerate(SOURCE.splitlines(), 1)
            if _ARCHIVE_COUPLING.search(line)]
    assert hits == [], (
        "verify_audit_phases still reaches the frozen SQL archive:\n" + "\n".join(hits))


# ── the two reads ─────────────────────────────────────────────────────────

class _Rows:
    """Stands in for a `mongo_query` reader and remembers how it was called."""

    def __init__(self, by_collection: dict):
        self.by_collection = by_collection
        self.calls: list[dict] = []

    def __call__(self, collection, query, columns, sort=None, limit=0):
        self.calls.append({"collection": collection, "query": query,
                           "columns": list(columns), "sort": sort, "limit": limit})
        return self.by_collection.get(collection, [])


def test_desks_are_read_from_mongo_by_table_name_in_ticker_order(monkeypatch):
    rec = _Rows({"shared_desk": [("AAA", {}), ("BBB", {})]})
    monkeypatch.setattr("app.db.mongo_query.find_rows", rec)

    assert vap._desks("cycle-x") == [("AAA", {}), ("BBB", {})]

    assert len(rec.calls) == 1, "one cycle's desks is one read"
    call = rec.calls[0]
    # The POSTGRES TABLE NAME. `mongo_query` runs it through `collection_for()`
    # itself, exactly once; passing an already-resolved name resolves twice and
    # the day renames are switched on the read misses silently.
    assert call["collection"] == "shared_desk"
    assert call["query"] == {"cycle_id": "cycle-x"}
    assert call["columns"] == ["ticker", "desk_data"]
    # `ORDER BY ticker` — the header prints the tickers in this order and the
    # desk dict is built from it.
    assert call["sort"] == [("ticker", 1)]


def test_both_reads_survive_the_renames_being_switched_on(monkeypatch):
    """The behavioural half of the double-resolution rule.

    `tests/unit/test_no_double_collection_resolution.py` catches the shape by
    AST across the whole repo. It cannot fail on the VALUE, because
    `collection_for` is the identity while `renames_active()` is False — which
    is precisely why asserting `collection == "shared_desk"` passes for a
    caller that already resolved the name. So switch the map on: a reader that
    resolves the name itself now hands `mongo_query` a name it will resolve
    again, the read misses, and a write would create an invisible second
    collection.
    """
    monkeypatch.setattr("app.db.collections.collection_for",
                        lambda table: f"renamed_{table}")
    rows = _Rows({"shared_desk": [("AAA", {})]})
    one = _Rows({"shared_desk": [("cycle-x", None)]})

    def _find_row(collection, query, columns, sort=None, limit=0):
        one.calls.append({"collection": collection})
        return ("cycle-x", None)

    monkeypatch.setattr("app.db.mongo_query.find_rows", rows)
    monkeypatch.setattr("app.db.mongo_query.find_row", _find_row)

    vap._desks("cycle-x")
    vap._newest_cycle()

    assert rows.calls[0]["collection"] == "shared_desk"
    assert one.calls[0]["collection"] == "shared_desk", (
        "a resolved name here is resolved a second time inside mongo_query")


def test_the_newest_cycle_is_the_newest_by_created_at(monkeypatch):
    stamp = datetime(2026, 8, 30, 7, 21, 55)

    def _one(collection, query, columns, sort=None, limit=0):
        _one.call = {"collection": collection, "query": query,
                     "columns": list(columns), "sort": sort}
        return ("cycle-v3-1788074145", stamp)

    monkeypatch.setattr("app.db.mongo_query.find_row", _one)

    assert vap._newest_cycle() == ("cycle-v3-1788074145", stamp)
    assert _one.call["collection"] == "shared_desk"
    assert _one.call["query"] == {}
    # DESC. Ascending returns 2026-06-18, the OLDEST desk in the collection,
    # and every phase below would then be graded on a ten-week-old cycle.
    assert _one.call["sort"] == [("created_at", -1)], (
        "the newest cycle is ORDER BY created_at DESC LIMIT 1; sorting on "
        "updated_at or ascending picks a different cycle and says nothing")
    assert "created_at" in _one.call["columns"], (
        "the stamp has to come back too, or the caller cannot say how old the "
        "cycle it just verified is")


def test_no_cycles_at_all_exits_1(monkeypatch):
    monkeypatch.setattr("app.db.mongo_query.find_row", lambda *a, **k: None)
    monkeypatch.setattr(sys, "argv", ["verify_audit_phases.py"])
    out = io.StringIO()
    with redirect_stdout(out), redirect_stderr(io.StringIO()):
        rc = vap.main()
    assert rc == 1
    assert out.getvalue().strip() == "no cycles found"


# ── desk_data arrives in two shapes, and the recent one is a string ───────

def _desk(action=None):
    return {
        "cycle_metadata": {"bot_id": "bot-7", "held": True,
                           "quant_math_context": "HRP weights: ..."},
        "regime_classification": {"regime": "DEEP_DISCOUNT",
                                  "forward_call": {"basis": "low"}},
        "desk_note": {"triage_recommendation": "DEEP", "catalyst_call": "earnings"},
        "fundamental_report": {"horizon": "12m", "near_term_read": "soft"},
        "quant_report": {"_model_reported_metrics": {}},
        "final_decision": {"action": action, "decision_provenance": "board_verdict"},
    }


def test_json_text_desk_data_is_parsed():
    payload = _desk("BUY")
    assert vap._as_desk(json.dumps(payload)) == payload, (
        "274 of 2,036 documents — every one written since the cutover — store "
        "desk_data as JSON TEXT; handing the string straight through makes "
        "every `.get()` below an AttributeError")
    assert vap._as_desk(payload) is payload
    assert vap._as_desk(None) == {}
    assert vap._as_desk("") == {}


def _run(monkeypatch, desk_rows, tr_rows=()):
    """main() over a fixed cycle, with both Mongo reads stubbed."""
    rec = _Rows({"shared_desk": list(desk_rows), "trade_results": list(tr_rows)})
    monkeypatch.setattr("app.db.mongo_query.find_rows", rec)
    monkeypatch.setattr(sys, "argv",
                        ["verify_audit_phases.py", "--cycle", "cycle-x"])
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        rc = vap.main()
    return rc, out.getvalue()


def test_the_grid_is_identical_whichever_shape_desk_data_arrives_in(monkeypatch):
    """The archive half and the live half of the SAME collection must grade alike.

    This is the test the two shapes exist for: a port that reads only the
    backfilled subdocuments passes every archive cycle and crashes on every
    cycle written since 2026-08-19.
    """
    payload = _desk("BUY")
    saved = [("JPM", "BUY", "board_verdict")]   # the trade_results half
    rc_obj, out_obj = _run(monkeypatch, [("JPM", payload)], saved)
    vap._results.clear()
    rc_str, out_str = _run(monkeypatch, [("JPM", json.dumps(payload))], saved)

    assert rc_obj == rc_str == 0
    assert out_obj == out_str, (
        "the same desk graded differently depending on whether it was stored "
        "as a subdocument or as JSON text")
    assert "bot_id is populated on the desk" in out_obj
    assert "❌" not in out_obj, out_obj


def test_a_desk_with_no_action_still_fails_the_decision_check(monkeypatch):
    """The grid's verdicts are not an artefact of the port: a real FAIL exits 1."""
    rc, out = _run(monkeypatch, [("JPM", json.dumps(_desk(None)))])
    assert rc == 1
    assert "every ticker produced a decision" in out
    assert "❌" in out


def test_an_empty_cycle_exits_1_rather_than_grading_nothing(monkeypatch):
    """Trap 7: a run that returns [] must say so, not print a clean grid."""
    rc, out = _run(monkeypatch, [])
    assert rc == 1
    assert out.strip() == "no desks for cycle cycle-x"


# ── the stamp-less desks the newest-cycle sort cannot see ─────────────────

def _stamp(monkeypatch, n, newest):
    monkeypatch.setattr("app.db.mongo_query.agg_row",
                        lambda *a, **k: (n, newest))


def test_stampless_desks_are_reported_not_silently_skipped(monkeypatch):
    """76 of 2,036 rows have no created_at, so the sort cannot reach them."""
    _stamp(monkeypatch, 76, datetime(2026, 8, 18, 7, 9, 31))
    note = vap._stamp_note(datetime(2026, 8, 30, 7, 21, 55))
    assert note, (
        "silence here is the failure: a sort that cannot see 76 documents "
        "should say which 76 it could not see")
    assert "76" in note and not note.startswith("WARNING")


def test_a_stampless_desk_newer_than_the_pick_is_a_warning(monkeypatch):
    """The case that makes the chosen cycle the wrong one."""
    _stamp(monkeypatch, 3, datetime(2026, 8, 31, 12, 0, 0))
    note = vap._stamp_note(datetime(2026, 8, 30, 7, 21, 55))
    assert note.startswith("WARNING"), note
    assert "--cycle" in note, "a warning with no remedy is a log line"


def test_no_stampless_desks_means_no_note(monkeypatch):
    """And the note is not unconditional — otherwise it says nothing."""
    _stamp(monkeypatch, 0, None)
    assert vap._stamp_note(datetime(2026, 8, 30, 7, 21, 55)) == ""


def test_the_stampless_probe_counts_desks_with_no_created_at(monkeypatch):
    seen = {}

    def _agg(collection, query, aggs):
        seen.update(collection=collection, query=query, aggs=list(aggs))
        return (76, datetime(2026, 8, 18, 7, 9, 31))

    monkeypatch.setattr("app.db.mongo_query.agg_row", _agg)
    assert vap._unstamped_desks() == (76, datetime(2026, 8, 18, 7, 9, 31))
    assert seen["collection"] == "shared_desk"
    # `{"$ne": None}` matches neither a null nor a MISSING field, so the
    # complement has to be spelled `$exists: False` or the probe counts zero
    # and reports all-clear forever.
    assert seen["query"] == {"created_at": {"$exists": False}}
    assert seen["aggs"] == [("count", None), ("max", "updated_at")]
