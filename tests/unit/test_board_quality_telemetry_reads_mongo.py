"""`scripts/board_quality_telemetry.py` must read Mongo, and read it correctly.

WHY THIS FILE EXISTS
--------------------
This script is a *telemetry* tool: it exists to tell an operator where board
quality is falling. It connected to PostgreSQL, and PostgreSQL froze at the
2026-08-19 cutover. So it did not fail — it kept printing a panel headed
"last 14 days" filled with numbers that stopped moving, and it will keep doing
that until enough days pass that the frozen archive falls out of the window and
every panel goes quietly empty. A measuring instrument that reads a dead store
is worse than one that errors.

EVERY ASSERTION HERE WAS RED BEFORE THE PORT
--------------------------------------------
  * `test_no_postgres_coupling` — the old file did `import psycopg2` and
    `psycopg2.connect(os.environ["DATABASE_URL"])`; `gate_zero_pg.scan` counted
    2 couplings (driver_import at line 27, execute_call at line 39) and now
    counts 0.
  * every other test — the functions under test did not exist; the whole script
    was module-level statements against a cursor, so there was nothing to call.

The behavioural tests pin the three things a mechanical port of THIS script
gets wrong, each verified against the live stores on 2026-08-30:
  1. `evidence_gathering` / `pro_argument` / `con_argument` are JSON **STRINGS**
     in Mongo ($type "string" for 1131 and 846 documents respectively), so a
     dotted-path filter matches nothing while the value is right there in text.
  2. the regime join is on the PAIR (ticker, cycle_id) — `decision_evaluations`
     has 121 duplicate (ticker, cycle_id) groups, so a one-key join fans out.
  3. `ROUND(x::numeric, 2)` is half-up and keeps its trailing zeros;
     Python's `round()` is neither.
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from scripts import board_quality_telemetry as bqt  # noqa: E402
from scripts.gate_zero_pg import scan  # noqa: E402

SCRIPT = "scripts/board_quality_telemetry.py"


def test_no_postgres_coupling():
    """The instrument reads the store the system actually writes.

    RED BEFORE: `scan` reported total=2 for this file —
    `driver_import: import psycopg2` and `execute_call: .execute(<sql>)`.
    """
    result = scan(REPO, (SCRIPT,))
    assert result["total"] == 0, result["findings"]

    source = (REPO / SCRIPT).read_text(encoding="utf-8")
    for token in ("psycopg", "DATABASE_URL", "pg_connection", "dbname="):
        assert token not in source, f"{token!r} still in {SCRIPT}"


def test_failure_reason_reads_the_json_string_not_a_subdocument():
    """Trap 1: `evidence_gathering` is TEXT holding JSON, in Mongo as in the archive.

    A port that filtered/grouped on `evidence_gathering.failure_reason` would
    return 'none' for all 1131 live documents — a clean, plausible, wrong panel
    saying the board never fails.
    """
    as_stored = ('{"price_history": true, "news": true, '
                 '"failure_reason": "faithfulness_failure", "grounding_score": 0.549}')
    assert bqt._failure_reason(as_stored) == "faithfulness_failure"

    # COALESCE(..., 'none'): missing column, missing key and JSON null agree.
    assert bqt._failure_reason(None) == "none"
    assert bqt._failure_reason('{"news": true}') == "none"
    assert bqt._failure_reason('{"failure_reason": null}') == "none"
    # Postgres failed the whole panel on unparseable text; naming it is better.
    assert bqt._failure_reason("not json at all") == "(unparseable)"


def test_json_key_reads_the_winning_persona_out_of_a_string():
    """Same trap on debate_history: pro/con_argument are JSON strings too."""
    pro = '{"persona": "Macro_Quant", "claim": "...", "attack_points": []}'
    assert bqt._json_key(pro, "persona") == "Macro_Quant"
    assert bqt._json_key(pro, "nope") is None      # `->>` on a missing key is NULL
    assert bqt._json_key(None, "persona") is None
    assert bqt._json_key("{oops", "persona") is None


def test_round_is_half_up_and_keeps_its_trailing_zeros():
    """`ROUND(AVG(q)::numeric, 2)` — the printed column, not a float.

    `round(2.675, 2)` is 2.67 in Python (banker's) and 2.68 in Postgres, and
    `str(round(4.5, 2))` is "4.5" where the SQL printed "4.50". Both would have
    shifted this script's output away from the numbers the archive reported.
    """
    assert bqt._round(2.675, 2) == Decimal("2.68")
    assert str(bqt._round(4.5, 2)) == "4.50"
    assert str(bqt._round(0.0, 2)) == "0.00"
    assert bqt._round(None, 2) is None
    # ROUND(100.0 * n / NULLIF(total, 0), 1)
    assert str(bqt._pct(2, 63)) == "3.2"
    assert bqt._pct(0, 0) is None


def test_mean_matches_sql_avg_over_nulls():
    """AVG skips NULLs; an all-NULL group is NULL, not 0."""
    assert bqt._mean([4.0, None, 5.0]) == 4.5
    assert bqt._mean([None, None]) is None
    assert bqt._mean([]) is None


def test_window_boundary_is_utc_not_the_local_clock():
    """`NOW() - INTERVAL 'N days'` on a UTC server, against naive-UTC BSON dates.

    This host runs 7 h behind UTC. A naive `datetime.now()` here would move the
    boundary by that offset and silently add or drop rows at the edge of the
    window — a bug no row count looks wrong for.
    """
    since = bqt._window(14)
    expected = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=14)
    assert abs((since - expected).total_seconds()) < 5
    assert since.tzinfo is None

    # And it is NOT the local clock. Skipped rather than asserted on a UTC box,
    # where the two are identical and the check could prove nothing — a green
    # that depends on the box's timezone is a green that says nothing.
    local_offset = (datetime.now() - datetime.now(timezone.utc).replace(tzinfo=None))
    if abs(local_offset.total_seconds()) > 60:
        local_since = datetime.now() - timedelta(days=14)
        assert abs((since - local_since).total_seconds()) == pytest.approx(
            abs(local_offset.total_seconds()), abs=5)


def _fake_store(monkeypatch, de_docs, tr_docs):
    """Point the script's two reads at fixtures — never at production Mongo."""
    def find_docs(collection, query, **kwargs):
        if collection == bqt.DE:
            lo = query["timestamp"]["$gt"]
            hi = query["timestamp"].get("$lte")
            return [d for d in de_docs
                    if d["timestamp"] > lo and (hi is None or d["timestamp"] <= hi)]
        if collection == bqt.TR:
            return list(tr_docs)
        raise AssertionError(f"unexpected collection {collection!r}")

    monkeypatch.setattr(bqt.mongo_store, "find_docs", find_docs)


def test_regime_join_uses_the_composite_key_and_keeps_unmatched_rows(monkeypatch):
    """LEFT JOIN de -> tr ON (ticker, cycle_id) — both halves of that sentence.

    `mongo_query.left_join_rows` joins on ONE equality. Joining on ticker alone
    is the plausible port and it is wrong twice over: AAPL's two evaluations
    would each pick up BOTH cycles' regimes (4 rows out of 2), and the totals
    would stop matching the zero-score panel computed over the same window.
    Verified on live data: section 2's group sizes sum to the section-4 total
    (2 + 27 + 34 = 63 for the default window on 2026-08-30).
    """
    t0 = datetime(2026, 8, 20, 12, 0)
    de = [
        {"ticker": "AAPL", "cycle_id": "c1", "timestamp": t0, "final_quality_score": 5.0},
        {"ticker": "AAPL", "cycle_id": "c2", "timestamp": t0, "final_quality_score": 1.0},
        {"ticker": "MSFT", "cycle_id": "c9", "timestamp": t0, "final_quality_score": 3.0},
    ]
    tr = [
        {"ticker": "AAPL", "cycle_id": "c1", "regime": "DEEP_DISCOUNT"},
        {"ticker": "AAPL", "cycle_id": "c2", "regime": "CONTRADICTORY"},
        # MSFT/c9 has no trade_results row -> the LEFT JOIN must keep it.
    ]
    _fake_store(monkeypatch, de, tr)

    columns, rows = bqt.per_regime_quality(datetime(2026, 8, 1))
    assert columns == ["regime", "n", "mean_q"]
    assert {r[0]: (r[1], str(r[2])) for r in rows} == {
        "CONTRADICTORY": (1, "1.00"),
        "(unknown)": (1, "3.00"),
        "DEEP_DISCOUNT": (1, "5.00"),
    }
    # every evaluation is counted exactly once: no fan-out, no dropped row
    assert sum(r[1] for r in rows) == len(de)


def test_failure_panel_percentages_are_over_the_whole_window(monkeypatch):
    """`SUM(COUNT(*)) OVER ()` is the window total, not the group's own count."""
    t0 = datetime(2026, 8, 20, 12, 0)
    de = [
        {"timestamp": t0, "final_quality_score": 5.0, "evidence_gathering": '{"news": true}'},
        {"timestamp": t0, "final_quality_score": 4.0, "evidence_gathering": '{"news": true}'},
        {"timestamp": t0, "final_quality_score": 0.0,
         "evidence_gathering": '{"failure_reason": "parse_failure"}'},
        {"timestamp": t0, "final_quality_score": 0.0,
         "evidence_gathering": '{"failure_reason": "parse_failure"}'},
    ]
    _fake_store(monkeypatch, de, [])
    columns, rows = bqt.failure_reasons(datetime(2026, 8, 1))
    assert columns == ["failure_reason", "n", "pct", "mean_q"]
    assert [(r[0], r[1], str(r[2]), str(r[3])) for r in rows] == [
        ("none", 2, "50.0", "4.50"),
        ("parse_failure", 2, "50.0", "0.00"),
    ]


def test_window_excludes_rows_outside_it(monkeypatch):
    """The panel is a WINDOW, and the port has to honour both of its edges."""
    de = [
        {"timestamp": datetime(2026, 7, 1), "final_quality_score": 1.0,
         "evidence_gathering": "{}", "ticker": "OLD", "cycle_id": "x"},
        {"timestamp": datetime(2026, 8, 25), "final_quality_score": 4.0,
         "evidence_gathering": "{}", "ticker": "NEW", "cycle_id": "y"},
    ]
    _fake_store(monkeypatch, de, [])
    _, rows = bqt.failure_reasons(datetime(2026, 8, 1))
    assert [(r[0], r[1]) for r in rows] == [("none", 1)]
    _, rows = bqt.failure_reasons(datetime(2026, 6, 1), datetime(2026, 8, 1))
    assert [(r[0], r[1]) for r in rows] == [("none", 1)]


def test_tournament_panel_explains_its_own_emptiness(monkeypatch):
    """Trap 7: an empty answer has to show WHY it is empty.

    Section 6 reads `debate_history`, and the tournament was retired on
    2026-07-29 (HANDOFF_tournament_retired_2026-07-29.md). Nothing in `app/`
    reads or writes that collection any more; the live one is frozen at 846
    documents whose newest `created_at` is 2026-07-29 20:05:55. So for any
    normal window section 6 is empty forever, and a bare "(no rows)" is
    indistinguishable from a broken read.
    """
    frozen = datetime(2026, 7, 29, 20, 5, 55)
    monkeypatch.setattr(bqt.mongo_query, "scalar", lambda *a, **k: frozen)
    note = bqt.tournament_retirement_note()
    assert "RETIRED" in note and "2026-07-29" in note

    monkeypatch.setattr(bqt.mongo_query, "scalar", lambda *a, **k: None)
    assert "retired" in bqt.tournament_retirement_note()


def test_tournament_panel_still_answers_when_the_window_reaches_back(monkeypatch):
    """Retired is not deleted: over the tournament era the panel must still work.

    Checked against both stores on 2026-08-30 for 2026-06-01..2026-07-30 —
    Postgres and Mongo returned the identical four rows: Macro_Quant 98 (27.2),
    Value_Quant 93 (25.8), Volatility_Quant 88 (24.4), Momentum_Quant 81 (22.5).
    """
    t0 = datetime(2026, 7, 1)
    docs = [
        {"winner": "bull", "pro_argument": '{"persona": "Macro_Quant"}',
         "con_argument": '{"persona": "Value_Quant"}', "created_at": t0},
        {"winner": "bear", "pro_argument": '{"persona": "Macro_Quant"}',
         "con_argument": '{"persona": "Value_Quant"}', "created_at": t0},
        {"winner": "bear", "pro_argument": '{"persona": "Macro_Quant"}',
         "con_argument": '{"persona": "Value_Quant"}', "created_at": t0},
    ]
    monkeypatch.setattr(bqt.mongo_store, "find_docs", lambda *a, **k: list(docs))
    columns, rows = bqt.tournament_win_rate(datetime(2026, 6, 1))
    assert columns == ["winning_persona", "wins", "pct"]
    assert [(r[0], r[1], str(r[2])) for r in rows] == [
        ("Value_Quant", 2, "66.7"),
        ("Macro_Quant", 1, "33.3"),
    ]


# ── the script has to RUN, not just import cleanly under a helpful sys.path ──
def test_runs_under_the_command_its_own_docstring_documents():
    """`python scripts/board_quality_telemetry.py [DAYS]` — Usage line, verbatim.

    RED BEFORE: the port moved the script's reads into `app.db` but never put
    the repo root on `sys.path`. Invoked as a script, `sys.path[0]` is
    `scripts/`, and this venv carries no path entry for the repo (its only
    .pth files are `__editable__.lazycat-0.1.0`, `a1_coverage` and
    `distutils-precedence`), so line 57 raised:

        ModuleNotFoundError: No module named 'app'      EXIT=1

    Zero output — not one panel, not the `=== done ===` footer. The archive
    version imported only the stdlib and a DB driver and ran fine that way, so
    this was a regression the port introduced, and it made the telemetry
    unrunnable by the only invocation it documents.

    The rest of this file could not see it: it puts REPO on `sys.path` itself
    (line 42) and imports `from scripts import board_quality_telemetry`, which
    is the one path that was never broken. Only a subprocess reproduces the
    interpreter's real `sys.path[0]`, so only a subprocess can catch it.

    Hermetic on purpose: `PRISM_MONGO_URI=mongodb://` makes the lazily-built
    MongoClient raise "Must provide at least one hostname or IP" inside every
    panel's own try/except. The script therefore reaches, renders and closes
    all six panels in ~0.3 s while touching NO database. That is exactly the
    property under test — that the file gets far enough to have panels at all.
    """
    import os
    import subprocess

    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    env["PRISM_MONGO_URI"] = "mongodb://"

    proc = subprocess.run(
        [sys.executable, "scripts/board_quality_telemetry.py", "3"],
        cwd=str(REPO), env=env, capture_output=True, text=True, timeout=120)

    assert "ModuleNotFoundError" not in proc.stderr, proc.stderr
    assert proc.returncode == 0, f"exit={proc.returncode}\n{proc.stderr}"
    for panel in ("1. Per-ticker mean quality", "2. Per-regime mean quality",
                  "3. Failure-reason distribution", "4. Zero-score rate",
                  "5. Regime -> persona routing",
                  "6. H2H tournament persona win rate"):
        assert f"--- {panel} ---" in proc.stdout, proc.stdout
    assert proc.stdout.rstrip().endswith("=== done ===")


# ── the panels the behavioural tests could not see ──────────────────────────
def _capture_queries(monkeypatch, target, attr, result):
    """Record the filter each panel actually sends, and answer with `result`."""
    seen = []

    def fake(collection, query, *a, **k):
        seen.append((collection, query))
        return result

    monkeypatch.setattr(target, attr, fake)
    return seen


def test_panels_5_and_6_window_on_created_at_not_timestamp(monkeypatch):
    """The two collections that have no `timestamp` field at all.

    Panels 1-4 read `decision_evaluations.timestamp`; panels 5 and 6 read
    `trade_results.created_at` and `debate_history.created_at`, exactly as the
    SQL did. Carrying `timestamp` across to them is the obvious copy-paste, and
    it does not raise — `{"timestamp": {"$gt": ...}}` simply matches nothing on
    a collection with no such field, so panel 5 prints "(no rows)" and panel 6
    prints "(no rows)" WHICH IT IS EXPECTED TO ANYWAY. That is trap 7: a script
    that compiles, runs and answers nothing, with the one panel that is allowed
    to be empty providing the alibi.

    Measured 2026-08-30: `timestamp` exists on 0 of 1155 trade_results
    documents and 0 of 846 debate_history documents.
    """
    since, until = datetime(2026, 6, 1), datetime(2026, 7, 30)

    tr = _capture_queries(monkeypatch, bqt.mongo_query, "group_rows", [])
    bqt.regime_persona_routing(since, until)
    assert [c for c, _ in tr] == [bqt.TR]
    assert list(tr[0][1]) == ["created_at"], tr[0][1]
    assert tr[0][1]["created_at"] == {"$gt": since, "$lte": until}

    dh = _capture_queries(monkeypatch, bqt.mongo_store, "find_docs", [])
    bqt.tournament_win_rate(since, until)
    assert [c for c, _ in dh] == [bqt.DH]
    assert "created_at" in dh[0][1] and "timestamp" not in dh[0][1]
    assert dh[0][1]["created_at"] == {"$gt": since, "$lte": until}


def test_panel_1_keeps_limit_40_and_puts_null_means_first(monkeypatch):
    """`ORDER BY mean_q ASC NULLS FIRST LIMIT 40` — both halves.

    Two silent mutations live here. Dropping the LIMIT turns a 40-row panel
    into every ticker in the window (191 groups on the wide window), and
    sorting NULLs LAST hides exactly the rows the panel exists to surface: a
    ticker whose every evaluation scored NULL is the worst case, not the best,
    and NULLS LAST pushes it past the cut where it is never printed at all.

    The LIMIT is applied in PYTHON, after a full `group_rows` with no limit,
    which is the other half of the contract: pushing `limit=40` into the
    pipeline would return the 40 groups Mongo happened to build first (trap 2),
    not the 40 lowest-scoring ones.
    """
    groups = [(f"T{i:03d}", 1, float(i), float(i), float(i)) for i in range(60)]
    groups += [("ZNULL", 1, None, None, None)]

    def fake_group_rows(collection, query, keys, aggs, select, **k):
        assert "limit" not in k or not k["limit"], "LIMIT must not be pushed into Mongo"
        return list(groups)

    monkeypatch.setattr(bqt.mongo_query, "group_rows", fake_group_rows)

    _, rows = bqt.per_ticker_quality(datetime(2026, 1, 1))
    assert len(rows) == 40, f"LIMIT 40 lost: {len(rows)} rows"
    assert rows[0][0] == "ZNULL" and rows[0][2] is None, "NULLS FIRST lost"
    assert [r[0] for r in rows[1:4]] == ["T000", "T001", "T002"]


def test_panel_4_counts_scores_that_are_exactly_zero(monkeypatch):
    """`COUNT(*) FILTER (WHERE final_quality_score = 0)` — equality, not `<= 0`.

    `{"$lte": 0}` reads as the same question and is not: it also counts any
    negative score, inflating the headline "zero rate" that the whole panel
    exists to report, and it does it without erroring or looking odd.
    """
    docs = [{"final_quality_score": v} for v in (5.0, 0.0, 0.0, -1.0, None)]

    def fake_count(collection, query=None):
        q = query or {}
        if "final_quality_score" not in q:
            return len(docs)
        want = q["final_quality_score"]
        assert not isinstance(want, dict), f"must be equality, got {want!r}"
        return sum(1 for d in docs if d["final_quality_score"] == want)

    monkeypatch.setattr(bqt.mongo_query, "count", fake_count)

    columns, rows = bqt.zero_score_rate(datetime(2026, 1, 1))
    assert columns == ["total", "zeroed", "zero_pct"]
    assert rows[0][0] == 5
    assert rows[0][1] == 2, "the -1.0 score is not a hard zero"
    assert str(rows[0][2]) == "40.0"


def test_regime_join_survives_a_document_written_before_the_field(monkeypatch):
    """A trade_results document with no `regime` key must read as NULL, not raise.

    `mongo_query._to_tuple` states the rule this panel has to follow: "a
    document written before a column was added simply lacks the field, and
    Postgres would have returned NULL for it. Raising here would fail a read
    that the SQL answered fine." Indexing the projected doc with `r["regime"]`
    breaks that — one such document turns the whole panel into
    `Error: 'regime'` where the SQL printed `(unknown)`.

    Measured 2026-08-30: `regime` is $type string on all 1155 documents and
    missing from 0, so this is a guard against the next write, not a live bug.
    """
    t0 = datetime(2026, 8, 20, 12, 0)
    de = [{"ticker": "AAPL", "cycle_id": "c1", "timestamp": t0,
           "final_quality_score": 4.0}]
    tr = [{"ticker": "AAPL", "cycle_id": "c1"}]        # no `regime` key at all
    _fake_store(monkeypatch, de, tr)

    _, rows = bqt.per_regime_quality(datetime(2026, 8, 1))
    assert [(r[0], r[1], str(r[2])) for r in rows] == [("(unknown)", 1, "4.00")]
