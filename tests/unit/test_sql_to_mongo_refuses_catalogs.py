"""A Postgres system catalog must be refused, not translated.

THE DEFECT
----------
`pg_tables`, `pg_class` and `information_schema.columns` describe the DATABASE,
not the application's data. Mongo has no equivalent, so a translation of them
is not a port — it is a query against a collection that does not exist:

    SELECT tablename FROM pg_tables WHERE schemaname = 'public'
 -> mongo_query.find_rows('pg_tables', {'schemaname': 'public'}, ['tablename'])

Valid code. No error. An empty list, forever. Every caller that iterates the
result simply does nothing, and "list the tables in this database" reports a
clean, empty database.

Found on 2026-08-19 in trading-client, where the codemod was about to rewrite
**10 such call sites** across the schema browser, the ontology builder, the
data-audit sweep and the pipeline router. None of them would have raised.

`columns` is the dangerous one: sqlglot hands back `information_schema.columns`
as the bare name `columns`, which reads like an ordinary application table.

The correct port is `db.list_collection_names()` — a hand transform, not a
query rewrite. So the translator refuses and says so.
"""
from __future__ import annotations

import pytest

# `sqlglot` is MIGRATION TOOLING, not an application dependency: 467c77b pinned
# it in requirements-migration.in and 6bc835f took it out of the app image with
# the rest of the Postgres teardown. Importing it unguarded at module scope
# turns "the optional tool is absent" into a COLLECTION ERROR, and a collection
# error aborts the ENTIRE run — `pytest tests` stopped before executing a single
# test, so the full suite has been unrunnable without
# --continue-on-collection-errors. Skip cleanly instead.
pytest.importorskip("sqlglot", reason="migration tooling; see requirements-migration.in")

from scripts.sql_to_mongo import Unsupported, translate


@pytest.mark.parametrize("sql", [
    "SELECT tablename FROM pg_tables WHERE schemaname = 'public'",
    "SELECT relname FROM pg_class",
    "SELECT * FROM pg_stat_user_tables",
    "SELECT indexname FROM pg_indexes WHERE tablename = %s",
    "SELECT count(*) FROM pg_stat_activity",
    "SELECT column_name, data_type FROM information_schema.columns WHERE table_name = %s",
    "SELECT table_name FROM information_schema.tables",
])
def test_a_system_catalog_is_refused(sql):
    with pytest.raises(Unsupported) as exc:
        translate(sql)
    assert "system catalog" in str(exc.value), (
        f"refused for the wrong reason: {exc.value}"
    )


def test_the_refusal_names_the_replacement():
    """A refusal that does not say what to do instead gets worked around."""
    with pytest.raises(Unsupported) as exc:
        translate("SELECT tablename FROM pg_tables")
    assert "list_collection_names" in str(exc.value)


@pytest.mark.parametrize("sql", [
    "SELECT id, ticker FROM positions WHERE bot_id = %s",
    "SELECT id FROM watchlist",
    "SELECT ticker, close FROM price_history WHERE ticker = %s",
    "SELECT cash_balance FROM bots WHERE bot_id = %s",
    "INSERT INTO watchlist (ticker, status) VALUES (%s, %s)",
    "UPDATE bots SET cash_balance = %s WHERE bot_id = %s",
    "DELETE FROM analysis_results WHERE cycle_id = %s",
])
def test_ordinary_tables_still_translate(sql):
    """NEGATIVE CONTROL: the refusal must not be a blanket one.

    Without this, `_SYSTEM_CATALOGS` growing a name that collides with a real
    application table — or the matcher becoming too broad — would silently stop
    the conversion dead, and the only symptom would be the codemod's skip count
    rising.
    """
    translate(sql)  # must not raise


def test_a_table_merely_named_like_a_catalog_column_is_fine():
    """`tables` and `columns` are refused; a table whose NAME contains them is not.

    The matcher is exact-name, not substring — `ticker_metadata` and
    `analysis_results` must not be caught by a loose `in` check on "tables".
    """
    translate("SELECT ticker FROM ticker_metadata WHERE ticker = %s")
    translate("SELECT cycle_id FROM analysis_results WHERE cycle_id = %s")


def test_a_catalog_in_a_join_is_also_refused():
    """The scan walks every table in the tree, not just the FROM target.

    A catalog joined into an otherwise ordinary query is the same defect —
    half the result comes from a collection that does not exist.
    """
    with pytest.raises(Unsupported):
        translate(
            "SELECT t.tablename, c.column_name FROM pg_tables t "
            "JOIN information_schema.columns c ON c.table_name = t.tablename"
        )


def test_the_codemod_skips_these_rather_than_rewriting_them():
    """End to end: the refusal has to reach the codemod, not stop at the translator.

    A translator that refuses while the codemod rewrites anyway would be a
    guard with no effect on the thing it guards.
    """
    from scripts.codemod_pg_to_mongo import literal_params  # noqa: F401

    # The codemod calls translate() and treats Unsupported as a skip; assert the
    # exception type it catches is the one raised here, so a refactor that
    # narrows either side is caught.
    with pytest.raises(Unsupported):
        translate("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
