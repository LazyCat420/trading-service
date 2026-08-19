"""Parity must compare money to the cent, not to a tolerance.

THE DEFECT
----------
`pg_to_mongo_backfill._values_equal` compares any pair where either side is a
float with `math.isclose(rel_tol=1e-9, abs_tol=1e-9)`. That is correct for the
328 DOUBLE PRECISION columns that are genuinely IEEE doubles on both sides.

But `_normalize` ran FIRST and demoted every `Decimal` to `float`:

    if isinstance(v, Decimal):
        return float(v)

so a money column — now stored as Decimal128 precisely so it is exact — had its
exactness thrown away on the way INTO the comparator, and was then compared
with a tolerance. The parity check could not see the drift the Decimal128
migration exists to remove: it would certify a money table as at-parity while
the stored values disagreed once enough operations had accumulated.

This is the "a metric that cannot fail on the reported defect" shape. The fix
is per-column, through the same `table_spec.column_is_money` both the read and
write paths use, so money compares exactly and everything else keeps its
tolerance.
"""
from __future__ import annotations

from decimal import Decimal

import pytest

from scripts.pg_to_mongo_backfill import _values_equal


class FakeDecimal128:
    """Stands in for bson.Decimal128, which is what the Mongo side returns."""

    def __init__(self, value: str) -> None:
        self._v = Decimal(value)

    def to_decimal(self) -> Decimal:
        return self._v


# ── money compares EXACTLY ──────────────────────────────────────────────────

def test_a_sub_cent_money_difference_is_a_mismatch():
    """The regression: float drift in a money column must be caught.

    100000.07 vs 100000.06999999999 is the exact shape float accumulation
    produces, and it passes `math.isclose(abs_tol=1e-9)` — the old comparator
    called these equal.
    """
    pg_value = 100000.07
    mongo_value = FakeDecimal128("100000.06999999999")

    assert not _values_equal(pg_value, mongo_value, money=True), (
        "a money column that drifted below the cent compared EQUAL — the "
        "parity check cannot see the defect it exists to catch"
    )


def test_the_old_tolerance_would_have_passed_it():
    """NEGATIVE CONTROL: proves the case above discriminates.

    The same pair, compared the non-money way, IS equal. Without this, the
    assertion above could be passing because the values differ grossly rather
    than because money is compared exactly.
    """
    assert _values_equal(100000.07, FakeDecimal128("100000.06999999999"),
                         money=False), (
        "the non-money comparator also rejects this pair, so the money test "
        "above is not demonstrating the exactness — pick a difference that is "
        "inside the 1e-9 tolerance"
    )


def test_equal_money_still_compares_equal():
    """Exactness must not turn every money column into a false mismatch.

    The PG side is DOUBLE PRECISION (there is not one NUMERIC column in the
    schema), so it arrives as a float. Routing it through `str()` — not
    `Decimal(float)` — is what makes it match the Decimal128 written from the
    same printed value. `Decimal(100000.07)` is
    100000.0699999999997089616954326629638671875 and would fail here.
    """
    assert _values_equal(100000.07, FakeDecimal128("100000.07"), money=True)
    assert _values_equal(0.01, FakeDecimal128("0.01"), money=True)
    assert _values_equal(-4321.05, FakeDecimal128("-4321.05"), money=True)
    assert _values_equal(0.0, FakeDecimal128("0.00"), money=True)


def test_decimal_on_both_sides_compares_exactly():
    assert _values_equal(Decimal("1.10"), FakeDecimal128("1.1"), money=True)
    assert not _values_equal(Decimal("1.10"), FakeDecimal128("1.11"), money=True)


def test_none_is_still_none():
    """A NULL must not become Decimal('0') — that turns a missing value real."""
    assert _values_equal(None, None, money=True)
    assert not _values_equal(None, FakeDecimal128("0.00"), money=True)
    assert not _values_equal(0.0, None, money=True)


# ── non-money keeps its tolerance ───────────────────────────────────────────

def test_a_ratio_keeps_the_float_tolerance():
    """Ratios and quantities are IEEE doubles on both sides.

    Comparing THOSE exactly would flag the last bit of every stop-loss
    percentage as a parity failure — thousands of false mismatches that would
    bury the real ones.
    """
    assert _values_equal(0.08, 0.08000000001, money=False)
    assert not _values_equal(0.08, 0.09, money=False)


def test_the_default_is_the_tolerant_comparison():
    """`money` defaults to False, so an unclassified column is never compared
    exactly by accident — the safe direction, since a false mismatch on a
    float column is noise and a missed mismatch on money is the bug."""
    assert _values_equal(0.08, 0.08000000001)


# ── the wiring: verify paths ask per column ─────────────────────────────────

@pytest.mark.parametrize("func_name", ["verify_fields", "verify_all"])
def test_both_verifiers_pass_the_per_column_money_flag(func_name):
    """A comparator that supports exactness but is never told which columns
    are money is the same as not having it."""
    import ast
    import inspect

    from scripts import pg_to_mongo_backfill as bf

    source = inspect.getsource(getattr(bf, func_name))
    tree = ast.parse(source)

    calls = [
        n for n in ast.walk(tree)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
        and n.func.id == "_values_equal"
    ]
    assert calls, f"{func_name} does not call _values_equal"
    for call in calls:
        kwargs = {kw.arg for kw in call.keywords}
        assert "money" in kwargs, (
            f"{func_name} calls _values_equal without the `money` flag, so "
            "money columns fall back to the 1e-9 tolerance"
        )


def test_the_money_flag_comes_from_the_shared_policy():
    """It must be `table_spec.column_is_money`, not a local list.

    A second list of money columns in the verifier would drift from the one
    the read and write paths use, and a parity check keyed on the wrong set is
    worse than none — it reports OK for the columns it forgot.
    """
    import inspect

    from scripts import pg_to_mongo_backfill as bf

    for func_name in ("verify_fields", "verify_all"):
        source = inspect.getsource(getattr(bf, func_name))
        assert "table_spec.column_is_money" in source, (
            f"{func_name} does not resolve money through "
            "table_spec.column_is_money"
        )
