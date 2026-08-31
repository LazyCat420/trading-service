"""`scripts/research_loop_report.py` must read the store the cycle writes to.

WHAT WAS ACTUALLY WRONG, AND WHY IT LOOKED FINE
-----------------------------------------------
The script asks two questions. The question-ledger half already read Mongo and
answered correctly. The worklist-shadow half went through
`scripts.migration.pg_connection.get_db()`, which after the cutover raises
`AttributeError` on the deleted settings field — and the script caught it,
printed `query failed: ...`, and then printed

    (worklist_shadow_runs is created on first record() call)

which reads as "no cycle has recorded anything yet". It exited 0. So for the
eight days after 2026-08-19 the operator's shadow report said, in effect,
"nothing to see", while Mongo held 92 post-cutover cycles it never looked at.
A dead read that renders as an idle subsystem is the failure this port exists
to remove, so the tests below pin both halves of the fix: the read goes to
Mongo, AND a read that fails is no longer mistakable for an empty one.

EVERY TEST HERE IS RED ON THE PRE-PORT FILE. `test_no_postgres_coupling` fails
on the `pg_connection` import and the two `get_db()` calls; the behavioural
tests fail because `shadow_section` returned `{"rows": 0}` for every input; and
`shadow_pipeline` / `shadow_row` did not exist at all. Two negative controls
are included, because a scanner that finds nothing and a shape-assertion that
accepts anything both pass in silence.
"""
from __future__ import annotations

import copy
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from app.db import mongo_query, mongo_store  # noqa: E402
from scripts import research_loop_report as rlr  # noqa: E402
from scripts.gate_zero_pg import scan  # noqa: E402

REL = "scripts/research_loop_report.py"

# One recorded cycle's worth of group output, in the shape `$group` emits.
FAKE_GROUP = [{"_id": None, "n": 7, "avg_budget": 2.5,
               "ov_free": 0.25, "ov_queue": 0.5, "empty": 2}]


# ── the coupling is gone ───────────────────────────────────────────────────

def test_no_postgres_coupling():
    """AST scan, not grep: a docstring that describes the hazard is not one."""
    result = scan(REPO, targets=(REL,))
    assert result["errors"] == [], result["errors"]
    assert result["total"] == 0, (
        "; ".join(f"{f['kind']} at line {f['line']}" for f in result["findings"]))


def test_the_scan_would_have_caught_the_pre_port_file(tmp_path):
    """NEGATIVE CONTROL. A scan that reports zero because it looked at nothing
    passes the test above just as happily. This feeds it the exact import and
    call the pre-port file carried and pins that it comes back nonzero."""
    bad = tmp_path / "research_loop_report.py"
    bad.write_text(
        "from scripts.migration.pg_connection import get_db\n"
        "def f():\n"
        "    with get_db() as db:\n"
        "        return db.execute('SELECT count(*) FROM worklist_shadow_runs').fetchone()\n",
        encoding="utf-8")
    assert scan(tmp_path, targets=("research_loop_report.py",))["total"] > 0


# ── the read goes to Mongo, by TABLE name ──────────────────────────────────

def test_shadow_row_reads_mongo_by_table_name(monkeypatch):
    seen: dict = {}

    def fake_aggregate(collection, pipeline, session=None):
        seen["collection"] = collection
        seen["pipeline"] = pipeline
        return FAKE_GROUP

    monkeypatch.setattr(mongo_store, "aggregate", fake_aggregate)
    row = rlr.shadow_row(14)

    assert row == (7, 2.5, 0.25, 0.5, 2), row
    # The POSTGRES TABLE NAME. `collection_for()` is called inside
    # mongo_store, exactly once; passing a resolved name here would resolve it
    # twice and, once renames are live, read a collection that does not exist.
    assert seen["collection"] == "worklist_shadow_runs"


def test_shadow_section_reports_the_mongo_numbers(monkeypatch, capsys):
    monkeypatch.setattr(mongo_store, "aggregate",
                        lambda c, p, session=None: FAKE_GROUP)
    monkeypatch.setattr(mongo_store, "count_docs", lambda c, q=None: 39)

    out = rlr.shadow_section(14)
    text = capsys.readouterr().out

    assert out == {"rows": 7, "overlap_free": 0.25, "overlap_queue": 0.5,
                   "empty": 2, "undated": 39}, out
    assert "cycles recorded ............. 7" in text
    assert "25.0%" in text and "50.0%" in text
    # The pre-port file never printed this line, because it never knew the
    # window silently drops documents that carry no created_at.
    assert "undated documents ........... 39" in text


def test_json_mode_counts_mongo(monkeypatch, capsys):
    calls: list = []
    monkeypatch.setattr(mongo_query, "count",
                        lambda c, q=None: calls.append((c, q)) or 41)
    monkeypatch.setattr(rlr, "undated_shadow_docs", lambda: 39)
    monkeypatch.setattr("app.services.question_ledger.stats",
                        lambda days=14: {"total": 0})
    monkeypatch.setattr(sys, "argv", ["research_loop_report.py", "--json"])

    assert rlr.main() == 0
    payload = capsys.readouterr().out
    assert '"shadow_rows": 41' in payload
    assert '"shadow_undated": 39' in payload
    assert calls[0][0] == "worklist_shadow_runs"
    assert "$gte" in calls[0][1]["created_at"]


# ── the SQL's semantics, not merely its output shape ───────────────────────

def _mean_of_ratios_check(pipeline: list[dict]) -> None:
    """Assert the overlap columns are `avg(a / NULLIF(b,0))`, per document.

    `sum(a)/sum(b)` is the tempting shortcut and it is a DIFFERENT number
    whenever the budget varies between cycles — it does, 1..7 in the archive —
    so it would report a budget-weighted mean under an unweighted label, with
    no error and a plausible value.
    """
    group = next(s["$group"] for s in pipeline if "$group" in s)
    for name, numerator in (("ov_free", "overlap_live_free"),
                            ("ov_queue", "overlap_live_queue")):
        expr = group[name]
        assert set(expr) == {"$avg"}, f"{name} must be an $avg, got {expr}"
        cond = expr["$avg"]
        assert "$cond" in cond, f"{name} must divide per document, got {cond}"
        guarded, then, otherwise = cond["$cond"]
        assert then == {"$divide": [f"${numerator}", "$budget"]}, then
        # NULLIF(budget,0): the else branch must be NULL, not 0. Coalescing to
        # zero adds a term per undivideable row and pulls the mean down.
        assert otherwise is None, f"{name} else-branch must be null, got {otherwise!r}"
        assert {"$ne": ["$budget", 0]} in guarded["$and"], guarded


def test_the_overlap_averages_are_means_of_per_cycle_ratios():
    _mean_of_ratios_check(rlr.shadow_pipeline(rlr._cutoff(14)))


def test_the_shape_check_rejects_the_shortcut_it_exists_to_forbid():
    """NEGATIVE CONTROL for the assertion above: an assertion that accepts
    everything is not an assertion. Both wrong forms must be rejected."""
    ratio_of_means = [{"$group": {
        "_id": None,
        "ov_free": {"$divide": [{"$sum": "$overlap_live_free"}, {"$sum": "$budget"}]},
        "ov_queue": {"$divide": [{"$sum": "$overlap_live_queue"}, {"$sum": "$budget"}]},
    }}]
    with pytest.raises(AssertionError):
        _mean_of_ratios_check(ratio_of_means)

    zero_coalesced = copy.deepcopy(rlr.shadow_pipeline(rlr._cutoff(14)))
    zero_coalesced[1]["$group"]["ov_free"]["$avg"]["$cond"][2] = 0
    with pytest.raises(AssertionError):
        _mean_of_ratios_check(zero_coalesced)


def test_queue_empty_counts_only_true_the_way_the_case_expression_did():
    """`sum(CASE WHEN queue_empty THEN 1 ELSE 0 END)`. A missing flag — and
    Mongo has no column DEFAULT to supply one — takes the ELSE branch, as a
    NULL did in SQL. `{"$ne": [..., False]}` would count it as empty."""
    group = next(s["$group"] for s in rlr.shadow_pipeline(rlr._cutoff(14))
                 if "$group" in s)
    assert group["empty"] == {
        "$sum": {"$cond": [{"$eq": ["$queue_empty", True]}, 1, 0]}}


def test_an_empty_window_returns_sqls_empty_group_row(monkeypatch):
    """`$group` over no documents emits NO document; `SELECT count(*), avg(...)`
    over no rows still returns ONE row. The caller unpacks five values."""
    monkeypatch.setattr(mongo_store, "aggregate", lambda c, p, session=None: [])
    assert rlr.shadow_row(14) == (0, None, None, None, None)


# ── a failed read is an error, not an empty result ─────────────────────────

def test_a_failed_read_exits_nonzero_and_does_not_say_no_rows(monkeypatch, capsys):
    def boom(*a, **k):
        raise AttributeError("'Settings' object has no attribute 'X'")

    monkeypatch.setattr(mongo_store, "aggregate", boom)
    monkeypatch.setattr("app.services.question_ledger.stats",
                        lambda days=14: {"total": 0})
    monkeypatch.setattr(sys, "argv", ["research_loop_report.py"])

    code = rlr.main()
    text = capsys.readouterr().out

    assert code == 1, "a shadow read that failed must not exit 0"
    shadow = text.split("WORKLIST SHADOW", 1)[1]
    assert "READ FAILED" in shadow
    # The pre-port file printed exactly these two reassurances on top of the
    # failure, which is what made a dead read look like an idle subsystem.
    assert "created on first record() call" not in shadow
    assert "no rows" not in shadow
