"""What SQL's NULL predicates mean once they are Mongo filters.

The conversion has to answer the same three questions over and over — is this
`IS NOT NULL`? does this COUNT skip nulls? does this comparison see a missing
field? — and the answers are folklore until something checks them. During the
2026-08-18 conversion pass a claim entered the code comments that
`{'$ne': None}` also matches documents MISSING the field and therefore needs
`$exists: True` to mean `IS NOT NULL`. Measured against the live server, that
is false: `{'$ne': None}` already excludes missing fields.

The claim was harmless in effect (the extra clause changes nothing) and
dangerous in propagation: it is the kind of asserted-but-unmeasured rule that
the next reader applies somewhere it does matter. So the rules get a test.

These run against a real MongoDB because that is the only authority on its own
matcher; a hand-written fake would encode the same folklore it is meant to
check. They are marked `real_mongo` and skip unless TRADING_BOT_MONGO_TEST=1.
"""
import pytest

pytestmark = pytest.mark.real_mongo


@pytest.fixture
def coll(real_mongo):
    c = real_mongo["null_semantics"]
    c.delete_many({})
    c.insert_many([
        {"k": "has_value", "f": 1},
        {"k": "explicit_null", "f": None},
        {"k": "field_missing"},
    ])
    yield c
    c.delete_many({})


def _keys(cursor):
    return sorted(d["k"] for d in cursor)


def test_ne_null_already_excludes_a_missing_field(coll):
    """`{'$ne': None}` IS `IS NOT NULL`; it does not need $exists.

    This is the claim that entered the code as a comment saying the opposite.
    """
    assert _keys(coll.find({"f": {"$ne": None}})) == ["has_value"]


def test_adding_exists_changes_nothing(coll):
    """The belt-and-braces form is equivalent, not a correction."""
    bare = _keys(coll.find({"f": {"$ne": None}}))
    braced = _keys(coll.find({"f": {"$ne": None, "$exists": True}}))
    assert bare == braced == ["has_value"]


def test_eq_null_matches_both_explicit_null_and_missing(coll):
    """The asymmetry that makes the above surprising.

    `{'f': None}` DOES match a missing field, so `IS NULL` is the predicate
    that needs care, not `IS NOT NULL`. A conversion that reaches for $exists
    is usually reaching on the wrong side.
    """
    assert _keys(coll.find({"f": None})) == ["explicit_null", "field_missing"]
    assert _keys(coll.find({"f": {"$exists": False}})) == ["field_missing"]


def test_count_of_a_field_skips_nulls_like_sql(coll):
    """SQL COUNT(col) skips NULLs while COUNT(*) does not — the Mongo
    equivalent has to be written deliberately, one filter per meaning."""
    assert coll.count_documents({}) == 3                       # COUNT(*)
    assert coll.count_documents({"f": {"$ne": None}}) == 1      # COUNT(f)


def test_a_missing_field_sorts_with_null_not_last(coll):
    """DESC sorts put missing/null FIRST in Mongo, as they do in Postgres
    without NULLS LAST. A latest-per-key conversion that forgets this picks
    the null row as 'newest'."""
    got = [d["k"] for d in coll.find({}).sort("f", -1)]
    assert got[0] == "has_value"
    assert set(got[1:]) == {"explicit_null", "field_missing"}


def test_ne_against_a_value_and_ne_against_null_differ(coll):
    """The asymmetry that makes this whole area a trap, measured both ways.

    `{'f': {'$ne': None}}`  EXCLUDES a document missing the field.
    `{'f': {'$ne': "x"}}`   INCLUDES one.

    They read alike. During the 2026-08-18 conversion the difference was
    asserted wrongly in BOTH directions on the same day — a comment claiming
    $ne:None needs $exists to mean IS NOT NULL (it does not), and this test's
    own first draft predicting that $ne:"system" drops missing rows (it does
    not). Only the server settles it, so both halves are pinned here.

    The practical consequence: `COALESCE(job_type,'user') <> 'system'` ports
    to a bare `{'$ne': 'system'}` and is correct, while
    `resolved_at IS NOT NULL` ports to a bare `{'$ne': None}` and is also
    correct — but swapping the reasoning between them produces a filter that
    silently drops or admits a whole population.
    """
    coll.delete_many({})
    coll.insert_many([
        {"k": "user_row", "job_type": "user"},
        {"k": "system_row", "job_type": "system"},
        {"k": "null_row", "job_type": None},
        {"k": "missing_row"},
    ])

    # $ne against a VALUE: null and missing both come back.
    assert _keys(coll.find({"job_type": {"$ne": "system"}})) == [
        "missing_row", "null_row", "user_row"
    ]
    # $ne against NULL: only the rows that actually hold a value.
    assert _keys(coll.find({"job_type": {"$ne": None}})) == ["system_row", "user_row"]
