"""The write-smoke instrument's own controls.

A smoke test whose tripwire has stopped tripping reports "0 attempts to reach
Postgres" and means nothing by it — which is the exact failure shape this repo
keeps producing, and the reason `gate_zero_pg` reporting 0 couplings on
2026-08-19 sat happily beside a retry query matching 0 of 98 tickers. So the
instrument is held to the same standard it applies: every leg is shown to be
able to FAIL before its passing is worth anything.

No database is touched here. The parts that need one are exercised in the
script itself; the parts that decide whether the script has found something are
pure, and they are what these tests drive.
"""
from __future__ import annotations

import ast
import textwrap
from pathlib import Path

import pytest

from scripts.mongo_write_smoke import (
    _PRODUCTION_DBS,
    Tripwire,
    _point_at_smoke_db,
    _resolve_collections,
    _synthetic,
    census,
)


# ── the tripwire ─────────────────────────────────────────────────────────

def test_the_tripwire_catches_an_import():
    with Tripwire() as wire:
        with pytest.raises(AssertionError, match="blocks Postgres"):
            __import__("psycopg2")
    assert len(wire.attempts) == 1
    assert "import psycopg2" in wire.attempts[0]


def test_the_tripwire_catches_a_lazy_import_inside_a_function():
    """The shape that defeats a module-attribute patch.

    `app/v3/model_shadow._record` does `from app.db import mongo_store` inside
    its own body; the same trick with psycopg would walk straight past a patch
    on an already-imported module. Guarding `__import__` is what closes it.
    """
    def reaches_for_postgres():
        import psycopg  # noqa: F401

    with Tripwire() as wire:
        with pytest.raises(AssertionError):
            reaches_for_postgres()
    assert wire.attempts and "psycopg" in wire.attempts[0]


def test_the_tripwire_catches_the_archive_pool():
    with Tripwire() as wire:
        with pytest.raises(AssertionError):
            __import__("scripts.migration.pg_connection")
    assert wire.attempts


def test_the_tripwire_records_who_tried():
    import traceback as _tb
    from scripts.mongo_write_smoke import REPO as _R
    with Tripwire() as wire:
        with pytest.raises(AssertionError):
            __import__("psycopg2")
    print("\nDBG attempts:", len(wire.attempts))
    for a in wire.attempts:
        print("   ", a)
    assert "test_mongo_write_smoke.py:" in wire.attempts[0], (
        f'"{wire.attempts[0]}" does not name the caller — "something reached '
        'for Postgres" is not actionable')


def test_the_tripwire_lets_everything_else_through_and_restores_itself():
    with Tripwire():
        import json as _json
        assert _json.dumps({"a": 1}) == '{"a": 1}'
    import psycopg2  # noqa: F401  — must work again once disarmed
    assert True


# ── the census ───────────────────────────────────────────────────────────

def _resolve(src: str, expr: str):
    module = ast.parse(textwrap.dedent(src))
    node = ast.parse(expr, mode="eval").body
    return _resolve_collections(node, module)


def test_a_literal_collection_resolves():
    assert _resolve("x = 1", "'positions'") == ["positions"]


def test_a_module_constant_resolves():
    assert _resolve('COMMAND_COLLECTION = "v3_system_commands"',
                    "COMMAND_COLLECTION") == ["v3_system_commands"]


def test_a_loop_over_a_literal_list_resolves_to_every_name():
    """`bot_manager` wipes eleven collections this way, three times over.

    Reporting those as "dynamic" would have called eleven determinate writes
    unaccounted, and the run would have failed for a reason that was not true.
    """
    src = '''
    def reset(bot_id):
        tables_to_clear = ["positions", "trade_fills", "lot_closures"]
        for table in tables_to_clear:
            mongo_store.delete_docs(table, {"bot_id": bot_id})
    '''
    assert _resolve(src, "table") == ["positions", "trade_fills", "lot_closures"]


def test_a_genuinely_dynamic_collection_is_reported_not_guessed():
    src = '''
    def f(name):
        mongo_store.delete_docs(name, {})
    '''
    assert _resolve(src, "name") is None


def test_the_census_finds_writes_and_ignores_reads(tmp_path):
    pkg = tmp_path / "app"
    pkg.mkdir()
    (pkg / "m.py").write_text(textwrap.dedent('''
        from app.db import mongo_store, mongo_query
        def write(d):
            mongo_store.insert_docs("positions", [d])
            mongo_store.upsert_doc("watchlist", {"t": 1}, d)
        def read():
            return mongo_query.find_rows("positions", {}, ["ticker"])
    '''))
    found, unresolved = census([pkg])
    assert unresolved == []
    assert set(found) == {"positions", "watchlist"}, (
        "a READ must not be counted as a write, and both writes must be")


def test_the_census_reports_a_dynamic_write_rather_than_dropping_it(tmp_path):
    pkg = tmp_path / "app"
    pkg.mkdir()
    (pkg / "m.py").write_text(textwrap.dedent('''
        from app.db import mongo_store
        def f(name, d):
            mongo_store.insert_docs(name, [d])
    '''))
    found, unresolved = census([pkg])
    assert found == {}
    assert len(unresolved) == 1 and "m.py:4" in unresolved[0]


def test_the_census_skips_test_trees(tmp_path):
    """Fixture writes are not the application's write surface."""
    pkg = tmp_path / "app"
    (pkg / "tests").mkdir(parents=True)
    (pkg / "tests" / "t.py").write_text(
        'from app.db import mongo_store\n'
        'mongo_store.insert_docs("fixture_only", [{}])\n')
    found, _ = census([pkg])
    assert found == {}


# ── the synthetic document ───────────────────────────────────────────────

def test_the_synthetic_document_exercises_the_declared_coercions():
    """It must go IN as a string, or the round-trip proves nothing.

    If the fixture handed the seam a datetime, the assertion that it comes back
    a datetime would hold whether or not `date_fields` did anything.
    """
    from app.db import date_fields

    coll = next((c for c in ("price_history", "technicals", "shared_desk")
                 if date_fields.date_fields(c) or date_fields.timestamp_fields(c)),
                None)
    assert coll, "no collection in the manifest declares a date field"
    doc = _synthetic(coll, "run1")
    declared = date_fields.date_fields(coll) | date_fields.timestamp_fields(coll)
    assert declared, coll
    for f in declared:
        assert isinstance(doc[f], str), f"{f} must go in as a string"
    assert doc["_smoke_run"] == "run1"


# ── the safety interlock ─────────────────────────────────────────────────

@pytest.mark.parametrize("name", sorted(_PRODUCTION_DBS))
def test_a_production_database_is_refused_by_name(name):
    with pytest.raises(SystemExit, match="production database"):
        _point_at_smoke_db(name)


def test_the_refusal_names_both_production_databases():
    assert _PRODUCTION_DBS == {"trading_bot", "prism"}
