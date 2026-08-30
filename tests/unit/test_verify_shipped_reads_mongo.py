"""`verify_shipped.check_database` reads Mongo, and cannot pass while blind.

TWO FAILURES IN ONE FUNCTION, both measured 2026-08-30.

1. **It read the frozen archive.** `check_database` opened
   `scripts.migration.pg_connection.get_db` to count `model_shadow_runs` and to
   find the newest local-box call in `llm_audit_logs`. Both moved to Mongo at
   the 2026-08-19 cutover, so between 08-19 and 08-28 this graded a shipped
   deploy against a store that had stopped being written — silently, with rows
   that looked current.

2. **It failed OPEN.** After `settings.DATABASE_URL` was removed on 08-28 the
   import raised `AttributeError`, and a bare `except Exception` turned that
   into `WARN "unreadable"` and let the script exit 0. An acceptance check that
   reports "I could not read the database" and then passes is not a check —
   and this is the tool a session runs to decide whether a deploy landed.

Both directions are pinned here: the reads must go through `app.db.mongo_query`,
and a read that raises must land as FAIL. The second test is the one that would
have caught the outage; it is written as a proper negative control (an injected
failure), not as a source-text grep, because the WARN was correct *code* — it
was the wrong *verdict*.
"""
from __future__ import annotations

import importlib.util
import pathlib
from unittest.mock import patch

import pytest

REPO = pathlib.Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def vs():
    spec = importlib.util.spec_from_file_location(
        "verify_shipped_under_test", REPO / "scripts" / "verify_shipped.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_it_reads_the_two_ledgers_from_mongo(vs):
    from app.db import mongo_query

    with patch.object(mongo_query, "group_rows", return_value=[
        ("v3_portfolio_manager", "SUCCESS", 12),
    ]) as group, patch.object(mongo_query, "agg_row", return_value=(None,)) as agg:
        rep = vs.Report()
        vs.check_database(rep)

    assert group.call_args[0][0] == "model_shadow_runs", group.call_args
    assert agg.call_args[0][0] == "llm_audit_logs", agg.call_args
    # ILIKE '%jetson%' OR = 'vllm' must survive as a case-insensitive $or, or
    # the "has the local box been used" line answers about nothing.
    query = agg.call_args[0][1]
    assert query == {"$or": [
        {"endpoint_name": {"$regex": "jetson", "$options": "i"}},
        {"endpoint_name": "vllm"},
    ]}, query

    verdicts = {r["claim"]: r["status"] for r in rep.rows}
    assert verdicts["Gatekeeper shadow rows (the blocking measurement)"] == vs.PASS


def test_an_unreadable_database_is_a_failure_not_a_warning(vs):
    """NEGATIVE CONTROL for the fail-open. The old code turned this exact
    exception into WARN and exited 0."""
    from app.db import mongo_query

    with patch.object(mongo_query, "group_rows",
                      side_effect=AttributeError("'Settings' object has no attribute 'DATABASE_URL'")):
        rep = vs.Report()
        vs.check_database(rep)

    rows = [r for r in rep.rows if r["claim"] == "Database state"]
    assert len(rows) == 1, rep.rows
    assert rows[0]["status"] == vs.FAIL, (
        "an acceptance check that cannot read the database must not pass; "
        f"got {rows[0]}"
    )
    assert "AttributeError" in rows[0]["detail"], (
        "the report must name the exception TYPE — the whole incident was a "
        "message-shaped classification hiding what actually went wrong"
    )


def test_check_database_does_not_import_the_archive(vs):
    """The archive connection is allow-listed for migration/parity tooling.
    `verify_shipped` is neither: it grades the LIVE deploy."""
    import ast
    import inspect

    src = inspect.getsource(vs.check_database)
    tree = ast.parse(src.lstrip())
    imported = {
        (node.module or "")
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
    }
    assert not any("pg_connection" in m for m in imported), imported
    assert "app.db" in imported or any(m.startswith("app.db") for m in imported), imported
