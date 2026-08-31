"""`scripts/verify_fidelity_fixes.py` must grade the cycles that EXIST.

It is the acceptance harness for the 2026-07-28 fidelity + accounting fixes,
and its value comes from having been written before the verification cycle ran
— the pass criteria could not be retrofitted to whatever the cycle produced.
That property survives only if two things stay true: the criteria are not
edited, and the harness can still see a cycle.

The second one had already stopped being true. Measured against both stores on
2026-08-30:

* Its two reads went to Postgres, which stopped being written at the
  2026-08-19 cutover. `shared_desk` holds 1762 rows there (newest
  2026-08-19 22:44:43 UTC) against 2036 in Mongo (newest 2026-08-30 07:21:55);
  `decision_outcomes` 2637 against 2693.
* Loaded in-process and pointed at three real post-cutover cycles
  (`cycle-v3-1787232600`, `-1787626597`, `-1787786020`), the pre-port `_desks`
  returned 0, 0 and 0 desks — so `main()` printed "no desks for this cycle —
  nothing to verify" and exited 1 for all 201 desks (96 cycles) written since,
  which is indistinguishable from the fixes having regressed. On the
  pre-cutover `cycle-v3-1785418200` it returned 9, so nothing about the output
  said which of the two had happened.
* `desk_data` is the trap underneath that: `$type` says 1762 documents hold a
  subdocument and 274 hold JSON **TEXT**, no cycle mixes the two, and the text
  ones are the recent ones — 195 of the 201 post-cutover desks, over 94
  cycles. A port that fetched the field but dropped the `json.loads` branch
  would return [] for every cycle worth running today, report SKIP on every
  check, and exit 0.

Equivalence, so the port is not a rewrite: reading the desks from Postgres and
from Mongo and feeding both into the SAME seven check functions over six
pre-cutover cycles (70 desks) gives byte-identical (status, detail) pairs for
all 42 (check, cycle) pairs, and identical `(ticker, overridden_from)` tuples
for X1 on every one.

Which of these fail against `git show HEAD:scripts/verify_fidelity_fixes.py`,
verified by loading that exact source and re-running them:

    test_the_script_has_no_postgres_coupling            RED (2 pg_connection lines)
    test_desks_reads_shared_desk_from_mongo             RED (find_rows never called)
    test_desks_parses_the_post_cutover_json_text_shape  RED (reads Postgres instead)
    test_overrides_are_read_from_decision_outcomes      RED (find_rows never called)
    test_an_unrecorded_override_is_still_a_failure      RED (reads Postgres instead)
    test_the_pre_registered_criteria_are_unchanged      GREEN before and after —
        it is a ratchet on the pass criteria, not a check on the port, and it
        is here because "the criteria were written first" is the only reason
        this file's verdicts mean anything.
"""
from __future__ import annotations

import importlib.util
import json
import pathlib
import re
from unittest.mock import patch

REPO = pathlib.Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts" / "verify_fidelity_fixes.py"


def _load():
    """The script, loaded by path.

    `SCRIPT` is read at call time, not bound as a default, so the negative
    control can point this at `git show HEAD:` output and re-run every test
    below against the pre-port source.
    """
    spec = importlib.util.spec_from_file_location("vff_under_test", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_the_script_has_no_postgres_coupling():
    """The archive still ANSWERS, so a leftover import does not fail — it lies.

    `scripts/migration/pg_connection.get_db` was repaired on 2026-08-30 to
    resolve the archive DSN by name, which means the pre-port file connects
    cleanly and returns July rows. A grep is the only thing that catches that.
    """
    hits = [
        f"{i}: {line.strip()}"
        for i, line in enumerate(SCRIPT.read_text(encoding="utf-8").splitlines(), 1)
        if re.search(r"psycopg|DATABASE_URL|pg_connection|dbname=|postgres", line)
    ]
    assert not hits, hits


def test_desks_reads_shared_desk_from_mongo():
    """One read, against the TABLE name, filtered only on `cycle_id`.

    The table name and not `collection_for("shared_desk")`: every mongo_query
    helper resolves it internally exactly once, and a pre-resolved name is
    resolved twice (see tests/unit/test_no_double_collection_resolution.py).

    And only on `cycle_id`: `desk_data` is JSON text on every current desk, so
    a filter on `desk_data.<anything>` would match 0 of them while the value
    sits in the string.
    """
    vff = _load()
    from app.db import mongo_query

    with patch.object(mongo_query, "find_rows", return_value=[]) as fr:
        assert vff._desks("cycle-X") == []

    assert fr.call_count == 1, "the desks must come from one Mongo read"
    collection, query, columns = fr.call_args[0][:3]
    assert collection == "shared_desk"
    assert query == {"cycle_id": "cycle-X"}
    assert list(columns) == ["desk_data"]
    assert not [k for k in query if "." in k], (
        f"a dotted path into desk_data matches nothing — it is JSON text: {query}"
    )


def test_desks_parses_the_post_cutover_json_text_shape():
    """Both shapes survive, and a row that is neither is dropped, not raised.

    All 274 string values in `shared_desk` parse to a dict today, so the
    unparseable branch is not exercised by live data — it is here because the
    SQL version had it and dropping a malformed row is not the same as
    crashing the whole acceptance run on one.
    """
    vff = _load()
    from app.db import mongo_query

    rows = [
        ({"ticker": "AAA", "phase": "PM_DONE"},),      # pre-cutover: subdocument
        (json.dumps({"ticker": "BBB", "phase": "PM_DONE"}),),  # post-cutover: TEXT
        ("{not json at all",),                          # malformed -> skipped
        (None,),                                        # NULL       -> skipped
        (json.dumps([1, 2, 3]),)                        # JSON, not a desk -> skipped
    ]
    with patch.object(mongo_query, "find_rows", return_value=rows):
        desks = vff._desks("cycle-X")

    assert [d["ticker"] for d in desks] == ["AAA", "BBB"], (
        "the json.loads branch is what nearly every desk written since "
        "2026-08-18 19:44 needs — 195 of the 201 post-cutover ones"
    )


def test_overrides_are_read_from_decision_outcomes():
    """X1's second read, and the pairing it asserts.

    `overridden_from` carries the BOARD's action; the desk's `trade_decision`
    is what the Board said and `final_decision` is what the synthesizer issued,
    so a recorded override matches the desk's `final_decision.action`. Verified
    on live data: `cycle-v3-1785418200` desk LRCX has final=BUY / trade=HOLD
    and its `decision_outcomes` row is action=HOLD, overridden_from='BUY'.
    """
    vff = _load()
    from app.db import mongo_query

    desks = [{"ticker": "LRCX",
              "final_decision": {"action": "BUY"},
              "trade_decision": {"action": "HOLD"}}]
    calls = []

    def fake(collection, query, columns, *a, **kw):
        calls.append((collection, query, list(columns)))
        return [("LRCX", "BUY"), ("CPS", None)]

    with patch.object(mongo_query, "find_rows", side_effect=fake):
        status, detail = vff.check_overrides_recorded("cycle-X", desks)

    assert calls == [("decision_outcomes", {"cycle_id": "cycle-X"},
                      ["ticker", "overridden_from"])]
    assert (status, detail) == (vff.PASS,
                                "1 override(s) recorded with the Board's action")


def test_an_unrecorded_override_is_still_a_failure():
    """The whole point of X1, and the shape trap 3 would have hidden.

    `overridden_from` has no Postgres DEFAULT, so a post-cutover document may
    simply lack the field; `find_rows` yields None for it, exactly as the SQL
    yielded NULL. Nothing here filters with `{"$ne": None}`, which would match
    neither a null nor a missing field and would have turned every silently
    unrecorded override into an empty result and a SKIP.
    """
    vff = _load()
    from app.db import mongo_query

    desks = [{"ticker": "LRCX",
              "final_decision": {"action": "BUY"},
              "trade_decision": {"action": "HOLD"}}]

    with patch.object(mongo_query, "find_rows", return_value=[("LRCX", None)]):
        status, detail = vff.check_overrides_recorded("cycle-X", desks)

    assert status == vff.FAIL, (status, detail)
    assert "LRCX" in detail


def test_the_pre_registered_criteria_are_unchanged():
    """A ratchet on the thresholds, not a check on the port.

    This file's verdicts are worth something only because the criteria were
    fixed before the verification cycle ran. Moving the store is not a licence
    to move a threshold, and a threshold that drifts later should have to
    explain itself here first.
    """
    vff = _load()
    src = SCRIPT.read_text(encoding="utf-8")

    assert [label for label, _ in vff.CHECKS] == [
        "P1  FA emits a metrics block",
        "P1  FA metrics match stored data",
        "P2  max_drawdown_est is computed",
        "P3  synthesizer sees verified blocks",
        "Q4  no impossible/distorted multiples",
        "Q1  bad denominators withheld everywhere",
        "--  no prose-only decisive agent",
    ]

    for criterion in (
        # P1: a stated metric may differ from the stored one by 2%.
        'if abs(stated - verified) / max(abs(verified), 1e-9) > 0.02:',
        # P2: 5% on the drawdown, and the literal prompt placeholder.
        'if abs(stated - verified) / max(abs(verified), 1e-9) > 0.05:',
        'placeholder = [t for t, v in vals if v == 12.5]',
        'if len(placeholder) > 1:',
        # Q4: EV/EBIT must sit above EV/EBITDA and inside the wedge.
        'if ratio < 1.0 or ratio > 3.0:',
        # Q1: the withheld multiple must take both EBIT-derived siblings with it.
        'if "NOT MEANINGFUL" not in str(nc.get("ev_to_ebit", "")):',
        'for sibling in ("implied_growth_pct", "net_debt_to_ebit"):',
        # The structural claim, and the metadata fields that do not count.
        'meta = {"confidence", "_quality_score", "quality_score"}',
        # SKIP never fails the run; FAIL always does.
        "return 1 if n_fail else 0",
    ):
        assert criterion in src, f"a pre-registered pass criterion changed: {criterion}"
