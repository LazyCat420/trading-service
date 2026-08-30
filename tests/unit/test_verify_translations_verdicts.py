"""The differential oracle must not score the live month as a translation bug.

Postgres froze on 2026-08-19; Mongo has taken every write since. Comparing a
live collection therefore finds rows in Mongo that the archive cannot have, and
the first sweep of scripts/ scored 17 statements DIFFER of which 13 were only
that. A checker whose loudest signal is a known, expected, permanent condition
is a checker nobody reads.

The direction carries the meaning, so it is the direction that is asserted here:

  a row only in Postgres  -> Mongo is MISSING it. Real defect. DIFFER.
  a row only in Mongo     -> written after the cutover. SUPERSET.

Proven red on the pre-change tree: every SUPERSET case below returned DIFFER.
"""
from scripts.verify_translations import compare


def test_identical_result_sets_match():
    v, _ = compare(["ticker"], [("AAPL",), ("MSFT",)], [("AAPL",), ("MSFT",)])
    assert v == "MATCH"


def test_both_empty_matches():
    assert compare(["ticker"], [], [])[0] == "MATCH"


def test_mongo_holding_every_archive_row_plus_more_is_a_superset():
    v, detail = compare(["ticker"], [("AAPL",)], [("AAPL",), ("NVDA",)])
    assert v == "SUPERSET", detail
    assert "after the cutover" in detail


def test_an_empty_archive_against_a_live_collection_is_a_superset():
    # The archive never held this table's rows; Mongo does. Not a bug.
    assert compare(["ticker"], [], [("NVDA",)])[0] == "SUPERSET"


def test_a_row_only_in_postgres_is_a_real_difference():
    v, detail = compare(["ticker"], [("AAPL",), ("MSFT",)], [("AAPL",)])
    assert v == "DIFFER", detail
    assert "only in pg" in detail


def test_a_changed_value_is_a_difference_not_a_superset():
    # Same row count, different content: one row is only in pg AND one only in
    # mongo, which is what a moved singleton looks like. It must stay loud.
    v, detail = compare(["cycle_id"], [("cycle-1",)], [("cycle-2",)])
    assert v == "DIFFER", detail


def test_a_mongo_row_of_the_wrong_width_is_a_difference():
    v, detail = compare(["a", "b"], [(1, 2)], [(1,)])
    assert v == "DIFFER"
    assert "values" in detail


def test_a_dict_missing_the_selected_field_is_a_difference():
    v, detail = compare(["a"], [(1,)], [{"b": 1}])
    assert v == "DIFFER"
    assert "missing field" in detail


def test_an_existence_probe_compares_counts_in_both_directions():
    assert compare(["?column?"], [(1,)], [(1,)])[0] == "MATCH"
    assert compare(["?column?"], [(1,)], [(1,), (1,)])[0] == "SUPERSET"
    v, detail = compare(["?column?"], [(1,), (1,)], [(1,)])
    assert v == "DIFFER"
    assert "FEWER" in detail
