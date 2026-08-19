"""The collection map, its resolver, and the validator that keeps them honest.

Two properties matter more than the names themselves.

**The map ships inert.** A rename is a data move, not a code change. The 13
collections holding data are named for their tables; the moment collection_for
returns a new name, a running container reads a collection that does not exist
-- and Mongo creates it empty on first write rather than erroring. Nothing
raises, the dashboard just goes blank. So `apply_renames` is false until a
coordinated stop/rename/deploy, and these tests pin that.

**The validator can fail.** A hand-authored map needs a machine to check it,
and a checker nobody has watched fail is not a checker. Every rule below is
sabotaged separately.
"""

from __future__ import annotations

import importlib.util
import json
import pathlib
import sys

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[2]
_SCRIPT = _ROOT / "scripts" / "check_collection_map.py"
_MAP = _ROOT / "app" / "db" / "collection_map.json"

spec = importlib.util.spec_from_file_location("check_collection_map", _SCRIPT)
ccm = importlib.util.module_from_spec(spec)
sys.modules["check_collection_map"] = ccm
spec.loader.exec_module(ccm)


# ── the shipped state ──────────────────────────────────────────────────────

class TestTheMapShipsInert:
    def test_apply_renames_is_false(self):
        d = json.loads(_MAP.read_text(encoding="utf-8"))
        assert d["apply_renames"] is False, (
            "the map is active; activating it is a coordinated stop/rename/deploy "
            "of BOTH repos, not a code commit"
        )

    def test_collection_for_is_the_identity_function_while_inert(self):
        from app.db import collections as C

        C.reset_cache()
        assert not C.renames_active()
        for table in ("pipeline_events", "trade_results", "price_history", "positions"):
            assert C.collection_for(table) == table

    def test_the_targets_are_still_visible_while_inert(self):
        """Inert must not mean invisible -- the rename tooling reads these."""
        from app.db import collections as C

        C.reset_cache()
        assert C.target_collection_for("pipeline_events") == "log_pipeline_events"
        assert C.target_collection_for("v3_system_commands") == "q_commands"
        # The legacy queue must NOT collide with the current one.
        assert C.target_collection_for("system_commands") == "q_autoresearch_commands"

    def test_an_unmapped_table_falls_through_rather_than_raising(self):
        from app.db import collections as C

        C.reset_cache()
        assert C.collection_for("txn_scratch_test") == "txn_scratch_test"


class TestTheRealMapIsValid:
    def test_the_committed_map_passes(self):
        assert ccm.main([]) == 0

    def test_every_migrate_table_is_mapped(self):
        from app.db import collections as C

        C.reset_cache()
        ledger = json.loads(
            (_ROOT / "app" / "db" / "migration_ledger.json").read_text(encoding="utf-8")
        )
        migrate = {t["table"] for t in ledger["tables"] if t["disposition"] == "migrate"}
        assert set(C.all_tables()) == migrate

    def test_the_mapping_is_injective(self):
        from app.db import collections as C

        C.reset_cache()
        assert len(C.all_collections()) == len(set(C.all_collections()))
        assert len(C.all_collections()) == len(C.all_tables())


# ── the validator must be able to fail ─────────────────────────────────────

@pytest.fixture
def tree(tmp_path, monkeypatch):
    (tmp_path / "app" / "db").mkdir(parents=True)
    for src in (_MAP, _ROOT / "app" / "db" / "migration_ledger.json"):
        (tmp_path / "app" / "db" / src.name).write_bytes(src.read_bytes())
    monkeypatch.setattr(ccm, "REPO_ROOT", tmp_path)
    return tmp_path


def _edit(tree, fn):
    p = tree / "app" / "db" / "collection_map.json"
    d = json.loads(p.read_text())
    fn(d)
    p.write_text(json.dumps(d))


def test_sabotage_unmapped_table_fails(tree):
    _edit(tree, lambda d: d["collections"].pop("watchlist"))
    assert ccm.main([]) == 1


def test_sabotage_two_tables_one_collection_fails(tree):
    """In Mongo this does not error -- it silently merges two entities."""
    _edit(tree, lambda d: d["collections"]["watchlist"].__setitem__(
        "collection", d["collections"]["positions"]["collection"]))
    assert ccm.main([]) == 1


def test_sabotage_missing_prefix_fails(tree):
    _edit(tree, lambda d: d["collections"]["watchlist"].__setitem__(
        "collection", "watchlist_entries"))
    assert ccm.main([]) == 1


def test_sabotage_version_token_fails(tree):
    """`v3_` is how the current naming mess started."""
    _edit(tree, lambda d: d["collections"]["watchlist"].__setitem__(
        "collection", "state_v2_watchlist"))
    assert ccm.main([]) == 1


def test_sabotage_unexplained_prefix_shape_mismatch_fails(tree):
    def f(d):
        e = d["collections"]["watchlist"]
        e["collection"] = "log_watchlist"      # shape is mutable -> state_
        e.pop("rename_reason", None)
    _edit(tree, f)
    assert ccm.main([]) == 1


def test_an_explained_prefix_shape_mismatch_passes(tree):
    """The rule is 'state your reason', not 'never deviate' -- shapes drift."""
    def f(d):
        e = d["collections"]["watchlist"]
        e["collection"] = "log_watchlist"
        e["rename_reason"] = "deliberate, for this test"
    _edit(tree, f)
    assert ccm.main([]) == 0


def test_sabotage_money_outside_ledger_prefix_fails(tree):
    _edit(tree, lambda d: d["collections"]["watchlist"].__setitem__(
        "numeric_policy", "dec128"))
    assert ccm.main([]) == 1


def test_sabotage_ledger_collection_without_dec128_fails(tree):
    _edit(tree, lambda d: d["collections"]["positions"].__setitem__(
        "numeric_policy", "float"))
    assert ccm.main([]) == 1


def test_the_shipped_map_states_a_reason_wherever_it_overrides_the_ledger(tree):
    """`uses_decimal128()` reads the map FIRST and the ledger only as a
    fallback, so a map entry can demote a money table to float and nothing
    would disagree out loud: the write path stops storing Decimal128, the read
    path stops unwrapping it, and both halves agree, in float, about the cash.

    Two live overrides exist and both are deliberate — `bots` promoted to
    dec128 against a generated `float`, `trade_results` demoted to float on
    ch.64's reasoning that its numbers are decision parameters rather than
    settled amounts. Each carries its reason, so the checker passes as shipped.
    """
    assert ccm.main([]) == 0


def test_sabotage_a_silent_money_downgrade_fails(tree):
    """Strip the REASON, not the policy: the override stays, its justification
    goes. That is the shape a silent demotion actually arrives in."""
    _edit(tree, lambda d: d["collections"]["trade_results"].pop(
        "numeric_policy_reason", None))
    assert ccm.main([]) == 1


def test_sabotage_a_new_undocumented_override_fails(tree):
    def _flip(d):
        d["collections"]["positions"]["numeric_policy"] = "float"
        d["collections"]["positions"].pop("numeric_policy_reason", None)
    _edit(tree, _flip)
    assert ccm.main([]) == 1


def test_an_override_that_agrees_with_the_ledger_needs_no_reason(tree):
    """NEGATIVE CONTROL: the rule fires on DISAGREEMENT, not on the presence of
    a numeric_policy — every one of the 161 entries carries one."""
    def _agree(d):
        d["collections"]["bots"]["numeric_policy"] = "float"
        d["collections"]["bots"].pop("numeric_policy_reason", None)
        d["collections"]["bots"]["collection"] = "state_bots"   # dec128 outside ledger_* is rule 5
    _edit(tree, _agree)
    assert ccm.main([]) == 0


def test_a_missing_file_is_exit_2(tree):
    (tree / "app" / "db" / "collection_map.json").unlink()
    assert ccm.main([]) == 2
