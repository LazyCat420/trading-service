"""Generated SQL must quote its identifiers.

`cycle_schedules` has a column literally named `analyze`, which Postgres
classifies as fully reserved (`pg_get_keywords().catcode = 'R'`). The generator
interpolated column and table names bare, so the SELECT it built for that table
was a syntax error -- and because the sweep had no per-table recovery, that one
table aborted a 158-table run about fifteen tables in. Every table after it went
unchecked while the run reported a traceback, which is indistinguishable from
"nothing was wrong" if you only read the exit code.

Two properties are pinned here: the quoting itself, and the sweep's refusal to
let one table end the run.
"""

from __future__ import annotations

import pytest

from app.db.table_spec import quote_ident


class TestQuoteIdent:
    def test_a_reserved_word_becomes_usable(self):
        assert quote_ident("analyze") == '"analyze"'

    def test_an_ordinary_name_is_still_quoted(self):
        # Uniform quoting: every name from information_schema is already the
        # real lowercase name, so quoting never changes meaning -- and a rule
        # with no exceptions cannot be applied inconsistently.
        assert quote_ident("ticker") == '"ticker"'

    def test_an_embedded_quote_is_doubled_not_dropped(self):
        assert quote_ident('we"ird') == '"we""ird"'

    def test_a_nul_byte_is_rejected_rather_than_truncated(self):
        with pytest.raises(ValueError):
            quote_ident("bad\x00name")

    @pytest.mark.parametrize(
        "name", ["analyze", "collect", "trade", "timestamp", "close", "order", "user"]
    )
    def test_keyword_columns_all_survive(self, name):
        q = quote_ident(name)
        assert q.startswith('"') and q.endswith('"') and name in q


def test_the_generated_select_quotes_every_name(monkeypatch):
    """The whole point: spec_for must emit quoted identifiers, not bare ones."""
    from app.db import table_spec

    monkeypatch.setattr(table_spec, "key_fields_for", lambda t: ["id"])
    monkeypatch.setattr(
        table_spec,
        "columns_for",
        lambda t, db: [("id", "integer", "int4"), ("analyze", "boolean", "bool")],
    )
    monkeypatch.setattr(table_spec, "uses_decimal128", lambda t: False)

    select_sql, keys, _ = table_spec.spec_for("cycle_schedules", db=None)
    assert '"analyze"' in select_sql
    assert '"cycle_schedules"' in select_sql
    # The bare forms must be gone, or the fix is cosmetic.
    assert ", analyze" not in select_sql
    assert "FROM cycle_schedules" not in select_sql


class TestSweepResilience:
    def test_one_raising_table_does_not_end_the_sweep(self, capsys):
        import importlib.util
        import pathlib
        import sys

        root = pathlib.Path(__file__).resolve().parents[2]
        spec = importlib.util.spec_from_file_location(
            "pg_to_mongo_backfill", root / "scripts" / "pg_to_mongo_backfill.py"
        )
        mod = importlib.util.module_from_spec(spec)
        sys.modules["pg_to_mongo_backfill"] = mod
        spec.loader.exec_module(mod)

        seen = []

        def run(table):
            seen.append(table)
            if table == "boom":
                raise RuntimeError("reserved word")
            return 0

        worst = mod._sweep(["a", "boom", "z"], run)

        assert seen == ["a", "boom", "z"], "the sweep stopped early"
        assert worst == 2, "a raising table must not report success"
        err = capsys.readouterr().err
        assert "boom" in err and "1 of 3" in err
