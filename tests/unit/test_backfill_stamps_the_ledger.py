"""A seeded table must leave an artifact saying so.

THE DEFECT
----------
`migration_ledger.json` has carried a `backfilled_at` field for all 190 tables
since it was generated, and as of 2026-08-18 every one of the 161 migrate-scope
rows still had it empty: the writer was never built. So statements like
"135/141 tables seeded" rested on nothing a reader could check — the repo held
no record of which tables had been loaded, when, or whether the row counts
agreed afterwards.

An empty field is worse than a missing one. A reader that checks
`if row.get("backfilled_at")` concludes "not seeded yet" for every table,
whether or not that is true, so the ledger reports a uniform answer that is
independent of reality — it cannot fail.

WHAT THESE TESTS PIN
--------------------
1. A successful backfill stamps `backfilled_at` WITH the counts it verified.
2. A MISMATCHED backfill does NOT stamp it — a failed load must never read as
   a completed one. It records the attempt separately instead.
3. The write is atomic, because the ledger is 406KB of migration state and a
   half-written file during a sweep loses it.
4. Every stamp goes through the same `_LEDGER_PATH` that `table_spec` reads,
   so the writer cannot stamp a file nobody reads.
"""
from __future__ import annotations

import json

import pytest

from app.db import table_spec
from scripts import pg_to_mongo_backfill as bf


@pytest.fixture
def ledger(tmp_path, monkeypatch):
    """A throwaway ledger, so no test mutates the repo's real one."""
    path = tmp_path / "migration_ledger.json"
    path.write_text(json.dumps({
        "tables": [
            {"table": "alpha", "disposition": "migrate", "backfilled_at": None},
            {"table": "beta", "disposition": "migrate", "backfilled_at": None},
        ]
    }), encoding="utf-8")
    monkeypatch.setattr(table_spec, "_LEDGER_PATH", str(path))
    return path


def _row(path, table):
    data = json.loads(path.read_text(encoding="utf-8"))
    return next(r for r in data["tables"] if r["table"] == table)


def test_a_successful_backfill_stamps_the_timestamp_and_the_counts(ledger):
    bf.stamp_backfilled("alpha", pg_rows=1234, mongo_docs=1234, ok=True)

    row = _row(ledger, "alpha")
    assert row["backfilled_at"], "a successful backfill left backfilled_at empty"
    assert row["backfilled_pg_rows"] == 1234
    assert row["backfilled_mongo_docs"] == 1234
    # The counts are the artifact; a bare timestamp would let "seeded" mean
    # "the script ran", not "the rows arrived".
    assert row["backfill_last_attempt"]["ok"] is True


def test_a_mismatch_does_not_stamp_backfilled_at(ledger):
    """The one that matters: a failed load must not read as a completed one."""
    bf.stamp_backfilled("beta", pg_rows=1000, mongo_docs=997, ok=False)

    row = _row(ledger, "beta")
    assert not row["backfilled_at"], (
        "a MISMATCHED backfill stamped backfilled_at — 3 missing rows would "
        "now read as a completed seed"
    )
    # But the attempt is recorded, so the failure is visible rather than silent.
    attempt = row["backfill_last_attempt"]
    assert attempt["ok"] is False
    assert attempt["pg_rows"] == 1000
    assert attempt["mongo_docs"] == 997


def test_only_the_named_table_is_touched(ledger):
    bf.stamp_backfilled("alpha", pg_rows=5, mongo_docs=5, ok=True)
    assert not _row(ledger, "beta")["backfilled_at"], (
        "stamping one table modified another"
    )


def test_an_unknown_table_is_not_invented(ledger, capsys):
    """A typo must not silently add a row that later reads as real."""
    bf.stamp_backfilled("does_not_exist", pg_rows=1, mongo_docs=1, ok=True)

    data = json.loads(ledger.read_text(encoding="utf-8"))
    assert [r["table"] for r in data["tables"]] == ["alpha", "beta"], (
        "an unknown table name was added to the ledger"
    )
    assert "not in the ledger" in capsys.readouterr().err


def test_the_write_is_atomic(ledger, monkeypatch):
    """A crash mid-write must leave the ORIGINAL ledger, not a truncated one."""
    before = ledger.read_text(encoding="utf-8")

    import os as os_module

    def boom(src, dst):
        raise OSError("simulated crash during replace")

    monkeypatch.setattr(os_module, "replace", boom)
    with pytest.raises(OSError):
        bf.stamp_backfilled("alpha", pg_rows=1, mongo_docs=1, ok=True)

    assert ledger.read_text(encoding="utf-8") == before, (
        "the ledger was modified despite the write failing — a crash mid-sweep "
        "would corrupt 406KB of migration state"
    )
    leftovers = list(ledger.parent.glob("*.tmp"))
    assert not leftovers, f"a temp file was left behind: {leftovers}"


def test_the_stamper_writes_the_file_table_spec_reads(monkeypatch, tmp_path):
    """Writer and reader must agree on WHICH ledger file.

    `stamp_backfilled` resolves the path through `table_spec._LEDGER_PATH`
    rather than rebuilding it, so a stamp cannot land in a file no reader
    opens. This asserts the coupling instead of trusting it.
    """
    import inspect

    source = inspect.getsource(bf.stamp_backfilled)
    assert "table_spec._LEDGER_PATH" in source, (
        "stamp_backfilled builds its own path to the ledger; it must use the "
        "same constant table_spec reads"
    )


def test_backfill_calls_the_stamper_but_not_on_a_verify_only_run():
    """`--verify-only` writes nothing, so it must not claim a seed either."""
    import ast
    import inspect

    source = inspect.getsource(bf.backfill)
    tree = ast.parse(source)
    calls = [
        n for n in ast.walk(tree)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
        and n.func.id == "stamp_backfilled"
    ]
    assert calls, "backfill() never stamps the ledger"
    assert "if not verify_only:" in source, (
        "the stamp is not gated on verify_only — a count-only check would "
        "record itself as a completed backfill"
    )
