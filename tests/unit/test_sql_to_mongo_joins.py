"""JOIN translation, and the differential checker that has to judge it.

Two halves, and the second is the reason the first is trustworthy:

* `sql_to_mongo._translate_join` turns a two-table INNER JOIN into a
  `mongo_query.join_rows()` call, and refuses every shape join_rows cannot
  express exactly rather than approximating it.
* `verify_translations.compare` is the differential checker. It had two stacked
  faults that made it judge nothing while still printing a percentage, so it is
  pinned here too. A translator is only as good as the oracle that scores it.
"""

import pytest

# `sqlglot` is MIGRATION TOOLING, not an application dependency: 467c77b pinned
# it in requirements-migration.in and 6bc835f took it out of the app image with
# the rest of the Postgres teardown. Importing it unguarded at module scope
# turns "the optional tool is absent" into a COLLECTION ERROR, and a collection
# error aborts the ENTIRE run — `pytest tests` stopped before executing a single
# test, so the full suite has been unrunnable without
# --continue-on-collection-errors. Skip cleanly instead.
pytest.importorskip("sqlglot", reason="migration tooling; see requirements-migration.in")

from scripts.sql_to_mongo import Unsupported, translate
from scripts.verify_translations import as_row_list, compare


# --------------------------------------------------------------------------
# JOIN translation
# --------------------------------------------------------------------------

_INNER = ("SELECT a.author_agent, a.note FROM whiteboard_annotations a "
          "JOIN whiteboard_entries e ON e.id = a.entry_id "
          "WHERE e.cycle_id = %s ORDER BY a.created_at ASC")


def test_a_two_table_inner_join_becomes_join_rows():
    t = translate(_INNER)
    assert "mongo_query.join_rows(" in t.call
    assert "'whiteboard_annotations'" in t.call and "'whiteboard_entries'" in t.call
    # ON e.id = a.entry_id -> left key is the FROM side's column.
    assert "'entry_id'" in t.call
    assert t.returns == "rows"


def test_the_where_is_split_onto_the_side_that_owns_it():
    t = translate(_INNER)
    # cycle_id belongs to the JOINed table, so it must land in right_query and
    # NOT in the left collection's filter, which would match nothing.
    assert "right_query={'cycle_id'" in t.call
    assert t.call.index("join_rows('whiteboard_annotations', {}") > 0


@pytest.mark.parametrize("sql, expect_left, expect_right", [
    # Placeholders are numbered in the order next_param() is CALLED. Bucketing
    # by side while numbering in SQL order is the whole subtlety: build one
    # side fully and then the other, and every parameter crossing the split is
    # silently re-bound to the wrong value. Nothing about that fails to parse.
    ("SELECT a.note, e.section FROM whiteboard_annotations a "
     "JOIN whiteboard_entries e ON e.id = a.entry_id "
     "WHERE a.author_agent = %s AND e.ticker = %s", "{p0}", "{p1}"),
    ("SELECT a.note, e.section FROM whiteboard_annotations a "
     "JOIN whiteboard_entries e ON e.id = a.entry_id "
     "WHERE e.ticker = %s AND a.author_agent = %s", "{p1}", "{p0}"),
])
def test_placeholders_follow_sql_order_not_side_order(sql, expect_left, expect_right):
    call = translate(sql).call
    assert f"{{'author_agent': {expect_left}}}" in call
    assert f"right_query={{'ticker': {expect_right}}}" in call


@pytest.mark.parametrize("sql, because", [
    # An inner stitch DROPS the non-matching rows a LEFT JOIN keeps.
    ("SELECT n.id, c.consensus FROM news_articles n "
     "LEFT JOIN ticker_consensus c ON n.ticker = c.ticker", "LEFT JOIN"),
    # `LEFT JOIN ... WHERE right.col IS NULL` is an ANTI-join: as an inner join
    # it returns the exact COMPLEMENT of the intended rows.
    ("SELECT l.id FROM llm_audit_logs l "
     "LEFT JOIN decision_evaluations e ON l.id = e.decision_id "
     "WHERE e.decision_id IS NULL", "LEFT JOIN"),
    # join_rows sorts the LEFT collection then stitches, so a right-side sort
    # key would be accepted and silently dropped.
    ("SELECT a.note FROM whiteboard_annotations a "
     "JOIN whiteboard_entries e ON e.id = a.entry_id ORDER BY e.created_at",
     "ORDER BY"),
    # Without the schema there is no way to say which side owns a bare column.
    ("SELECT note FROM whiteboard_annotations a "
     "JOIN whiteboard_entries e ON e.id = a.entry_id", "unqualified"),
    ("SELECT * FROM whiteboard_annotations a "
     "JOIN whiteboard_entries e ON e.id = a.entry_id", "SELECT *"),
    # A predicate over both tables is a join condition, not a filter.
    ("SELECT a.note FROM whiteboard_annotations a "
     "JOIN whiteboard_entries e ON e.id = a.entry_id WHERE a.x = e.y",
     "spans both tables"),
    ("SELECT a.note FROM whiteboard_annotations a "
     "JOIN whiteboard_entries e ON e.id = a.entry_id AND e.x = a.y", "equality"),
])
def test_shapes_join_rows_cannot_express_are_refused_by_name(sql, because):
    with pytest.raises(Unsupported) as err:
        translate(sql)
    assert because in str(err.value)


# --------------------------------------------------------------------------
# The oracle
# --------------------------------------------------------------------------

def test_compare_reads_tuple_rows_positionally():
    """The regression that made the checker useless.

    find_rows/agg_row/group_rows/join_rows return TUPLES in the SQL's column
    order -- that shape compatibility is the reason 578 positional call sites
    could be rewritten mechanically. compare() only handled dicts, so
    `if c not in d` tested the tuple's VALUES for a column NAME. It was nearly
    always true, and every such statement was reported as
    "mongo doc missing field '<first column>'" -- 38 of 43 comparable
    statements, none of them a real defect.
    """
    verdict, _ = compare(["ticker", "n"], [("AAPL", 3)], [("AAPL", 3)])
    assert verdict == "MATCH"


def test_compare_still_reads_dict_rows_by_name():
    verdict, _ = compare(["ticker"], [("AAPL",)], [{"ticker": "AAPL"}])
    assert verdict == "MATCH"


def test_compare_catches_a_real_value_difference():
    """The check must be able to FAIL, or MATCH means nothing."""
    verdict, detail = compare(["ticker"], [("AAPL",)], [("MSFT",)])
    assert verdict == "DIFFER", detail


def test_compare_catches_a_wrong_width_row():
    verdict, detail = compare(["a", "b"], [(1, 2)], [(1,)])
    assert verdict == "DIFFER" and "values" in detail


def test_a_single_row_helper_is_wrapped_before_counting():
    """agg_row()/find_row() return ONE row, as fetchone() did.

    Unwrapped, len(tuple) — the COLUMN count — was compared against the
    Postgres ROW count, so a correct one-row translation differed on
    arithmetic that had nothing to do with it.
    """
    assert as_row_list((5, 7), "row") == [(5, 7)]
    assert as_row_list(None, "row") == []
    assert as_row_list([(1,), (2,)], "rows") == [(1,), (2,)]
    verdict, _ = compare(["n", "m"], [(5, 7)], as_row_list((5, 7), "row"))
    assert verdict == "MATCH"
