"""`scripts/agent_fidelity_audit.py` must read `shared_desk` from MongoDB.

WHY THIS FILE EXISTS
--------------------
The fidelity audit is the counter behind "171 invented RSIs out of 305". Until
2026-08-30 it reached its rows through `scripts.migration.pg_connection.get_db`,
which no longer exposes that name, so the script raised AttributeError — loud,
but no more alive than a silent one. Restoring it against Postgres would have
been worse than the crash: the archive froze on 2026-08-19, so

    SELECT desk_data FROM shared_desk
    WHERE created_at > now() - ('7' || ' days')::interval

answers **0 desks** today (measured 2026-08-30 against the live archive), and
the report that renders is a full page of headings with every count at zero —
which is exactly what "no agent fabricated anything this week" looks like.

WHAT IS PINNED, AND WHY EACH LINE IS HERE
-----------------------------------------
  1. no Postgres coupling remains (there were three findings: the
     `pg_connection` import at line 125, the `get_db()` call at 138 and the
     `.execute()` at 139);
  2. the read seam: the POSTGRES TABLE NAME, the two columns IN UNPACKING
     ORDER, and no `limit`;
  3. `desk_data` is decoded in Python from BOTH shapes. The collection is
     split down the cutover — 1762 desks backfilled from the jsonb column are
     subdocuments, the 274 the live writer has stored since are JSON **TEXT**
     — so a reader that handles one shape silently drops the other half, and
     on the DEFAULT 7-day window the half it drops is the whole answer (133
     of 133 desks measured 2026-08-30);
  4. the window admits a desk carrying no `created_at`. 76 of the 2036 desks
     have none (Mongo-only mirror rows from 2026-08-18; 0 of the 76 are in
     the Postgres archive), and `$gt` does not match a missing field, so the
     mechanical translation drops all 76 (591 desks vs 667 at `--days 30`,
     measured). NOTE, because the first version of this file said otherwise:
     Postgres did NOT declare that column NOT NULL. `information_schema`
     reports `is_nullable = YES, column_default = now()`; the default is why
     0 of the 1762 archive rows are NULL. So the disjunction is a deliberate
     WIDENING — a NULL `created_at` would have been EXCLUDED by
     `created_at > ...`, the opposite of what the fallback does — and what
     the tests below pin is that the widening is REPORTED, not that it is
     faithful;
  5. VACUITY is measured on the artifacts audited, not on the rows fetched.
     Rows that arrive and do not decode, or decode and carry no artifact,
     render the same full banner over the same nothing;
  6. the whole report is a function of the data, not of the order the store
     returned it in — INCLUDING the printed `prose_mismatch_examples`;
  7. the report's headline: `reconciled_fields` and `UNGUARDED_fields`, and
     that the reconciled tuples are really read out of `app/quant/*`;
  8. the counting rules that decide the numbers: one artifact counted once,
     booleans are not numeric fields, the [:8] cut, the rounded rate;
  9. the CLI surface the port claims it left alone: `--days` default 7,
     `--json`, exit 0.

The reads are STUBBED. The live numbers move with the store, and a test that
asserts today's counts fails tomorrow for no defect; the live comparison
belongs in the two-store probe, not here.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from app.db import mongo_query  # noqa: E402
from scripts import agent_fidelity_audit as afa  # noqa: E402
from scripts.gate_zero_pg import scan  # noqa: E402

NOW = datetime.now(timezone.utc)


# ── 1. the coupling is gone ────────────────────────────────────────────────

def test_the_audit_has_no_postgres_coupling():
    """RED before the port: 3 findings — connection_import at line 125,
    get_db_call at 138, execute_call at 139."""
    result = scan(REPO, targets=("scripts/agent_fidelity_audit.py",))
    assert result["errors"] == [], result["errors"]
    assert result["total"] == 0, (
        "agent_fidelity_audit.py still reads Postgres: "
        + "; ".join(f"{f['kind']} at line {f['line']}" for f in result["findings"]))


def test_the_scan_can_still_fail(tmp_path):
    """NEGATIVE CONTROL. A scan that finds nothing because it looked at nothing
    passes the assertion above just as happily."""
    (tmp_path / "reader.py").write_text(
        "from scripts.migration.pg_connection import get_db\n"
        "def f():\n"
        "    with get_db() as db:\n"
        "        return db.execute('SELECT desk_data FROM shared_desk').fetchall()\n",
        encoding="utf-8")
    assert scan(tmp_path, targets=("reader.py",))["total"] > 0


# ── the stub ───────────────────────────────────────────────────────────────

def _desk(ticker="AAA", *, pe=20.0, rsi=55.0, summary=""):
    """One desk_data document, in the shape the pipeline writes."""
    return {
        "ticker": ticker,
        "fundamental_report": {
            "summary": summary,
            "metrics": {"pe_ratio": pe, "confidence": 88},
        },
        "quant_report": {"risk_metrics": {"rsi": rsi, "diversification_ratio": 1.4}},
    }


def _fundamental(ticker="AAA", *, metrics=None, summary="",
                 unreconciled=None, model_reported=None):
    """A desk carrying ONE artifact, so a test can name exactly what it counts."""
    art: dict = {"summary": summary, "metrics": dict(metrics or {})}
    if model_reported is not None:
        art["_model_reported_fundamentals"] = model_reported
    if unreconciled is not None:
        art["_unreconciled_fundamentals"] = unreconciled
    return {"ticker": ticker, "fundamental_report": art}


def _doc(desk, created=NOW, *, as_text=True):
    """A shared_desk DOCUMENT — the shape the store holds, not the shape the
    script unpacks. The stub does the projection, so the column order the
    script asks for is the thing under test rather than a fixture constant."""
    return {"desk_data": json.dumps(desk) if as_text else desk,
            "created_at": created}


@pytest.fixture
def store(monkeypatch):
    """Stub the read seam so that it behaves like the real `find_rows`.

    THE POINT OF THE PROJECTION. `mongo_query` exists to return "tuples in the
    column order the SQL asked for" — that shape compatibility is the only
    reason positional call sites survived the codemod. A stub that returns
    fixed tuples regardless of `columns` cannot see a caller that asks for the
    wrong order, and that is not a hypothetical: swapping the two projected
    columns puts a timestamp where the script unpacks `desk_data`, decodes
    nothing, and prints the full "133 desks" banner over zero agent sections
    at exit 0. So this stub projects, exactly as the seam does.
    """
    seen: dict = {}

    def _rows(docs, *, total=2036):
        def find_rows(collection, query, columns, sort=None, limit=0, **kw):
            seen["collection"] = collection
            seen["query"] = query
            seen["columns"] = list(columns)
            seen["sort"] = sort
            seen["limit"] = limit
            rows = [tuple(d.get(c) for c in columns) for d in docs]
            # `limit` is honoured, so a caller that adds one gets the FEWER
            # rows a limit really returns rather than a stub that ignores it.
            return rows[:limit] if limit else rows
        monkeypatch.setattr(mongo_query, "find_rows", find_rows)
        monkeypatch.setattr(mongo_query, "count", lambda c, q=None: total)
        return seen

    return _rows


@pytest.fixture
def cli(monkeypatch):
    """Run `main()` as the shell would, without touching the environment."""
    def _run(*argv):
        monkeypatch.setattr("dotenv.load_dotenv", lambda *a, **k: False)
        monkeypatch.setattr(sys, "argv", ["agent_fidelity_audit.py", *argv])
        return afa.main()
    return _run


# ── 2. it reads Mongo, by the POSTGRES TABLE NAME, in unpacking order ──────

def test_it_reads_shared_desk_through_the_mongo_seam(store):
    seen = store([_doc(_desk())])
    rep = afa.audit(7)

    # The table name, never a resolved collection: mongo_query calls
    # collection_for() itself, exactly once.
    assert seen["collection"] == "shared_desk"
    assert rep["desks"] == 1
    assert rep["agents"]["fundamental_analyst"]["artifacts"] == 1


def test_the_columns_are_projected_in_unpacking_order(store):
    """`for desk, created in rows` is a POSITIONAL contract with the seam.

    Red against `["created_at", "desk_data"]` — a one-token swap that is
    otherwise invisible: it keeps `rows` non-empty, so the old `if not rows`
    guard stayed silent, and every assertion in the previous version of this
    file still passed because its stub returned fixed tuples and only ever
    checked that "desk_data" was somewhere in the list.
    """
    seen = store([_doc(_desk())])
    rep = afa.audit(7)

    assert seen["columns"] == ["desk_data", "created_at"], (
        "the tuple is unpacked as (desk_data, created_at); the projection must "
        f"ask for them in that order, not {seen['columns']}")
    # ... and behaviourally, through the projecting stub: the desk decoded.
    assert (rep["desks"], rep["desks_decoded"], rep["artifacts"]) == (1, 1, 2)


def test_the_read_takes_no_limit(store):
    """Trap 2. A `limit` on a growing collection does not sample it, it samples
    the PAST — natural order returns the oldest documents first. Red against
    `limit=500`, which is otherwise invisible on any fixture smaller than 500
    rows (i.e. every fixture in this file)."""
    seen = store([_doc(_desk())])
    afa.audit(7)
    assert seen["limit"] == 0, "an audit reads its whole window"
    assert seen["sort"] is None


def test_the_limit_in_the_stub_really_truncates(store):
    """NEGATIVE CONTROL for the assertion above: a stub that ignored `limit`
    would let a limited read pass every behavioural test in this file."""
    seen = store([_doc(_desk("AAA")), _doc(_desk("BBB"))])
    rows = mongo_query.find_rows("shared_desk", {}, ["desk_data"], limit=1)
    assert len(rows) == 1 and seen["limit"] == 1


# ── 3. both shapes of desk_data ────────────────────────────────────────────

def test_json_text_desk_data_is_decoded(store):
    """The LIVE half: every desk written since the cutover stores desk_data as
    JSON text. Red against a port that assumed a subdocument — and on the
    default 7-day window that is 100% of the rows."""
    store([_doc(_desk(pe=31.5))])
    rep = afa.audit(7)
    assert rep["agents"]["fundamental_analyst"]["numeric_fields_emitted"] == {"pe_ratio": 1}


def test_subdocument_desk_data_is_counted(store):
    """The ARCHIVE half: the 1762 backfilled desks are real subdocuments."""
    store([_doc(_desk(pe=31.5), as_text=False)])
    rep = afa.audit(7)
    assert rep["agents"]["fundamental_analyst"]["numeric_fields_emitted"] == {"pe_ratio": 1}


def test_undecodable_desk_data_is_skipped_not_fatal(store):
    store([{"desk_data": "{not json", "created_at": NOW}, _doc(_desk())])
    rep = afa.audit(7)
    assert rep["desks"] == 2
    assert rep["desks_decoded"] == 1
    assert rep["agents"]["fundamental_analyst"]["artifacts"] == 1


# ── 4. the window must admit a desk with no created_at ─────────────────────

def test_the_window_admits_a_desk_with_no_created_at(store):
    """`$gt` does not match a missing field, and 76 desks have no created_at.
    Red against `{"created_at": {"$gt": cutoff}}`, which is the mechanical
    translation and drops all 76 with no error. It is a WIDENING — see the
    module docstring: the column is nullable in Postgres and a NULL would have
    been excluded by the SQL — so what is pinned is that it is reported."""
    seen = store([_doc(_desk(), created=None)])
    rep = afa.audit(7)

    q = seen["query"]
    assert "$or" in q, f"window is not a disjunction: {q}"
    fallback = [b for b in q["$or"] if b.get("created_at") == {"$exists": False}]
    assert fallback, f"no branch admits a desk without created_at: {q}"
    assert "updated_at" in fallback[0], "the fallback must still bound the window"

    # and the desk it recovers is REPORTED, not folded in silently
    assert rep["desks"] == 1
    assert rep["desks_dated_by_updated_at"] == 1


def test_the_window_is_computed_from_days(store):
    seen = store([])
    before = datetime.now(timezone.utc)
    afa.audit(30)
    dated = [b for b in seen["query"]["$or"] if "$gt" in b.get("created_at", {})]
    cutoff = dated[0]["created_at"]["$gt"]
    assert isinstance(cutoff, datetime), "the boundary must be a value, not SQL"
    # `before` is read a hair before audit() reads its own clock, so the gap is
    # 30 days minus microseconds, not 30 days exactly.
    assert timedelta(days=29, hours=23) <= before - cutoff <= timedelta(days=30, seconds=5)


# ── 5. vacuity is about the artifacts audited, not the rows fetched ────────

def test_an_empty_window_is_vacuity_not_a_pass(store):
    store([])
    rep = afa.audit(7)
    assert (rep["desks"], rep["artifacts"]) == (0, 0)
    assert rep["collection_total"] == 2036, (
        "an empty report must say how many documents the collection holds — "
        "'quiet week' and 'wrong store' are the same output without it")


def test_rows_that_do_not_decode_are_vacuity_too(store, cli, capsys):
    """The guard used to be `if not rows`, which is the wrong quantity: rows
    ARRIVED here, none of them decoded, and the run measured nothing. Red
    against `if not rows` — `desks` is 3, so the old guard said nothing and the
    banner claimed three desks' worth of clean fidelity."""
    store([{"desk_data": "{not json", "created_at": NOW}] * 3)
    rep = afa.audit(7)
    assert (rep["desks"], rep["desks_decoded"], rep["artifacts"]) == (3, 0, 0)
    assert rep["collection_total"] == 2036

    assert cli() == 0
    out = capsys.readouterr().out
    assert "VACUITY" in out and "NOT ONE" in out, out


def test_rows_that_carry_no_artifact_are_vacuity_too(store, cli, capsys):
    """Decoded fine, audited nothing: the eight artifact keys are all absent.
    Same banner, same exit 0, and without this guard the same false clean bill."""
    store([_doc({"ticker": "AAA", "phase": "INIT"})])
    rep = afa.audit(7)
    assert (rep["desks"], rep["desks_decoded"], rep["artifacts"]) == (1, 1, 0)
    assert rep["collection_total"] == 2036

    assert cli() == 0
    out = capsys.readouterr().out
    assert "VACUITY" in out and "not one carries" in out, out


def test_a_real_run_is_not_reported_as_vacuous(store, cli, capsys):
    """NEGATIVE CONTROL for the three tests above: a guard that fires always is
    as useless as one that never fires, and would pass all of them."""
    store([_doc(_desk())])
    rep = afa.audit(7)
    assert rep["artifacts"] == 2
    assert "collection_total" not in rep

    assert cli() == 0
    out = capsys.readouterr().out
    assert "VACUITY" not in out, out
    assert "fundamental_analyst" in out


# ── 6. the report is a function of the data, not of row order ──────────────

def test_the_report_does_not_depend_on_row_order(store):
    """Reducing the same 1762 pre-cutover desks with the row list REVERSED
    changed three fields of the report — all of them `prose_mismatch_examples`,
    and those lines are printed to the terminal as findings ("LMT: prose says
    ev_to_ebit=1.9, field says 16.1"). Red against `if len(...) < 5: append`,
    which keeps the first five SEEN, and red against `key=lambda kv: -kv[1]`,
    which leaves every tie in arrival order."""
    # Seven mismatching desks, deliberately NOT in alphabetical order, and
    # more of them than the five example slots — so "first five seen" and
    # "five smallest by content" are different answers. Each desk also
    # contributes one tied `emitted` field and one tied disagreement, in the
    # same shuffled order.
    order = ["GGG", "AAA", "FFF", "CCC", "BBB", "EEE", "DDD"]
    rows = [
        _doc(_fundamental(
            t,
            metrics={f"m_{t.lower()}": 10.0},
            summary="trading at a P/E of 8.59",
            unreconciled={f"w_{t.lower()}": 1},
        ) | {"fundamental_report": {
            "summary": "trading at a P/E of 8.59",
            "metrics": {"pe_ratio": 14.97, f"m_{t.lower()}": 10.0},
            "_unreconciled_fundamentals": {f"w_{t.lower()}": 1},
        }})
        for t in order
    ]
    store(rows)
    forward = afa.audit(7)
    store(list(reversed(rows)))
    backward = afa.audit(7)

    a = forward["agents"]["fundamental_analyst"]
    # A LIST, not the dict: `{"a": 1, "b": 1} == {"b": 1, "a": 1}` is True, so
    # comparing these as dicts cannot see a reordering at all.
    assert list(a["fields_most_often_wrong"]) == [
        "w_aaa", "w_bbb", "w_ccc", "w_ddd", "w_eee", "w_fff", "w_ggg"]
    assert list(a["numeric_fields_emitted"]) == [
        "pe_ratio", "m_aaa", "m_bbb", "m_ccc", "m_ddd", "m_eee", "m_fff", "m_ggg"]
    # the five examples are chosen by CONTENT, not by arrival
    assert [e["ticker"] for e in a["prose_mismatch_examples"]] == [
        "AAA", "BBB", "CCC", "DDD", "EEE"]
    assert a["prose_mismatches"] == 7, "all seven are still counted"

    assert forward == backward, "the report changed with the row order"


@pytest.fixture
def no_guarded_fields(monkeypatch):
    """Pin the reconciled set to EMPTY for tests about the emitted ranking.

    `audit()` reads the guarded set live out of
    `app/quant/{technical_baseline,valuation_block,fundamental_block}
    .VERIFIED_*_FIELDS`. Two tests below picked `beta` and `pe_ratio` as
    arbitrary fixture field names, and both are really in
    `fundamental_analyst`'s live list — so the assertions failed for a reason
    that has nothing to do with what they are testing, and would have failed
    again the next time someone added a field to that constant.

    A unit test must state its own premise. These two are about the SORT and
    the bool exclusion; what is guarded is irrelevant to both, so it is pinned
    rather than inherited.
    """
    import scripts.agent_fidelity_audit as _afa
    monkeypatch.setattr(_afa, "_reconciled_fields", lambda: {})


def test_ties_in_the_emitted_ranking_break_on_the_field_name(store, no_guarded_fields):
    """`-kv[1]` alone is not a total order and the tail of this report is
    nothing but ties, so the order fell out of the order the desks arrived in,
    which no store guarantees. This sort drives BOTH `numeric_fields_emitted`
    and `UNGUARDED_fields` — the report's headline — and reverting it alone
    survived every test in the previous version of this file.

    One field per desk, arriving gamma-alpha-beta, all tied at 1: under a
    count-only sort Python's stable sort keeps arrival order, so this is RED
    against `key=lambda kv: -kv[1]` on every run, not just an unlucky one."""
    store([_doc(_fundamental("CCC", metrics={"gamma": 1.0})),
           _doc(_fundamental("AAA", metrics={"alpha": 1.0})),
           _doc(_fundamental("BBB", metrics={"beta": 1.0}))])
    a = afa.audit(7)["agents"]["fundamental_analyst"]
    assert list(a["numeric_fields_emitted"]) == ["alpha", "beta", "gamma"]
    assert a["UNGUARDED_fields"] == ["alpha", "beta", "gamma"]


# ── 7. the headline: what is guarded and what is not ───────────────────────

def test_unguarded_is_emitted_minus_reconciled(store, monkeypatch):
    """`UNGUARDED_fields` IS the report. Red against the inversion
    (`if f in guarded`) and against `guarded = set()`, both of which survived
    the previous version of this file — nothing in it asserted either key."""
    monkeypatch.setattr(afa, "_reconciled_fields",
                        lambda: {"fundamental_analyst": ("pe_ratio",)})
    store([_doc(_fundamental(metrics={"pe_ratio": 20.0, "market_cap": 1e9}))])
    a = afa.audit(7)["agents"]["fundamental_analyst"]
    assert a["reconciled_fields"] == ["pe_ratio"]
    assert a["UNGUARDED_fields"] == ["market_cap"], (
        "a field the reconcile pass enforces is not a fabrication surface; "
        "a field it does not enforce is exactly one")


def test_the_reconciled_tuples_are_really_read_from_app_quant():
    """`_reconciled_fields()` swallows ImportError per module and returns `()`.
    That is the right behaviour and the wrong silence: if `app/quant/*` moves,
    every field reads as UNGUARDED and the audit reports a catastrophe that is
    entirely its own. Red the day those imports break."""
    enforced = afa._reconciled_fields()
    assert set(enforced) == {"quant_analyst", "valuation_analyst", "fundamental_analyst"}
    for label, expect in (("quant_analyst", "rsi"),
                          ("valuation_analyst", "ev_to_ebit"),
                          ("fundamental_analyst", "pe_ratio")):
        assert enforced[label], f"{label} reconciles nothing — did the import fail?"
        assert expect in enforced[label], (label, expect, enforced[label])


# ── 8. the counting rules ──────────────────────────────────────────────────

def test_one_artifact_counts_once_even_with_both_origin_keys(store):
    """The reconcile pass can write BOTH `_model_reported_*` and
    `_unreconciled_*` on the same artifact. Without the `break` the artifact is
    counted twice and the disagreement RATE exceeds 1. Red against dropping it."""
    store([_doc(_fundamental(model_reported={"pe_ratio": 9.0},
                             unreconciled={"forward_pe": 1}))])
    a = afa.audit(7)["agents"]["fundamental_analyst"]
    assert a["artifacts"] == 1
    assert a["artifacts_where_model_disagreed"] == 1
    assert a["fields_most_often_wrong"] == {"pe_ratio": 1}, (
        "the first origin key wins and the loop stops; counting both would "
        "count one artifact's disagreement twice")


def test_the_disagreement_rate_is_a_rounded_fraction(store):
    """One of three, not one. Red against handing back the raw count."""
    store([_doc(_fundamental("AAA", unreconciled={"pe_ratio": 1})),
           _doc(_fundamental("BBB")),
           _doc(_fundamental("CCC"))])
    a = afa.audit(7)["agents"]["fundamental_analyst"]
    assert (a["artifacts"], a["artifacts_where_model_disagreed"]) == (3, 1)
    assert a["disagreement_rate"] == 0.333


def test_a_boolean_is_not_a_numeric_field(store, no_guarded_fields):
    """`isinstance(True, int)` is True in Python, so a flag counts as an
    emitted number unless it is excluded — and then gets reported as an
    UNGUARDED fabrication surface. Red against dropping the bool guard."""
    store([_doc(_fundamental(metrics={"pe_ratio": 20.0, "is_profitable": True}))])
    a = afa.audit(7)["agents"]["fundamental_analyst"]
    assert a["numeric_fields_emitted"] == {"pe_ratio": 1}
    assert a["UNGUARDED_fields"] == ["pe_ratio"]


def test_the_worst_fields_list_is_cut_at_eight(store):
    """Nine fields tied at one disagreement each: eight print, alphabetically
    first. Red against `[:4]`, and red against a count-only sort."""
    store([_doc(_fundamental(unreconciled={f"f{i:02d}": 1 for i in range(9)}))])
    a = afa.audit(7)["agents"]["fundamental_analyst"]
    assert list(a["fields_most_often_wrong"]) == [f"f{i:02d}" for i in range(8)]


def test_metadata_is_not_counted_as_a_fabrication_surface(store):
    """`confidence` is the agent's opinion of itself and the underscore fields
    are written by our own validators; neither is a claim about the world."""
    store([_doc(_fundamental(metrics={"pe_ratio": 20.0, "confidence": 88,
                                      "quality_score": 4, "_quality_score": 4}))])
    a = afa.audit(7)["agents"]["fundamental_analyst"]
    assert a["numeric_fields_emitted"] == {"pe_ratio": 1}


# ── 9. prose-vs-field, the half that has nothing to do with the store ──────

def test_prose_is_checked_against_the_structured_field(store):
    store([_doc(_desk(pe=14.97, summary="trading at a P/E of 8.59"))])
    rep = afa.audit(7)
    a = rep["agents"]["fundamental_analyst"]
    assert (a["prose_claims_checked"], a["prose_mismatches"]) == (1, 1)
    assert a["prose_mismatch_examples"] == [
        {"ticker": "AAA", "field": "pe_ratio", "prose": 8.59, "field_value": 14.97}]


def test_a_forward_pe_in_prose_is_not_a_mismatch(store):
    """The audit must be at least as careful as the work it audits: SMCI's
    "a forward P/E of 8.59" against a trailing pe_ratio of 14.97 is correct."""
    store([_doc(_desk(pe=14.97, summary="a forward P/E of 8.59"))])
    rep = afa.audit(7)
    assert rep["agents"]["fundamental_analyst"]["prose_mismatches"] == 0


def test_a_desk_with_no_ticker_does_not_break_the_example_sort(store):
    """The examples are ordered by content, and `None < "AAA"` is a TypeError,
    not an order. Red against sorting on the raw ticker."""
    desk = _fundamental(metrics={"pe_ratio": 14.97}, summary="a P/E of 8.59")
    desk.pop("ticker")
    store([_doc(desk), _doc(_fundamental("AAA", metrics={"pe_ratio": 14.97},
                                         summary="a P/E of 8.59"))])
    a = afa.audit(7)["agents"]["fundamental_analyst"]
    assert [e["ticker"] for e in a["prose_mismatch_examples"]] == ["AAA", None]


# ── 10. the CLI the port claims it left alone ──────────────────────────────

def test_the_cli_defaults_to_seven_days_and_exits_zero(store, cli, monkeypatch, capsys):
    """`--days` default 7 and `return 0` were asserted NOWHERE: the previous
    version of this file never invoked `main()`, so "flags and exit code
    unchanged" was a claim, not a check. Red against a default of 30 and
    against `return 1`."""
    store([_doc(_desk())])
    called: dict = {}
    real = afa.audit

    def recording(days):
        called["days"] = days
        return real(days)

    monkeypatch.setattr(afa, "audit", recording)
    assert cli() == 0
    assert called["days"] == 7
    out = capsys.readouterr().out
    assert "AGENT NUMERIC FIDELITY" in out and "1 desks, last 7 days" in out


def test_the_cli_passes_days_through_and_writes_the_json(store, cli, monkeypatch, tmp_path):
    store([_doc(_desk())])
    called: dict = {}
    real = afa.audit

    def recording(days):
        called["days"] = days
        return real(days)

    monkeypatch.setattr(afa, "audit", recording)
    out_path = tmp_path / "fidelity.json"
    assert cli("--days", "30", "--json", str(out_path)) == 0
    assert called["days"] == 30

    written = json.loads(out_path.read_text(encoding="utf-8"))
    assert written["days"] == 30
    assert written["desks"] == 1 and written["artifacts"] == 2
    assert written["agents"]["fundamental_analyst"]["artifacts"] == 1
