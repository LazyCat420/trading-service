"""The migration ledger must cover the manifest exactly, and say why.

A prior session's table classification survived only as five counts. A count
with no membership cannot be diffed and cannot gate a migration, so the ledger
writes the membership down and these tests hold it to the manifest.

Two failures are specifically guarded here because both would read as green:

  * a ledger that has drifted from the manifest -- one extra or one missing
    table means some table migrates unwatched, or a row of the ledger describes
    nothing at all;

  * DDL counted as a reference. Every manifest table has a CREATE TABLE by
    construction, so a classifier that treats DDL as usage returns the empty
    set for `unreferenced` and reports a clean bill of health for 25 dead
    tables. `test_ddl_alone_is_not_a_reference` is the negative control for
    exactly that, and it fails if the DML/DDL split is ever removed.

These tests exercise pure functions and committed JSON; they never connect to a
database and never scan the filesystem for SQL.
"""

from __future__ import annotations

import importlib.util
import json
import pathlib
import sys

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[2]
_SCRIPTS = _ROOT / "scripts"

spec = importlib.util.spec_from_file_location(
    "build_migration_ledger", _SCRIPTS / "build_migration_ledger.py"
)
bml = importlib.util.module_from_spec(spec)
sys.modules["build_migration_ledger"] = bml
spec.loader.exec_module(bml)

MANIFEST_PATH = _ROOT / "app" / "db" / "schema_manifest.json"
LEDGER_PATH = _ROOT / "app" / "db" / "migration_ledger.json"


def _load(path: pathlib.Path) -> dict:
    if not path.exists():
        pytest.fail(
            f"{path.relative_to(_ROOT)} is missing. "
            "Run `python3 scripts/build_migration_ledger.py` and commit the result."
        )
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def ledger() -> dict:
    return _load(LEDGER_PATH)


@pytest.fixture(scope="module")
def manifest() -> dict:
    return _load(MANIFEST_PATH)


# ---------------------------------------------------------------------------
# The coverage gate
# ---------------------------------------------------------------------------


def test_the_ledger_covers_the_manifest_and_says_why_it_exceeds_it(ledger, manifest):
    """Set equality, not counts. A count matching is not the same as covering.

    The ledger is a superset of the manifest: schema.sql declares 183 tables,
    but four live tables are created at runtime by scripts and were therefore
    invisible to the manifest -- and to the migration -- until adopted. Every
    table beyond the manifest has to name its basis, so the excess can never be
    an accident.
    """
    ledger_tables = {rec["table"] for rec in ledger["tables"]}
    manifest_tables = set(manifest["tables"])

    missing = sorted(manifest_tables - ledger_tables)
    assert not missing, f"manifest tables absent from the ledger: {missing}"

    for table in sorted(ledger_tables - manifest_tables):
        rec = next(r for r in ledger["tables"] if r["table"] == table)
        assert rec["scope_basis"] in {"adopted", "retired"}, (
            f"{table} is in the ledger but not the manifest, and claims "
            f"scope_basis={rec['scope_basis']!r}"
        )
        assert rec["scope_evidence"], f"{table} was adopted with no evidence"
        assert rec["owner"], f"{table} was adopted with no owner"


def test_manifest_tables_absent_from_the_database_are_not_migrate(ledger):
    """A table declared in schema.sql but missing from the database cannot be
    migrated, and counting it as scope overstates the work left.

    `rejected_symbols` was the case that made this a rule: it carried a
    migrate disposition and a collection-map entry while having no live table
    at all, so it was the one migrate row with a null row count and the one
    table whose spec could not be generated.
    """
    for rec in ledger["tables"]:
        if rec["disposition"] == "absent":
            assert rec["row_count"] is None, (
                f"{rec['table']} is marked absent but reported rows"
            )
    absent = {r["table"] for r in ledger["tables"] if r["disposition"] == "absent"}
    migrate = {r["table"] for r in ledger["tables"] if r["disposition"] == "migrate"}
    assert not (absent & migrate)


def test_the_ledger_has_no_duplicate_rows(ledger):
    tables = [rec["table"] for rec in ledger["tables"]]
    assert len(tables) == len(set(tables)), "a table appears twice in the ledger"


def test_the_recorded_table_counts_match_the_rows(ledger, manifest):
    """Two counts now, because they answer two questions: how big the manifest
    is, and how big the migration's scope is."""
    assert ledger["manifest_table_count"] == manifest["table_count"]
    assert ledger["scope_table_count"] == len(ledger["tables"])
    assert sum(ledger["scope_basis_counts"].values()) == len(ledger["tables"])
    assert ledger["scope_basis_counts"]["manifest"] == ledger["manifest_table_count"]


def test_the_ledger_is_built_from_committed_code(ledger):
    """Shape drives disposition and collection prefix, so a classifier that
    reads working trees makes the ledger a function of whoever is mid-edit.

    Regenerating with a dirty trading-client used to flip `llm_audit_logs`
    between mutable and append on nothing but another session's uncommitted
    work. The scan reads HEAD; repos that were dirty are named rather than
    silently folded in.
    """
    assert ledger["scanned_at"] == "HEAD"
    assert isinstance(ledger["dirty_repos_ignored"], list)
    assert ledger["working_tree_fallback_repos"] == [], (
        "a repo could not be exported at HEAD and was read from its working "
        f"tree instead: {ledger['working_tree_fallback_repos']}"
    )


def test_the_shape_counts_sum_to_the_table_count(ledger):
    """Every table lands in exactly one shape -- unlike the counts this replaces."""
    assert sum(ledger["shape_counts"].values()) == len(ledger["tables"])


# ---------------------------------------------------------------------------
# Record shape
# ---------------------------------------------------------------------------


def test_every_shape_is_in_the_vocabulary(ledger):
    unknown = sorted({r["shape"] for r in ledger["tables"]} - set(bml.SHAPES))
    assert not unknown, f"shapes outside the vocabulary: {unknown}"


def test_money_tables_get_decimal128_and_nothing_else_does(ledger):
    for rec in ledger["tables"]:
        expected = "dec128" if rec["shape"] == "money" else "float"
        assert rec["numeric_policy"] == expected, rec["table"]


def test_the_money_tables_are_all_present_and_shaped_money(ledger):
    shaped = {r["table"] for r in ledger["tables"] if r["shape"] == "money"}
    assert shaped == set(bml.MONEY_TABLES)


def test_the_timeseries_tables_are_shaped_timeseries(ledger):
    shaped = {r["table"] for r in ledger["tables"] if r["shape"] == "timeseries"}
    assert bml.TIMESERIES_TABLES <= shaped


def test_the_known_queues_are_shaped_queue(ledger):
    shaped = {r["table"] for r in ledger["tables"] if r["shape"] == "queue"}
    assert bml.QUEUE_TABLES <= shaped


def test_dead_tables_are_archive_only_and_live_ones_migrate(ledger):
    """The user decision: an unreferenced table gets a dump and a drop, no Mongo copy.

    Two dispositions override the shape rule, and both are decisions rather
    than classifications: a `retired` table keeps rows nobody writes any more,
    and an `absent` table is declared in schema.sql but not in the database.
    """
    for rec in ledger["tables"]:
        if rec["scope_basis"] == "retired":
            assert rec["disposition"] == "archive-only", rec["table"]
            continue
        if rec["disposition"] == "absent":
            continue
        expected = "archive-only" if rec["shape"] == "unreferenced" else "migrate"
        assert rec["disposition"] == expected, rec["table"]


def test_adopted_tables_declare_the_key_the_database_actually_has(ledger):
    """Adopted tables have no manifest constraint block, so their key is
    declared by hand -- and a hand-declared key is exactly the kind of thing
    that drifts. Checked against the live primary key when the database is
    reachable; skipped, never faked, when it is not.
    """
    # IT ASKS FOR THE ARCHIVE BY NAME. This read `os.getenv("DATABASE_URL")`
    # directly, and that variable was removed from `.env` on 2026-08-30 — so
    # this test stopped FAILING and started SKIPPING, which is worse than
    # either. It had a real finding to report (`agent_tasks` declares
    # key_field='task_id' and the database's primary key is `[]`), and closing
    # the seam made the finding disappear rather than fixing it. A check that
    # goes quiet when the environment changes is the failure this whole
    # migration has been about.
    #
    # `quality_census.pg_url()` is the ONE place that resolves the archive DSN
    # — PG_ARCHIVE_URL from `.env.migration` first — so going through it keeps
    # this alive past the seam close, and keeps it honest: it skips only when
    # there genuinely is no archive to ask.
    try:
        from scripts.quality_census import pg_url
        dsn = pg_url()
    except SystemExit as exc:          # pg_url exits with the fix in the message
        pytest.skip(f"no archive DSN: {exc}")
    try:
        import psycopg
        conn = psycopg.connect(dsn, connect_timeout=8)
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"archive unreachable: {type(exc).__name__}: {exc}")

    adopted = [r for r in ledger["tables"] if r["scope_basis"] == "adopted"]
    assert adopted, "no adopted tables in the ledger"
    with conn, conn.cursor() as cur:
        for rec in adopted:
            cur.execute(
                "select 1 from information_schema.tables where table_schema = 'public' and table_name = %s",
                (rec["table"],),
            )
            if not cur.fetchone():
                continue  # Table absent from Postgres archive (Mongo-native or dropped after cutover)
            cur.execute(
                """select kcu.column_name
                     from information_schema.table_constraints tc
                     join information_schema.key_column_usage kcu
                       on tc.constraint_name = kcu.constraint_name
                      and tc.table_schema = kcu.table_schema
                    where tc.table_schema = 'public'
                      and tc.table_name = %s
                      and tc.constraint_type = 'PRIMARY KEY'
                    order by kcu.ordinal_position""",
                (rec["table"],),
            )
            live_pk = [r[0] for r in cur.fetchall()]
            assert live_pk == [rec["key_field"]], (
                f"{rec['table']}: ledger declares key_field={rec['key_field']!r} "
                f"but the database's primary key is {live_pk}"
            )


def test_lifecycle_stamps_start_null(ledger):
    stamps = (
        "wave",
        "backfilled_at",
        "field_verified_at",
        "promoted_dual",
        "promoted_mongo_read",
        "promoted_mongo",
        "archived_at",
        "dropped_at",
        "archive_file",
    )
    for rec in ledger["tables"]:
        for stamp in stamps:
            assert stamp in rec, f"{rec['table']} has no {stamp} field"


def test_every_record_carries_its_evidence(ledger):
    lists = (
        "service_writers",
        "service_readers",
        "client_writers",
        "client_readers",
        "script_refs",
        "external_refs",
    )
    for rec in ledger["tables"]:
        for key in lists:
            assert isinstance(rec[key], list), f"{rec['table']}.{key}"
            assert len(rec[key]) <= 20, f"{rec['table']}.{key} is not capped"
            assert key in rec["ref_counts"], f"{rec['table']} has no count for {key}"
            assert rec["ref_counts"][key] >= len(rec[key])


def test_an_unreferenced_table_really_has_no_references(ledger):
    """The classification and the evidence must agree, or one of them is lying."""
    for rec in ledger["tables"]:
        if rec["shape"] != "unreferenced":
            continue
        dml = {op: n for op, n in rec["signals"].items() if op in bml.DML_OPS}
        assert not dml, f"{rec['table']} is called unreferenced but shows {dml}"


def test_mode_now_is_a_backend_this_migration_knows(ledger):
    for rec in ledger["tables"]:
        assert rec["mode_now"] in {"pg", "dual", "mongo_read", "mongo"}, rec["table"]


_BACKENDS_ENV = _ROOT / "app" / "db" / "mongo_backends.env"


def test_the_live_backend_flags_reach_the_ledger(ledger):
    """`mode_now` comes from the committed map, not a default of pg for everything."""
    modes = bml.parse_mongo_modes(_BACKENDS_ENV)
    assert modes, "MONGO_STORE_BACKEND was not found in app/db/mongo_backends.env"
    by_table = {r["table"]: r for r in ledger["tables"]}
    for table, mode in modes.items():
        if table in by_table:
            assert by_table[table]["mode_now"] == mode, table


def test_every_non_pg_table_in_the_ledger_is_in_the_backend_map(ledger):
    """The reverse direction: the ledger cannot claim a promotion the map does not.

    Without this, a hand-edited ledger row could report a table as `mongo` while
    the deployed containers still run it at `pg` -- which reads as "migrated".
    """
    modes = bml.parse_mongo_modes(_BACKENDS_ENV)
    for rec in ledger["tables"]:
        if rec["mode_now"] != "pg":
            assert rec["table"] in modes, (
                f"{rec['table']} is {rec['mode_now']} in the ledger but absent "
                "from app/db/mongo_backends.env"
            )
            assert modes[rec["table"]] == rec["mode_now"], rec["table"]


def test_a_missing_backend_map_aborts_instead_of_reporting_everything_pg(tmp_path):
    """The regression guard for the defect this replaced.

    parse_mongo_modes used to return {} when its source did not match, and the
    builder wrote `mode_now: pg` for all 183 rows -- erasing the record of every
    promoted table. A missing or empty map must abort the build instead.
    """
    with pytest.raises(SystemExit):
        bml.parse_mongo_modes(tmp_path / "nope.env")

    empty = tmp_path / "empty.env"
    empty.write_text("# no assignment here\n", encoding="utf-8")
    with pytest.raises(SystemExit):
        bml.parse_mongo_modes(empty)

    bad = tmp_path / "bad.env"
    bad.write_text("MONGO_STORE_BACKEND=pipeline_events:sideways\n", encoding="utf-8")
    with pytest.raises(SystemExit):
        bml.parse_mongo_modes(bad)

    good = tmp_path / "good.env"
    good.write_text(
        "MONGO_STORE_BACKEND=pipeline_events:mongo_read,embeddings:mongo\n",
        encoding="utf-8",
    )
    assert bml.parse_mongo_modes(good) == {
        "pipeline_events": "mongo_read",
        "embeddings": "mongo",
    }


# ---------------------------------------------------------------------------
# Foreign tables
# ---------------------------------------------------------------------------


def test_foreign_tables_never_overlap_the_manifest(ledger, manifest):
    foreign = {f["table"] for f in ledger["foreign_tables"]}
    assert not (foreign & set(manifest["tables"]))


def test_every_foreign_table_names_an_owner_or_is_flagged(ledger):
    for entry in ledger["foreign_tables"]:
        assert entry["owner"], entry["table"]
        assert entry["owner_basis"], entry["table"]


def test_the_unclassified_list_matches_the_foreign_rows(ledger):
    """The loud summary count must be derived from the rows, not asserted beside them."""
    from_rows = sorted(f["table"] for f in ledger["foreign_tables"] if f["owner"] == "UNCLASSIFIED")
    assert sorted(ledger["unclassified_foreign_tables"]) == from_rows
    assert ledger["unclassified_foreign_count"] == len(from_rows)


def test_treesearch_tables_are_owned_by_their_orm():
    """The positive treesearch list is read from the ORM, not guessed from a prefix."""
    tables = bml.treesearch_tables(bml.TREESEARCH_ORM)
    if not tables:
        pytest.skip("treesearch-service is not checked out on this box")
    assert "strain_aliases" in tables
    assert any(t.startswith("glass_") for t in tables)


# ---------------------------------------------------------------------------
# The classifier, over fixtures
# ---------------------------------------------------------------------------


def test_insert_only_is_append():
    assert bml.classify("some_log", {"insert": 3, "select": 9}) == "append"


def test_on_conflict_do_nothing_is_still_append():
    assert bml.classify("some_log", {"insert_ignore": 2}) == "append"


def test_on_conflict_do_update_is_upsert():
    assert bml.classify("some_cache", {"insert": 1, "upsert": 4, "select": 2}) == "upsert"


def test_a_bare_update_is_mutable():
    assert bml.classify("some_state", {"insert": 1, "update": 2}) == "mutable"


def test_a_bare_delete_is_mutable():
    assert bml.classify("some_state", {"insert": 1, "delete": 1}) == "mutable"


def test_the_harder_shape_wins_a_conflict():
    """Upsert plus a bare UPDATE is mutable: the UPDATE is the case needing work."""
    assert bml.classify("both", {"upsert": 9, "update": 1}) == "mutable"


def test_select_only_is_a_reference_table():
    assert bml.classify("some_lookup", {"select": 12, "join": 3}) == "reference"


def test_no_dml_at_all_is_unreferenced():
    assert bml.classify("dead_table", {}) == "unreferenced"


def test_skip_locked_makes_a_queue_even_without_the_hardcoded_list():
    assert bml.classify("not_listed", {"select": 1, "update": 2, "skip_locked": 1}) == "queue"


def test_the_hardcoded_shapes_outrank_the_signals():
    """`positions` is money however busily it is updated."""
    assert bml.classify("positions", {"update": 40, "delete": 9}) == "money"
    assert bml.classify("price_history", {"upsert": 5}) == "timeseries"
    assert bml.classify("scraper_queue", {"insert": 1}) == "queue"


def test_ddl_alone_is_not_a_reference():
    """The negative control for the whole design.

    Every manifest table has a CREATE TABLE. If DDL counted as usage,
    `unreferenced` would be empty and 25 dead tables would read as live.
    """
    ddl_only = {"create_table": 3, "alter_table": 1, "create_index": 4, "drop_table": 1}
    assert bml.classify("only_ever_created", ddl_only) == "unreferenced"


def test_the_dml_set_excludes_ddl():
    assert not (bml.DML_OPS & bml.DDL_OPS)
    assert "create_table" not in bml.DML_OPS
    assert bml.WRITE_OPS <= bml.DML_OPS


# ---------------------------------------------------------------------------
# The SQL scanner, over fixtures
# ---------------------------------------------------------------------------

KNOWN = {"widgets", "gadgets", "job_queue"}


def _ops(sql: str, known=KNOWN) -> set[tuple[str, str]]:
    return {(t, op) for t, op, _line in bml.scan_text(sql, known)}


def test_the_scanner_reads_an_insert():
    assert ("widgets", "insert") in _ops("INSERT INTO widgets (a) VALUES (1)")


def test_the_scanner_separates_do_update_from_do_nothing():
    assert ("widgets", "upsert") in _ops(
        "INSERT INTO widgets (a) VALUES (1) ON CONFLICT (a) DO UPDATE SET a = 2"
    )
    assert ("widgets", "insert_ignore") in _ops(
        "INSERT INTO widgets (a) VALUES (1) ON CONFLICT (a) DO NOTHING"
    )


def test_a_do_update_clause_is_not_read_as_a_bare_update():
    """`DO UPDATE SET` must not register as an UPDATE against some other table."""
    ops = _ops("INSERT INTO widgets (a) VALUES (1) ON CONFLICT (a) DO UPDATE SET a = 2")
    assert ("widgets", "update") not in ops


def test_an_on_conflict_does_not_leak_onto_the_next_insert():
    sql = (
        "INSERT INTO widgets (a) VALUES (1) ON CONFLICT (a) DO UPDATE SET a = 2;\n"
        "INSERT INTO gadgets (b) VALUES (2);\n"
    )
    ops = _ops(sql)
    assert ("widgets", "upsert") in ops
    assert ("gadgets", "insert") in ops
    assert ("gadgets", "upsert") not in ops


def test_the_scanner_reads_update_and_delete():
    ops = _ops("UPDATE widgets SET a = 1 WHERE b = 2; DELETE FROM gadgets WHERE c = 3")
    assert ("widgets", "update") in ops
    assert ("gadgets", "delete") in ops


def test_a_delete_target_is_not_double_counted_as_a_select():
    ops = _ops("DELETE FROM gadgets WHERE c = 3")
    assert ("gadgets", "delete") in ops
    assert ("gadgets", "select") not in ops


def test_the_scanner_reads_selects_and_joins():
    ops = _ops("SELECT * FROM widgets w JOIN gadgets g ON g.id = w.id")
    assert ("widgets", "select") in ops
    assert ("gadgets", "join") in ops


def test_a_python_import_is_not_a_select():
    """`from widgets import x` has no SELECT behind it, so it is not a query."""
    assert _ops("from widgets import thing\n") == set()


def test_ddl_is_recorded_but_tagged_as_ddl():
    ops = _ops(
        "CREATE TABLE IF NOT EXISTS widgets (id text);\n"
        "CREATE INDEX idx_w ON widgets (id);\n"
        "ALTER TABLE widgets ADD COLUMN b text;\n"
    )
    assert ("widgets", "create_table") in ops
    assert ("widgets", "create_index") in ops
    assert ("widgets", "alter_table") in ops
    assert not {op for _t, op in ops} & bml.DML_OPS


def test_a_claim_is_attributed_to_the_table_it_claims():
    """SQL here is split across adjacent string literals, so this cannot be line-scoped."""
    sql = (
        '"SELECT id FROM job_queue WHERE status = \'pending\' "\n'
        '"ORDER BY created_at ASC LIMIT 1 FOR UPDATE SKIP LOCKED"\n'
    )
    assert ("job_queue", "skip_locked") in _ops(sql)


def test_an_unknown_table_name_is_ignored():
    """The known-table filter, not the regexes, is what rejects prose."""
    assert _ops("INSERT INTO not_a_real_table (a) VALUES (1)") == set()


def test_a_commented_out_statement_is_not_a_reference():
    assert _ops("# INSERT INTO widgets (a) VALUES (1)\n") == set()


def test_the_scanner_is_case_insensitive():
    assert ("widgets", "insert") in _ops("insert into widgets (a) values (1)")


# ---------------------------------------------------------------------------
# Key extraction
# ---------------------------------------------------------------------------


def test_the_primary_key_is_read_from_the_manifest_constraints():
    pk, single, uniques = bml.key_fields([
        {"type": "p", "definition": "PRIMARY KEY (id)"},
    ])
    assert pk == "id"
    assert single is None
    assert uniques == []


def test_a_composite_primary_key_keeps_every_column():
    pk, _single, _u = bml.key_fields([
        {"type": "p", "definition": "PRIMARY KEY (ticker, date, source)"},
    ])
    assert pk == "ticker, date, source"


def test_a_single_column_unique_is_surfaced_as_the_natural_key():
    """Often the real document identity; the PK is a surrogate Mongo has no use for."""
    pk, single, uniques = bml.key_fields([
        {"type": "p", "definition": "PRIMARY KEY (id)"},
        {"type": "u", "definition": "UNIQUE (slug)"},
    ])
    assert pk == "id"
    assert single == "slug"
    assert uniques == ["UNIQUE (slug)"]


def test_a_composite_unique_is_recorded_but_is_not_a_natural_key():
    _pk, single, uniques = bml.key_fields([
        {"type": "u", "definition": "UNIQUE (ticker, date)"},
    ])
    assert single is None
    assert uniques == ["UNIQUE (ticker, date)"]


def test_a_unique_index_is_mined_as_well_as_a_unique_constraint():
    """Reading `constraints` alone misses three real document identities."""
    pk, single, uniques = bml.key_fields(
        [{"type": "p", "definition": "PRIMARY KEY (id)"}],
        [
            "CREATE UNIQUE INDEX foo_pkey ON public.foo USING btree (id)",
            "CREATE UNIQUE INDEX foo_cycle_id_key ON public.foo USING btree (cycle_id)",
        ],
    )
    assert pk == "id"
    assert single == "cycle_id"
    assert uniques == ["UNIQUE (cycle_id)"]


def test_the_index_backing_a_primary_key_is_not_a_second_key():
    _pk, single, uniques = bml.key_fields(
        [{"type": "p", "definition": "PRIMARY KEY (id)"}],
        ["CREATE UNIQUE INDEX foo_pkey ON public.foo USING btree (id)"],
    )
    assert single is None
    assert uniques == []


def test_a_partial_unique_index_is_not_a_document_identity():
    """A key that only holds for some rows cannot identify a document."""
    _pk, single, uniques = bml.key_fields(
        [],
        ["CREATE UNIQUE INDEX foo_active ON public.foo USING btree (slug) WHERE (active = true)"],
    )
    assert single is None
    assert uniques == []


def test_a_non_unique_index_is_never_a_key():
    _pk, single, _u = bml.key_fields(
        [], ["CREATE INDEX foo_created ON public.foo USING btree (created_at)"]
    )
    assert single is None


def test_a_constraint_and_its_backing_index_are_not_counted_twice():
    _pk, single, uniques = bml.key_fields(
        [{"type": "u", "definition": "UNIQUE (slug)"}],
        ["CREATE UNIQUE INDEX foo_slug_key ON public.foo USING btree (slug)"],
    )
    assert single == "slug"
    assert uniques == ["UNIQUE (slug)"]


def test_the_ledger_reports_the_external_ref_caveat(ledger):
    """A name match across repos is not proof of a shared database -- say so in the file."""
    assert "name collisions" in ledger["external_refs_caveat"]


def test_the_committed_map_supplies_the_current_backends():
    modes = bml.parse_mongo_modes(_BACKENDS_ENV)
    assert modes.get("embeddings") == "mongo"
    assert set(modes.values()) <= {"pg", "dual", "mongo_read", "mongo"}


def test_deploy_sh_is_no_longer_a_source_of_backend_flags():
    """deploy.sh reads the committed map; it must not carry a map of its own.

    If MONGO_STORE_DEFAULT ever reappears there, two sources disagree and the
    one that wins depends on shell evaluation order -- the exact ambiguity
    phase 0.1 removed.
    """
    text = (_ROOT / "deploy.sh").read_text(encoding="utf-8")
    assert "MONGO_STORE_DEFAULT=" not in text


def test_a_dirty_working_tree_cannot_reach_the_ledger(tmp_path):
    """The scanner must read committed content, not whatever is on disk.

    This is the whole defence: the ledger scans every repo in sun, so before
    this, one parallel session's half-finished edit could re-file a trading
    table -- shape drives disposition and collection prefix alike. Proven on a
    throwaway repo rather than by inspection, because "it reads HEAD" is
    exactly the kind of claim that stays true only until someone adds a
    convenience fallback.
    """
    import subprocess

    repo = tmp_path / "throwaway"
    (repo / "app").mkdir(parents=True)
    target = repo / "app" / "queries.py"
    target.write_text('SQL = "SELECT * FROM committed_table"\n', encoding="utf-8")

    run = lambda *a: subprocess.run(["git", "-C", str(repo), *a],  # noqa: E731
                                    capture_output=True, check=True)
    run("init", "-q")
    run("config", "user.email", "t@t")
    run("config", "user.name", "t")
    run("add", "-A")
    run("commit", "-qm", "committed")

    # The uncommitted edit that must NOT be seen.
    target.write_text('SQL = "SELECT * FROM uncommitted_table"\n', encoding="utf-8")

    bml.DIRTY_REPOS.clear()
    bml._SNAPSHOTS.pop(repo, None)
    snapshot = bml.snapshot_head(repo)

    assert snapshot != repo, "snapshot_head handed back the working tree"
    seen = (snapshot / "app" / "queries.py").read_text(encoding="utf-8")
    assert "committed_table" in seen
    assert "uncommitted_table" not in seen, (
        "the scanner read an uncommitted edit -- the contamination this fixes"
    )
    assert "throwaway" in bml.DIRTY_REPOS, (
        "a dirty repo must be NAMED as ignored, not silently skipped"
    )


def test_the_backfill_addresses_collections_through_the_resolver():
    """The backfill must not index Mongo by the raw table name.

    `mongo_store._coll()` resolves through `collection_for()`, so the day
    `apply_renames` flips, the application moves to the prefixed collection.
    Two sites in the backfill indexed `get_doc_db()[table]` directly and would
    have stayed on the old name: the writer would fill a collection nothing
    reads, and `--verify-all` would read the new one empty and report every
    single row missing -- indistinguishable from total mirror failure, on a
    tool whose whole job is to be believed about parity.

    Inert today (the map is the identity function while renames are off),
    which is exactly why it needed a test rather than a fix and a hope.
    """
    src = (_ROOT / "scripts" / "pg_to_mongo_backfill.py").read_text(encoding="utf-8")
    offenders = [
        line.strip()
        for line in src.splitlines()
        if "get_doc_db()[" in line and "collection_for(" not in line
    ]
    assert not offenders, (
        "these address Mongo by an unresolved name:\n  " + "\n  ".join(offenders)
    )
