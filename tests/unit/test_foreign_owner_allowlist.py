"""The foreign-owner allowlist must match what treesearch-service declares.

`scripts/pg_quiescence.py` is the instrument that will certify "trading no
longer touches Postgres". It works by diffing `pg_stat_user_tables` per table
and excluding the tables another project owns — because `trading_bot` is a
SHARED database and treesearch-service writes into it forever, so a
database-wide counter can never go quiet.

That exclusion is `quality_gates.FOREIGN_OWNERS`, and on 2026-08-30 it had
drifted from `treesearch-service/src/models/orm.py` in BOTH directions:

  missing   `breeders` (20 rows) and `genetic_relationships` (4,195 rows)
            — ordinary treesearch traffic would be attributed to trading, so
            the probe prints NOT QUIESCENT forever and the retirement can
            never be certified;
  invented  `glass_votes`, which has no `__tablename__` anywhere in
            treesearch-service, and `source_strain_records`, which treesearch
            already DROPs in `docs/data_cleanup.sql`.

An allowlist maintained by hand against a file in another repository drifts
silently. This test reads that file and fails when it does. The file's own
comment says the list must be explicit rather than a name pattern — that stays
true; what changes is that the explicit list is now CHECKED against its source.
"""
from __future__ import annotations

import ast
import pathlib

import pytest

from scripts.quality_gates import FOREIGN_OWNERS, FOREIGN_RETIRED

ORM_REL = pathlib.Path("treesearch-service") / "src" / "models" / "orm.py"


def _find_orm() -> pathlib.Path | None:
    """Walk up looking for the sibling treesearch checkout.

    Walks rather than hardcoding `../` so this passes from a worktree under
    `.worktrees/`, which is where most of this migration was written.
    """
    for parent in pathlib.Path(__file__).resolve().parents:
        candidate = parent / ORM_REL
        if candidate.is_file():
            return candidate
    return None


def _tablenames(source: str) -> set[str]:
    """Every `__tablename__ = "..."` in an ORM module, by AST.

    A regex over the text would also match the string inside a docstring or a
    commented-out model; an assignment is an assignment.
    """
    names: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Assign):
            continue
        if not isinstance(node.value, ast.Constant) or not isinstance(node.value.value, str):
            continue
        for target in node.targets:
            if isinstance(target, ast.Name) and target.id == "__tablename__":
                names.add(node.value.value)
    return names


@pytest.fixture(scope="module")
def declared() -> set[str]:
    orm = _find_orm()
    if orm is None:
        pytest.skip(f"no sibling checkout containing {ORM_REL} — cannot check drift")
    names = _tablenames(orm.read_text())
    assert names, (
        f"{orm} parsed to ZERO __tablename__ assignments. That is the scanner "
        "failing, not treesearch declaring nothing — a comparison against an "
        "empty set would 'pass' by finding no drift."
    )
    return names


def test_the_allowlist_matches_what_treesearch_declares(declared):
    allowed = FOREIGN_OWNERS["treesearch-service"]
    missing = declared - allowed
    invented = allowed - declared
    assert not missing, (
        "treesearch-service declares these tables and the allowlist does not "
        "carry them, so their ordinary traffic reads as a TRADING touch and "
        f"pg_quiescence can never report quiet: {sorted(missing)}"
    )
    assert not invented, (
        "the allowlist carries tables treesearch no longer declares. Either "
        "move them to FOREIGN_RETIRED with a reason, or drop them — an "
        f"allowlist that outlives its source stops guarding anything: {sorted(invented)}"
    )


def test_retired_entries_are_really_retired(declared):
    """The other direction: nothing may sit in FOREIGN_RETIRED while it is
    still declared. That would hide a live foreign table in the 'historical'
    bucket, where nobody looks."""
    still_live = FOREIGN_RETIRED["treesearch-service"] & declared
    assert not still_live, (
        "these are listed as retired but treesearch still declares them; move "
        f"them back into FOREIGN_OWNERS: {sorted(still_live)}"
    )


def test_the_scanner_sees_a_planted_tablename():
    """Negative control: a pass above must mean "no drift", not "the parser
    returned nothing". Mirrors the control in test_no_pg_writers_for_trading_data."""
    planted = _tablenames(
        'class Breeder(Base):\n'
        '    """__tablename__ = "not_this_one" — prose, must not count."""\n'
        '    __tablename__ = "breeders"\n'
        'class Strain(Base):\n'
        '    __tablename__ = "canonical_strains"\n'
    )
    assert planted == {"breeders", "canonical_strains"}, planted
