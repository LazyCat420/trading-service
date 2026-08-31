"""`scripts/simplification_baseline.py` must read the store the desks are in.

WHY THIS FILE EXISTS
--------------------
The baseline is the instrument the whole simplification wave is graded on: a
stage is allowed to move LOC only if the behavioural distributions hold. It
read Postgres, which froze at the 2026-08-19 cutover, so from that day it
answered every capture with the SAME numbers, and `--diff` printed

    (no behavioural change — a clean structural-only stage)

for any stage whatsoever. A verification tool that cannot fail is worse than
none, because it reports success in detail, with numbers.

HOW THESE TESTS ARE CHOSEN
--------------------------
By MUTATION, not by reading the code and asserting what it says. Each of the
six numbers the snapshot carries was broken in turn and the suite re-run; a
mutation that stayed green got a test. The four the first version of this file
missed are named on the tests that now cover them (the guardrail total, the
artifact scan, the band WIDTH, and NULLS-LAST against a genuine 0.0).

Everything here drives a stub seam except `test_the_live_store_answers_every_
read_this_script_makes`, which is the one thing a stub cannot check: whether
the collection names the script uses exist in the store it is pointed at. Mongo
creates nothing on a read, so a mistyped name returns no rows, and the script
would compile, run, print `desks=0` and report a clean structural-only stage.
That is trap 7 and it is the failure this whole port exists to prevent, so it
gets a live probe — read-only, and skipped when Mongo is unreachable.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from scripts import simplification_baseline as sb  # noqa: E402
from scripts.gate_zero_pg import scan  # noqa: E402

REL = "scripts/simplification_baseline.py"


class FakeSeam:
    """A `mongo_query` stand-in. Returns tuples in SELECT order, as the real one does."""

    def __init__(self, *, rows=None, groups=None, guardrails=0, dated=None, kept=None):
        self.rows = rows or []
        self.groups = groups or {}
        self.guardrails = guardrails
        self.dated = dated or {}
        self.kept = kept or {}
        self.windows = []
        self.collections = []
        self.agg_calls = []

    def _seen(self, collection, query):
        self.collections.append(collection)
        self.windows.append(query)

    def find_rows(self, collection, query, columns, sort=None, limit=0):
        self._seen(collection, query)
        return list(self.rows)

    def group_rows(self, collection, query, keys, aggs, select, sort=None, limit=0):
        self._seen(collection, query)
        return list(self.groups.get(collection, []))

    def agg_row(self, collection, query, aggs):
        self._seen(collection, query)
        self.agg_calls.append((collection, tuple(aggs)))
        return (self.guardrails,)

    def count(self, collection, query=None):
        query = query or {}
        self._seen(collection, query)
        table = self.kept if "$nor" in query else self.dated
        return table.get(collection, 0)


def test_the_file_has_no_sql_coupling_left():
    """RED BEFORE: `scan()` reported 9 findings here — `import psycopg2` plus the
    eight `cur.execute(<sql>)` calls. Measured on the HEAD copy, 2026-08-30."""
    result = scan(REPO, targets=(REL,))
    assert result["errors"] == [], result["errors"]
    assert result["total"] == 0, (
        "still reads the SQL archive: "
        + "; ".join(f"{f['kind']} at line {f['line']}" for f in result["findings"][:5]))


def test_the_window_pins_the_instrumented_date_and_cuts_the_2026_08_18_burst():
    """The two decisions the window makes — and an honest note about a third.

    WHAT IT GUARDS. (a) The lower bound is INSTRUMENTED_FROM, the date the
    labels this baseline reads first existed. Move it and every distribution in
    the snapshot moves: 2026-07-01 instead of 2026-07-26 counts desks whose
    policy_action column was still NULL. (b) The 2026-08-18 19:44–19:50 hole is
    what keeps the 67 documents the unit suite wrote into the production
    database out of the baseline; the live probe below checks it removes
    exactly those 67 and no more.

    WHAT IT DOES NOT GUARD, said plainly because the first version of this file
    claimed otherwise. The bound being a `datetime` rather than the ISO string
    the SQL passed changes NO number in this codebase: every read reaches the
    server through `mongo_store`, which runs the filter through
    `date_fields.coerce_filter`, and `created_at` is a registered timestamp
    field on all four of these collections, so the string is parsed to exactly
    this datetime. Measured 2026-08-30 by forcing `_window()` to return the
    bare string: byte-identical snapshot, 759 desks either way. The type is
    asserted below as a CONTRACT — this file must not depend on a registry it
    does not own — and it is labelled as a contract rather than dressed up as a
    behaviour guard whose number cannot move.
    """
    w = sb._window()
    gte = w["created_at"]["$gte"]
    assert gte == datetime(2026, 7, 26), "the instrumented-from bound moved"
    assert sb.INSTRUMENTED_FROM == "2026-07-26"
    # contract, not behaviour — see the docstring
    assert isinstance(gte, datetime), f"window bound is {type(gte).__name__}"

    hole = w["$nor"][0]["created_at"]
    assert hole == {"$gte": datetime(2026, 8, 18, 19, 44),
                    "$lt": datetime(2026, 8, 18, 19, 50)}, hole
    # the dated window is the same read WITHOUT the hole, and the two are
    # differenced to report what the hole removed
    assert "$nor" not in sb._date_window()
    assert sb._date_window()["created_at"] == w["created_at"]


def test_every_collection_it_reads_is_a_table_the_store_map_knows():
    """TRAP 7, pinned without a database.

    RED BEFORE (and red under mutation now): renaming `shared_desk` to
    `shared_desks` at any of the four call sites left all eight of the previous
    tests GREEN. Mongo creates nothing on a read, so the script kept running
    and simply reported an empty baseline. `collections.is_mapped()` reads the
    hand-authored, machine-validated table→collection map, so a name that is
    not a real table fails here with no store involved; the second half checks
    the reads actually go to those four names and nowhere else.
    """
    from app.db import collections as collection_map

    assert len(sb.COLLECTIONS) == 4
    for name in sb.COLLECTIONS:
        assert collection_map.is_mapped(name), (
            f"{name!r} is not a table in app/db/collection_map.json — a read of it "
            "returns nothing and the baseline silently goes empty")

    mq = FakeSeam()
    sb.behavioural(mq)
    assert set(mq.collections) == set(sb.COLLECTIONS), (
        f"reads went to {sorted(set(mq.collections))}, expected {sorted(sb.COLLECTIONS)}")


def test_a_desk_stored_as_json_text_is_read_like_one_stored_as_a_subdocument():
    """THE TRAP. `shared_desk.desk_data` is a subdocument for every migrated
    desk and a JSON **string** for every desk written since the cutover (1762
    vs 274 on 2026-08-30). A Mongo filter on
    `desk_data.final_decision.confidence` — the literal transcription of the
    old jsonb path — therefore reads only the FROZEN half, drops every live
    desk, and reports a baseline that cannot move.

    RED BEFORE: the confidence bands and the >=70 floor came from two SQL
    statements against `desk_data->'final_decision'->>'confidence'`; nothing in
    this repo asserted that the text form is counted too.

    The confidences are 67 and 72 and not 61 and 72, which is what the first
    version used: 61 and 72 land in different bands under 5-point AND under
    10-point banding, so `floor(c/5)*5` mutated to `floor(c/10)*10` kept the
    test green. 67 separates them — band 65 five-wide, band 60 ten-wide — so
    the band WIDTH is now pinned and not just the banding.
    """
    as_object = {"final_decision": {"confidence": 72.0}, "quant_report": {"x": 1}}
    as_text = json.dumps({"final_decision": {"confidence": 67.0}, "quant_report": {"x": 2}})
    unparseable = "{not json"
    mq = FakeSeam(rows=[(as_object,), (as_text,), (unparseable,)])

    desks = sb._desks(mq)
    assert len(desks) == 2, "the JSON-text desk was dropped"

    bands, ge70, total = sb._confidence(desks)
    assert total == 2, "one of the two shapes did not reach the histogram"
    assert bands == {"65": 1, "70": 1}, "5-point bands, not 10-point"
    assert ge70 == 1


def test_artifact_presence_counts_populated_subdocuments_and_nothing_else():
    """Control 2 of the HOLD decomposition, and it had NO test.

    RED BEFORE: relaxing `isinstance(v, dict) and v` to a bare `if v:` changed
    all 16 percentages in the live snapshot and left every test green. Two
    distinct mistakes hide in that one line:

    * without `isinstance(v, dict)` the scalar columns every desk carries —
      `ticker`, `phase`, `desk_id` — each read as an artifact that is present
      on 100% of desks, and every real artifact's percentage is diluted by
      them;
    * without `and v` an artifact that was ATTEMPTED and came back `{}` counts
      as present, which is exactly the regression this control exists to see —
      presence holding flat while confidence falls is the "honest drop"
      verdict.
    """
    desks = [
        {"ticker": "AAPL", "phase": 3, "desk_id": "d1",
         "quant_report": {"pe": 1}, "delta_report": {}},
        {"ticker": "MSFT", "phase": 3, "desk_id": "d2",
         "quant_report": {"pe": 2}, "delta_report": {"d": 1}},
    ]
    pct = sb._artifact_presence(desks)
    assert pct == {"quant_report": 100.0, "delta_report": 50.0}, pct
    assert sb._artifact_presence([]) == {}


def test_the_guardrail_total_comes_from_the_guardrail_collection():
    """RED BEFORE: replacing the whole `agg_row(...)` call with the literal `0`
    left every test green — the only test that ran `behavioural()` did it with
    a stub counting 0, so it could not tell a read from a constant. One of the
    six behavioural invariants was completely unguarded."""
    mq = FakeSeam(guardrails=41)
    out = sb.behavioural(mq)
    assert out["guardrail_firings_total"] == 41
    assert mq.agg_calls == [(sb.GUARDRAILS, (("count", None),))], mq.agg_calls
    assert mq.agg_calls[0][1] == (("count", None),), "COUNT(*), not COUNT(col)"


def test_a_distribution_folds_two_groups_that_render_to_the_same_key():
    """RED BEFORE: the SQL result went straight into a dict comprehension. SQL
    put NULL in one group, so that was safe; a `$group` need not, and the
    second row would then OVERWRITE the first and take its rows out of the
    total silently — a distribution short a few rows still looks like a
    distribution."""
    mq = FakeSeam(groups={"trade_results": [(None, 4), (None, 2), ("HOLD", 9)]})
    assert sb._distribution(mq, sb.TRADES, "policy_action") == {"None": 6, "HOLD": 9}


def test_agent_seconds_rounds_the_way_the_sql_did_and_ranks_a_real_zero_above_a_null():
    """RED BEFORE: `round(avg(elapsed_ms)/1000.0, 1)` in Postgres is half AWAY
    FROM ZERO; Python's built-in `round()` is half-to-even, so 0.25 s would be
    recorded as 0.2 where the archive says 0.3. And `ORDER BY 3 DESC NULLS
    LAST` has to survive the port, or an agent with no timings jumps to the top
    of the cost table.

    The `zero` row is the fix for a mutation the first version missed: the sort
    key collapsed to `r[2] if r[2] is not None else 0.0` — NULLs treated as
    0.0 — stayed green, because the only NULL in the fixture competed with 0.3
    and 0.2 and landed last either way. It is not academic. On the live window
    two agents average a genuine 0.0 and two more have no timings at all, so
    the collapse makes four rows tie and their order fall out of input order.
    `no_timings` is fed to the seam BEFORE `zero` so the mutation's stable sort
    hands back the wrong one of the two.
    """
    mq = FakeSeam(groups={"v3_agent_telemetry": [
        ("slow", 3, 250.0),       # 0.25 s -> 0.3, not 0.2
        ("no_timings", 1, None),  # deliberately ahead of `zero`
        ("zero", 1, 0.0),         # a real 0.0 outranks a NULL
        ("fast", 2, 150.0),       # 0.15 s -> 0.2, not 0.1
    ]})
    rows = sb._agent_seconds(mq)
    assert [r[0] for r in rows] == ["slow", "fast", "zero", "no_timings"]
    assert rows[0][2] == 0.3
    assert rows[1][2] == 0.2
    assert rows[2][2] == 0.0
    assert rows[3][2] is None


def test_behavioural_records_which_store_it_read_and_what_the_burst_removed():
    mq = FakeSeam(guardrails=7,
                  dated={"trade_results": 100, "shared_desk": 200,
                         "v3_agent_telemetry": 300, "v3_guardrail_firings": 400},
                  kept={"trade_results": 96, "shared_desk": 197,
                        "v3_agent_telemetry": 296, "v3_guardrail_firings": 344})
    out = sb.behavioural(mq)
    assert out["store"] == "mongo"
    assert out["window_from"] == sb.INSTRUMENTED_FROM
    # the burst report is dated minus kept, per collection — not a constant
    assert out["test_burst_excluded"] == {"trade_results": 4, "shared_desk": 3,
                                          "v3_agent_telemetry": 4,
                                          "v3_guardrail_firings": 56}
    # every read that takes the window took the SAME window
    reads = [w for w in mq.windows if "created_at" in w and "$nor" in w]
    assert reads, "no read used the burst-excluded window"
    assert all(w == sb._window() for w in reads)


def test_diff_refuses_to_difference_two_different_stores(tmp_path, capsys, monkeypatch):
    """RED BEFORE: `diff()` had no idea where a snapshot came from. Differencing
    a July snapshot of the frozen archive against a live Mongo one attributes
    the whole migration — 86 HOLDs -> 506, 112 desks -> 762 — to whatever stage
    happened to run in between.

    RED AFTER THE FIRST FIX TOO, which is why this test changed: the first
    version printed a banner and then printed the entire meaningless delta
    underneath it, still set `changed`, and still returned 0. A warning above
    the numbers does not stop anyone reading the numbers. So the assertions are
    now that the delta is NOT produced and the exit status says the diff did
    not happen."""
    monkeypatch.setattr(sb, "OUT_DIR", tmp_path)
    before = {"label": "before", "captured_at": "", "git_head": "aaa",
              "structural": {"app_loc": 1}, "behavioural": {
                  "window_from": "2026-07-26", "action_distribution": {"HOLD": 86},
                  "policy_action_distribution": {}, "provenance_distribution": {},
                  "board_confidence_bands": {}, "artifact_presence_pct": {},
                  "board_confidence_at_or_above_floor": {"n_ge_70": 15, "n_total": 109}}}
    after = json.loads(json.dumps(before))
    after["label"] = "after"
    after["structural"]["app_loc"] = 2
    after["behavioural"]["store"] = "mongo"
    after["behavioural"]["action_distribution"] = {"HOLD": 506}
    after["behavioural"]["board_confidence_at_or_above_floor"] = {"n_ge_70": 151, "n_total": 719}
    (tmp_path / "before.json").write_text(json.dumps(before))
    (tmp_path / "after.json").write_text(json.dumps(after))

    assert sb.diff("before", "after") == 1, "an incomparable diff must not exit 0"
    out = capsys.readouterr().out
    assert "REFUSED" in out, out
    assert "pg-archive" in out and "mongo" in out, out
    # the structural half is read off the filesystem and stays comparable
    assert "app_loc" in out
    # ...the behavioural half is not printed at all
    assert "86" not in out and "506" not in out, out
    assert "151" not in out and "719" not in out, out
    assert "no behavioural change" not in out, out


def test_the_guard_stays_quiet_when_both_snapshots_read_the_same_store(tmp_path, capsys, monkeypatch):
    """NEGATIVE CONTROL: a warning that always prints is not a warning."""
    monkeypatch.setattr(sb, "OUT_DIR", tmp_path)
    snap = {"label": "x", "captured_at": "", "git_head": "aaa", "structural": {},
            "behavioural": {"window_from": "2026-07-26", "store": "mongo",
                            "action_distribution": {}, "policy_action_distribution": {},
                            "provenance_distribution": {}, "board_confidence_bands": {},
                            "artifact_presence_pct": {}, "test_burst_excluded": {},
                            "board_confidence_at_or_above_floor": {"n_ge_70": 0, "n_total": 0}}}
    (tmp_path / "a.json").write_text(json.dumps(snap))
    (tmp_path / "b.json").write_text(json.dumps(snap))
    assert sb.diff("a", "b") == 0
    out = capsys.readouterr().out
    assert "REFUSED" not in out
    assert "no behavioural change" in out


# ── the one thing a stub cannot answer ───────────────────────────────────────


@pytest.mark.real_mongo
def test_the_live_store_answers_every_read_this_script_makes():
    """TRAP 7 against the real store. READ-ONLY: counts and one `behavioural()`.

    `real_mongo` marks this as a deliberate production read so `tests/conftest
    .py::block_production_mongo` lets it through — that guard exists to stop
    ACCIDENTAL production access, and this baseline is an instrument pointed at
    production on purpose. Nothing here writes.

    RED BEFORE: nothing in this repo would have noticed the baseline going
    empty. Every other test drives a stub, so `find_rows("shared_desks", ...)`
    — one letter — reports 0 desks, empty bands, an empty artifact scan, and a
    `--diff` that says "a clean structural-only stage", with all tests green.
    The count assertions below fail on that mutation because Mongo answers a
    read of a collection nobody wrote with zero rows rather than an error.

    The 67 is pinned rather than checked for being positive: the burst window
    is a CLOSED interval six weeks in the past, so nothing can be added to it,
    and a count that moves means either the interval is wrong or somebody has
    been deleting archive rows. See TEST_BURST_FROM.
    """
    try:
        mq = sb._mongo()
        for name in sb.COLLECTIONS:
            n = mq.count(name, {})
    except Exception as exc:  # noqa: BLE001 — unreachable store is a skip, not a red
        pytest.skip(f"Mongo unreachable: {type(exc).__name__}: {exc}")

    empty = [c for c in sb.COLLECTIONS if mq.count(c, {}) == 0]
    assert not empty, (
        f"{empty} answered a read with zero documents — either the name is wrong "
        "or the collection is gone; the baseline would report an empty snapshot")

    # the lower bound is doing work: there are documents before it
    assert mq.count(sb.DESKS, sb._window()) < mq.count(sb.DESKS, {}), (
        "the window excludes nothing — the instrumented-from bound is not applying")

    excluded = {c: mq.count(c, sb._date_window()) - mq.count(c, sb._window())
                for c in sb.COLLECTIONS}
    assert excluded == {"trade_results": 4, "shared_desk": 3,
                        "v3_agent_telemetry": 4, "v3_guardrail_firings": 56}, excluded
    assert sum(excluded.values()) == 67

    out = sb.behavioural(mq)
    assert out["desks_sampled"] > 0, "no desks — see the collection names"
    assert out["guardrail_firings_total"] > 0
    assert out["action_distribution"], "empty action distribution"
    assert out["board_confidence_bands"], "no desk carried a Board confidence"
    assert out["artifact_presence_pct"], "no desk carried a populated artifact"
    assert out["agent_seconds"], "no agent telemetry in the window"
    assert out["test_burst_excluded"] == excluded
