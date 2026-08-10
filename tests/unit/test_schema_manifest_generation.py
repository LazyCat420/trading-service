"""The schema manifest must see what `information_schema` cannot.

`information_schema` carries tables and columns and NOTHING about partial
indexes, check constraints, triggers, enums, sequences or extensions. A
manifest built from it alone would pronounce a database complete while every
index was missing — the same shape of report that let the 161-vs-214 gap
survive.

These tests exercise the diff over fixtures; they never connect to a database.
"""

from __future__ import annotations

import importlib.util
import pathlib
import sys

import pytest

_SCRIPTS = pathlib.Path(__file__).resolve().parents[2] / "scripts"
spec = importlib.util.spec_from_file_location("generate_schema_manifest", _SCRIPTS / "generate_schema_manifest.py")
gsm = importlib.util.module_from_spec(spec)
sys.modules["generate_schema_manifest"] = gsm
spec.loader.exec_module(gsm)


def _manifest(**over) -> dict:
    base = {
        "manifest_format_version": gsm.MANIFEST_FORMAT_VERSION,
        "table_count": 1,
        "tables": ["news_articles"],
        "columns": {"news_articles": [
            {"name": "id", "type": "text", "nullable": False, "default": None},
            {"name": "published_at", "type": "timestamp with time zone", "nullable": True, "default": None},
        ]},
        "indexes": {"news_articles": ["CREATE INDEX idx_news_pub ON public.news_articles USING btree (published_at)"]},
        "constraints": {"news_articles": [
            {"name": "news_articles_pkey", "type": "p", "definition": "PRIMARY KEY (id)"},
        ]},
        "triggers": {},
        "enums": {"cycle_status": ["idle", "running"]},
        "sequences": [],
        "extensions": ["vector"],
    }
    base.update(over)
    return base


def test_an_identical_database_has_no_problems():
    assert gsm.diff(_manifest(), _manifest()) == []


def test_a_missing_table_is_reported():
    actual = _manifest(tables=[], columns={}, indexes={}, constraints={})
    assert "missing table: news_articles" in gsm.diff(_manifest(), actual)


def test_a_missing_column_is_reported():
    actual = _manifest(columns={"news_articles": [
        {"name": "id", "type": "text", "nullable": False, "default": None},
    ]})
    assert "missing column: news_articles.published_at" in gsm.diff(_manifest(), actual)


def test_a_missing_index_is_reported():
    """The failure information_schema alone cannot see."""
    actual = _manifest(indexes={"news_articles": []})
    problems = gsm.diff(_manifest(), actual)
    assert any(p.startswith("missing index:") for p in problems), problems


def test_a_missing_constraint_is_reported():
    actual = _manifest(constraints={"news_articles": []})
    problems = gsm.diff(_manifest(), actual)
    assert any(p.startswith("missing constraint on news_articles:") for p in problems), problems


def test_a_missing_enum_is_reported():
    assert "missing enum: cycle_status" in gsm.diff(_manifest(), _manifest(enums={}))


def test_an_enum_missing_a_label_is_reported():
    actual = _manifest(enums={"cycle_status": ["idle"]})
    problems = gsm.diff(_manifest(), actual)
    assert any("missing labels" in p for p in problems), problems


def test_a_missing_extension_is_reported():
    assert "missing extension: vector" in gsm.diff(_manifest(), _manifest(extensions=[]))


def test_extra_objects_are_not_failures():
    """A leftover table in the test database is not a reason to fail a build."""
    actual = _manifest(
        tables=["news_articles", "scratch"],
        columns={**_manifest()["columns"], "scratch": [{"name": "x", "type": "text", "nullable": True, "default": None}]},
        extensions=["vector", "pg_stat_statements"],
    )
    assert gsm.diff(_manifest(), actual) == []


def test_the_collector_queries_pg_catalog_and_not_only_information_schema():
    """Structural: the six object classes information_schema cannot supply."""
    import inspect

    src = inspect.getsource(gsm.collect)
    for needed in ("pg_indexes", "pg_constraint", "pg_trigger", "pg_enum", "pg_sequences", "pg_extension"):
        assert needed in src, f"{needed} is not queried — that object class would read as complete"


def test_the_manifest_carries_a_format_version():
    assert gsm.MANIFEST_FORMAT_VERSION == "1.0"
    assert _manifest()["manifest_format_version"] == "1.0"
