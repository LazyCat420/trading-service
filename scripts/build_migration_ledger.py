#!/usr/bin/env python3
"""Derive the Postgres->MongoDB migration ledger mechanically, from evidence.

    python3 scripts/build_migration_ledger.py            # write app/db/migration_ledger.json
    python3 scripts/build_migration_ledger.py --no-db    # skip the live row counts
    python3 scripts/build_migration_ledger.py --dry-run  # print the summary, write nothing

WHY THIS EXISTS
---------------
A prior session hand-derived a table classification (68 append-only, 58 upsert,
59 mutable, 10 read-only, 46 unreferenced) and only the COUNTS survived: the
membership existed nowhere on disk. A count with no membership cannot be
checked, cannot be diffed, and cannot gate a migration. So this script derives
the classification from the source that is actually authoritative -- the SQL in
the two repos that own the schema -- and writes the membership down.

The remembered numbers overlap (they sum to 241 over 183 tables) because a
table can carry several signals at once. This script records EVERY signal it
saw (`signals`) alongside the single `shape` it resolved to, so a disagreement
with any earlier count is inspectable rather than a matter of opinion. The
classifier is NOT tuned to reproduce those numbers.

WHAT IS AUTHORITATIVE HERE
--------------------------
`app/db/schema_manifest.json` (183 tables) is the list of TRADING-OWNED tables.
It is generated from what this repository itself creates and deliberately
excludes foreign tables belonging to other services sharing the Postgres
instance. The ledger's table set is exactly the manifest's table set -- see
`tests/unit/test_migration_ledger.py`, which fails if they ever diverge.

The live database carries MORE than the manifest. Every live public table that
is neither in the manifest nor on a positive owner list is emitted under
`foreign_tables` with `owner: "UNCLASSIFIED"`. Those block DROPs: you cannot
drop a table whose owner you cannot name.

`external_refs` MATCHES ON NAME, NOT ON DATABASE
------------------------------------------------
Refs from other sun repos are found by table NAME, and a name is not a
database. `chat_sessions` shows 13 hits in `HTML-Notes/app/database.py`, which
opens **sqlite3** -- a different store that happens to use the same word.
`episodic_memory` shows 91 hits in a vendored TypeScript memory engine, likewise
its own store. Treat `external_refs` as a list of places to CHECK, never as
proof that another service reads the trading Postgres. The refs that do matter
are the ones in `postgres-service/scripts/schema_pg.sql`, which is the shared
schema for this very instance.

DDL IS NOT A REFERENCE
----------------------
Every manifest table has a `CREATE TABLE` in `app/db/schema_pg.sql` by
construction -- that is where the manifest comes from. Counting DDL as a
reference would make `unreferenced` the empty set and the whole ledger useless.
So DDL (CREATE/ALTER/DROP TABLE, CREATE INDEX ... ON) is recorded as evidence
but excluded from the referenced/unreferenced decision, which reads DML only.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:  # pragma: no cover - dotenv is in requirements, absence is not fatal
    pass

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUT = REPO_ROOT / "app" / "db" / "migration_ledger.json"
MANIFEST_PATH = REPO_ROOT / "app" / "db" / "schema_manifest.json"

# The two repos that own trading SQL. Deliberately the PRIMARY checkouts, not a
# worktree: the ledger describes what ships, and a worktree is a work surface.
SUN_ROOT = Path("/home/lazycat/github/projects/sun")
SERVICE_REPO = SUN_ROOT / "trading-service"
CLIENT_REPO = SUN_ROOT / "trading-client"
TREESEARCH_ORM = SUN_ROOT / "treesearch-service" / "src" / "models" / "orm.py"

LEDGER_FORMAT_VERSION = "1.0"

DSN = os.environ.get(
    "DATABASE_URL", "postgresql://trader:trading_bot_pass@10.0.0.16:5433/trading_bot"
)

# ---------------------------------------------------------------------------
# Hard-coded shapes. These are decisions, not derivations, and are listed here
# so they are visible rather than buried in the classifier.
# ---------------------------------------------------------------------------

# Written by an append path but read as a series; Mongo wants a time-series
# collection, not a document per row shoved into a generic store.
TIMESERIES_TABLES = frozenset({
    "price_history",
    "price_backfill_progress",
    "asset_prices",
})

# Money. These get Decimal128 and never float, whatever their write shape is.
MONEY_TABLES = frozenset({
    "positions",
    "position_lots",
    "lot_closures",
    "trade_fills",
    "orders",
    "portfolio_snapshots",
    "trade_results",
})

# Claim-then-complete work queues. A queue's correctness lives in its claim,
# so it migrates differently from anything else regardless of its DML mix.
QUEUE_TABLES = frozenset({
    "v3_system_commands",
    "scraper_queue",
    "sub_task_queue",
    "v3_research_queues",
    "evolution_repair_queue",
})

SHAPES = (
    "append",
    "upsert",
    "mutable",
    "reference",
    "queue",
    "money",
    "timeseries",
    "unreferenced",
)

# ---------------------------------------------------------------------------
# SQL scanning
# ---------------------------------------------------------------------------

SCAN_SUFFIXES = {".py", ".sql", ".js", ".jsx", ".ts", ".tsx"}
SKIP_DIR_PARTS = {
    "node_modules",
    "__pycache__",
    ".venv",
    "venv",
    ".git",
    "dist",
    "build",
    ".next",
    "site-packages",
    ".mypy_cache",
    ".pytest_cache",
}

# A table name as it appears after a SQL keyword: optionally schema-qualified,
# optionally quoted. Kept deliberately loose -- every capture is then filtered
# against the known-table set, which is what actually rejects prose.
_T = r'["`\']?(?:public\.)?["`]?([A-Za-z_][A-Za-z0-9_]*)["`]?'

RE_INSERT = re.compile(r"\bINSERT\s+INTO\s+" + _T, re.IGNORECASE)
RE_UPDATE = re.compile(r"\bUPDATE\s+(?:ONLY\s+)?" + _T + r"\s+SET\b", re.IGNORECASE)
RE_DELETE = re.compile(r"\bDELETE\s+FROM\s+" + _T, re.IGNORECASE)
RE_TRUNCATE = re.compile(r"\bTRUNCATE\s+(?:TABLE\s+)?" + _T, re.IGNORECASE)
RE_FROM = re.compile(r"\bFROM\s+" + _T, re.IGNORECASE)
RE_JOIN = re.compile(r"\bJOIN\s+" + _T, re.IGNORECASE)
RE_CREATE_TABLE = re.compile(
    r"\bCREATE\s+(?:UNLOGGED\s+|TEMP\s+|TEMPORARY\s+)?TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?" + _T,
    re.IGNORECASE,
)
RE_ALTER_TABLE = re.compile(
    r"\bALTER\s+TABLE\s+(?:IF\s+EXISTS\s+)?(?:ONLY\s+)?" + _T, re.IGNORECASE
)
RE_DROP_TABLE = re.compile(r"\bDROP\s+TABLE\s+(?:IF\s+EXISTS\s+)?" + _T, re.IGNORECASE)
RE_CREATE_INDEX = re.compile(
    r"\bCREATE\s+(?:UNIQUE\s+)?INDEX\s+(?:CONCURRENTLY\s+)?(?:IF\s+NOT\s+EXISTS\s+)?"
    r"[A-Za-z0-9_\"`.]+\s+ON\s+" + _T,
    re.IGNORECASE,
)
RE_SKIP_LOCKED = re.compile(r"\bSKIP\s+LOCKED\b", re.IGNORECASE)
RE_ON_CONFLICT = re.compile(r"\bON\s+CONFLICT\b", re.IGNORECASE)
RE_DO_UPDATE = re.compile(r"\bDO\s+UPDATE\b", re.IGNORECASE)
RE_SELECT_BEHIND = re.compile(r"\bSELECT\b", re.IGNORECASE)

# How far past an INSERT to look for its ON CONFLICT clause, and how far back
# from a SKIP LOCKED to find the table it claims. SQL here is routinely split
# across adjacent Python string literals, so this cannot be line-scoped.
INSERT_WINDOW = 3000
CLAIM_LOOKBACK = 3000
SELECT_LOOKBACK = 400

WRITE_OPS = frozenset({"insert", "insert_ignore", "upsert", "update", "delete", "truncate"})
READ_OPS = frozenset({"select", "join"})
DDL_OPS = frozenset({"create_table", "alter_table", "drop_table", "create_index"})
# DDL is evidence, never a reason to call a table live. See module docstring.
DML_OPS = WRITE_OPS | READ_OPS


def _line_of(text: str, pos: int) -> int:
    return text.count("\n", 0, pos) + 1


def _is_comment_line(text: str, pos: int) -> bool:
    """Cheap prose filter: a `#`- or `//`-led line is documentation, not SQL."""
    start = text.rfind("\n", 0, pos) + 1
    lead = text[start:pos].lstrip()
    return lead.startswith("#") or lead.startswith("//") or lead.startswith("*")


def scan_text(text: str, known: set[str]) -> list[tuple[str, str, int]]:
    """Return (table, op, line) for every SQL reference to a known table.

    Only captures whose identifier is a known table are kept. That filter, not
    the regexes, is what stops English prose from being read as SQL.
    """
    refs: list[tuple[str, str, int]] = []
    delete_spans: list[tuple[int, int]] = []

    def emit(match: re.Match, op: str) -> None:
        name = match.group(1)
        if name not in known:
            return
        if _is_comment_line(text, match.start()):
            return
        refs.append((name, op, _line_of(text, match.start())))

    for m in RE_DELETE.finditer(text):
        delete_spans.append((m.start(), m.end()))
        emit(m, "delete")

    for m in RE_INSERT.finditer(text):
        name = m.group(1)
        if name not in known or _is_comment_line(text, m.start()):
            continue
        # The ON CONFLICT clause belongs to THIS insert only if no other insert
        # starts before it.
        window = text[m.end() : m.end() + INSERT_WINDOW]
        nxt = RE_INSERT.search(window)
        if nxt:
            window = window[: nxt.start()]
        conflict = RE_ON_CONFLICT.search(window)
        if conflict and RE_DO_UPDATE.search(window, conflict.end()):
            op = "upsert"
        elif conflict:
            op = "insert_ignore"  # ON CONFLICT DO NOTHING is still append-only
        else:
            op = "insert"
        refs.append((name, op, _line_of(text, m.start())))

    for m in RE_UPDATE.finditer(text):
        emit(m, "update")
    for m in RE_TRUNCATE.finditer(text):
        emit(m, "truncate")

    for m in RE_FROM.finditer(text):
        if any(s <= m.start() < e for s, e in delete_spans):
            continue  # already counted as the DELETE's target
        # Require a SELECT nearby, so `from positions import ...` and prose
        # like "read from analysis_results" are not read as queries.
        back = text[max(0, m.start() - SELECT_LOOKBACK) : m.start()]
        if not RE_SELECT_BEHIND.search(back):
            continue
        emit(m, "select")

    for m in RE_JOIN.finditer(text):
        emit(m, "join")

    for m in RE_CREATE_TABLE.finditer(text):
        emit(m, "create_table")
    for m in RE_ALTER_TABLE.finditer(text):
        emit(m, "alter_table")
    for m in RE_DROP_TABLE.finditer(text):
        emit(m, "drop_table")
    for m in RE_CREATE_INDEX.finditer(text):
        emit(m, "create_index")

    # A claim is a SELECT ... FOR UPDATE SKIP LOCKED. Attribute it to the
    # nearest table named behind it.
    for m in RE_SKIP_LOCKED.finditer(text):
        if _is_comment_line(text, m.start()):
            continue
        back = text[max(0, m.start() - CLAIM_LOOKBACK) : m.start()]
        best: tuple[int, str] | None = None
        for rx in (RE_FROM, RE_UPDATE, RE_JOIN):
            for hit in rx.finditer(back):
                if hit.group(1) in known and (best is None or hit.start() > best[0]):
                    best = (hit.start(), hit.group(1))
        if best:
            refs.append((best[1], "skip_locked", _line_of(text, m.start())))

    return refs


def _walk(root: Path):
    if root.is_file():
        if root.suffix in SCAN_SUFFIXES:
            yield root
        return
    if not root.is_dir():
        return
    for path in root.rglob("*"):
        if path.suffix not in SCAN_SUFFIXES or not path.is_file():
            continue
        if SKIP_DIR_PARTS & set(path.parts):
            continue
        yield path


def scan_roots(known: set[str]) -> tuple[dict[str, list], dict[str, dict[str, int]]]:
    """Scan every configured root; return refs bucketed by role, plus signal counts.

    Buckets follow the migration's actual question -- who writes, who reads,
    and from which side of the service/client split -- because that is what
    decides who has to be cut over together.
    """
    # (bucket-role, label, path). `role` picks the destination list; SQL op
    # then splits writers from readers.
    roots: list[tuple[str, Path]] = []
    for rel in ("app", "cycle_main.py", ".claude/hooks"):
        roots.append(("service", SERVICE_REPO / rel))
    for rel in ("scripts", "tools"):
        roots.append(("script", SERVICE_REPO / rel))
    for rel in ("app", "frontend/src"):
        roots.append(("client", CLIENT_REPO / rel))
    for rel in ("scripts", "tools"):
        roots.append(("script", CLIENT_REPO / rel))

    # Every other sun repo: a foreign reader of a trading table is exactly the
    # kind of coupling a migration breaks silently.
    for child in sorted(SUN_ROOT.iterdir()):
        if not child.is_dir() or child.name in {"trading-service", "trading-client"}:
            continue
        if child.name.startswith(".") or child.name == "node_modules":
            continue
        roots.append(("external", child))

    by_table: dict[str, list[tuple[str, str, str]]] = defaultdict(list)  # table -> (role, ref, op)
    signals: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))

    for role, root in roots:
        for path in _walk(root):
            try:
                text = path.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            if not text:
                continue
            for table, op, line in scan_text(text, known):
                try:
                    rel = path.relative_to(SUN_ROOT).as_posix()
                except ValueError:
                    rel = path.as_posix()
                by_table[table].append((role, f"{rel}:{line}:{op}", op))
                signals[table][op] += 1

    return by_table, {t: dict(s) for t, s in signals.items()}


def bucket_refs(entries: list[tuple[str, str, str]]) -> dict[str, list[str]]:
    """Split one table's refs into the six ledger lists."""
    out: dict[str, list[str]] = {
        "service_writers": [],
        "service_readers": [],
        "client_writers": [],
        "client_readers": [],
        "script_refs": [],
        "external_refs": [],
    }
    for role, ref, op in entries:
        if role == "script":
            out["script_refs"].append(ref)
        elif role == "external":
            out["external_refs"].append(ref)
        elif role == "service":
            out["service_writers" if op in WRITE_OPS else "service_readers"].append(ref)
        elif role == "client":
            out["client_writers" if op in WRITE_OPS else "client_readers"].append(ref)
    for key in out:
        out[key] = sorted(set(out[key]))
    return out


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------


def classify(table: str, signals: dict[str, int]) -> str:
    """Resolve one shape from the signal mix.

    Precedence is deliberate and, where signals conflict, the HARDER migration
    wins: a table carrying both an upsert and a bare UPDATE is `mutable`,
    because the bare UPDATE is the case that needs the extra work. The raw
    signal counts travel with the record so this call is always inspectable.
    """
    if table in TIMESERIES_TABLES:
        return "timeseries"
    if table in MONEY_TABLES:
        return "money"
    if table in QUEUE_TABLES or signals.get("skip_locked"):
        return "queue"
    if not any(signals.get(op) for op in DML_OPS):
        return "unreferenced"
    if any(signals.get(op) for op in ("update", "delete", "truncate")):
        return "mutable"
    if signals.get("upsert"):
        return "upsert"
    if signals.get("insert") or signals.get("insert_ignore"):
        return "append"
    return "reference"


RE_UNIQUE_INDEX = re.compile(
    r"CREATE\s+UNIQUE\s+INDEX\s+(\S+)\s+ON\s+\S+\s+USING\s+\w+\s+\((.+)\)\s*$",
    re.IGNORECASE,
)
# A trailing WHERE makes the index PARTIAL. `(.+)` above is greedy and would
# otherwise swallow `slug) WHERE (active = true` as a column name, which reads
# as a single-column key and is not one.
RE_PARTIAL_INDEX = re.compile(r"\)\s+WHERE\b", re.IGNORECASE)


def key_fields(
    constraints: list[dict], indexes: list[str] | None = None
) -> tuple[str | None, str | None, list[str]]:
    """Return (primary key, single-column unique, all unique definitions).

    A single-column UNIQUE is often the real document identity -- the PK is
    frequently a surrogate `id` that Mongo has no use for.

    Unique constraints and unique INDEXES are both read. In this schema 16
    uniques are constraints and 19 are non-pkey unique indexes, so reading
    `constraints` alone would miss three real document identities. The index
    backing a primary key is skipped: it restates the PK and is not a second
    key. Partial unique indexes (those carrying a WHERE clause) are skipped
    too -- a key that only holds for some rows is not a document identity.
    """
    pk = None
    uniques: list[str] = []
    single_cols: list[str] = []

    for con in constraints or []:
        definition = (con.get("definition") or "").strip()
        if con.get("type") == "p" and definition.upper().startswith("PRIMARY KEY"):
            inner = definition[definition.find("(") + 1 : definition.rfind(")")]
            pk = ", ".join(c.strip().strip('"') for c in inner.split(","))
        elif con.get("type") == "u" and definition.upper().startswith("UNIQUE"):
            uniques.append(definition)
            inner = definition[definition.find("(") + 1 : definition.rfind(")")]
            cols = [c.strip().strip('"') for c in inner.split(",")]
            if len(cols) == 1:
                single_cols.append(cols[0])

    for index in indexes or []:
        index = index.strip()
        if RE_PARTIAL_INDEX.search(index):
            continue
        match = RE_UNIQUE_INDEX.match(index)
        if not match or match.group(1).endswith("_pkey"):
            continue
        cols = [c.strip().strip('"') for c in match.group(2).split(",")]
        if any("(" in c for c in cols):
            continue  # an expression index, not a plain column identity
        definition = "UNIQUE (" + ", ".join(cols) + ")"
        if definition not in uniques:
            uniques.append(definition)
        if len(cols) == 1 and cols[0] not in single_cols:
            single_cols.append(cols[0])

    return pk, (single_cols[0] if single_cols else None), uniques


def parse_mongo_modes(deploy_sh: Path) -> dict[str, str]:
    """Read MONGO_STORE_DEFAULT out of deploy.sh -- the live per-table backend."""
    if not deploy_sh.exists():
        return {}
    modes: dict[str, str] = {}
    for line in deploy_sh.read_text(encoding="utf-8", errors="ignore").splitlines():
        stripped = line.strip()
        if not stripped.startswith("MONGO_STORE_DEFAULT="):
            continue
        value = stripped.split("=", 1)[1].strip().strip('"').strip("'")
        for pair in value.split(","):
            if ":" in pair:
                table, mode = pair.split(":", 1)
                modes[table.strip()] = mode.strip()
        break
    return modes


# ---------------------------------------------------------------------------
# Live database (read-only)
# ---------------------------------------------------------------------------


def read_live(dsn: str) -> tuple[list[str], dict[str, int], str | None]:
    """Read table names and row counts. Read-only: SELECT and count(*) only."""
    try:
        import psycopg2
    except ImportError:
        return [], {}, "psycopg2 is not importable"
    try:
        conn = psycopg2.connect(dsn, connect_timeout=15)
    except Exception as exc:  # noqa: BLE001 - any failure means "no live data"
        return [], {}, f"{type(exc).__name__}: {exc}".strip()
    try:
        conn.set_session(readonly=True, autocommit=True)
        cur = conn.cursor()
        cur.execute("SELECT tablename FROM pg_tables WHERE schemaname='public' ORDER BY 1")
        live = [r[0] for r in cur.fetchall()]
        counts: dict[str, int] = {}
        for table in live:
            try:
                cur.execute(f'SELECT count(*) FROM public."{table}"')
                counts[table] = int(cur.fetchone()[0])
            except Exception:  # noqa: BLE001 - one unreadable table is not fatal
                conn.rollback()
        return live, counts, None
    finally:
        conn.close()


def treesearch_tables(orm_path: Path) -> list[str]:
    """Every `__tablename__` in treesearch's ORM -- the positive treesearch list."""
    if not orm_path.exists():
        return []
    text = orm_path.read_text(encoding="utf-8", errors="ignore")
    return sorted(set(re.findall(r"__tablename__\s*=\s*[\"']([A-Za-z0-9_]+)[\"']", text)))


# ---------------------------------------------------------------------------
# Build
# ---------------------------------------------------------------------------


def cap(items: list[str], limit: int = 20) -> list[str]:
    return items[:limit]


def build(use_db: bool = True, ref_cap: int = 20) -> dict:
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    manifest_tables = list(manifest["tables"])
    constraints = manifest.get("constraints", {})

    live_tables, row_counts, db_error = ([], {}, "skipped (--no-db)")
    if use_db:
        live_tables, row_counts, db_error = read_live(DSN)

    ts_tables = treesearch_tables(TREESEARCH_ORM)
    known = set(manifest_tables) | set(live_tables) | set(ts_tables)

    refs_by_table, signals_by_table = scan_roots(known)
    modes = parse_mongo_modes(REPO_ROOT / "deploy.sh")

    records = []
    for table in sorted(manifest_tables):
        signals = signals_by_table.get(table, {})
        shape = classify(table, signals)
        buckets = bucket_refs(refs_by_table.get(table, []))
        pk, single_unique, uniques = key_fields(
            constraints.get(table, []), manifest.get("indexes", {}).get(table, [])
        )
        record = {
            "table": table,
            "shape": shape,
            "key_field": pk,
            "natural_key": single_unique,
            "unique_constraints": uniques,
            "numeric_policy": "dec128" if shape == "money" else "float",
            "mode_now": modes.get(table, "pg"),
            "wave": None,
            "signals": dict(sorted(signals.items())),
            "ref_counts": {k: len(v) for k, v in buckets.items()},
            "row_count": row_counts.get(table),
            "backfilled_at": None,
            "field_verified_at": None,
            "promoted_dual": None,
            "promoted_mongo_read": None,
            "promoted_mongo": None,
            "archived_at": None,
            "dropped_at": None,
            "archive_file": None,
            "disposition": "archive-only" if shape == "unreferenced" else "migrate",
        }
        for key, values in buckets.items():
            record[key] = cap(values, ref_cap)
        records.append(record)

    foreign = build_foreign(
        live_tables, set(manifest_tables), ts_tables, refs_by_table, row_counts, ref_cap
    )

    shape_counts: dict[str, int] = {s: 0 for s in SHAPES}
    for rec in records:
        shape_counts[rec["shape"]] += 1
    disposition_counts: dict[str, int] = defaultdict(int)
    for rec in records:
        disposition_counts[rec["disposition"]] += 1

    unclassified = [f["table"] for f in foreign if f["owner"] == "UNCLASSIFIED"]

    return {
        "ledger_format_version": LEDGER_FORMAT_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "manifest_table_count": len(manifest_tables),
        "manifest_format_version": manifest.get("manifest_format_version"),
        "external_refs_caveat": (
            "external_refs match on table NAME across sun repos, not on database. "
            "HTML-Notes/app/database.py is sqlite3 and the vendored mnemopi engine has its "
            "own store, so hits there are name collisions. Refs in "
            "postgres-service/scripts/schema_pg.sql are the ones that share this instance."
        ),
        "database_reachable": db_error is None,
        "database_note": db_error,
        "live_table_count": len(live_tables) or None,
        "shape_counts": shape_counts,
        "disposition_counts": dict(sorted(disposition_counts.items())),
        "foreign_table_count": len(foreign),
        "unclassified_foreign_count": len(unclassified),
        "unclassified_foreign_tables": unclassified,
        "tables": records,
        "foreign_tables": foreign,
    }


def build_foreign(
    live_tables: list[str],
    manifest_set: set[str],
    ts_tables: list[str],
    refs_by_table: dict[str, list],
    row_counts: dict[str, int],
    ref_cap: int,
) -> list[dict]:
    """Live public tables that are NOT trading's, each with an owner and evidence.

    Owner is assigned from POSITIVE lists only. Code that merely mentions a
    table is recorded as `inferred_owner` and never promoted to `owner` -- an
    inference is not a title deed, and `owner` is what a DROP would consult.
    """
    ts_set = set(ts_tables)
    out = []
    for table in sorted(t for t in live_tables if t not in manifest_set):
        entries = refs_by_table.get(table, [])
        evidence = sorted({ref for _role, ref, _op in entries})

        repos = sorted({ref.split("/", 1)[0] for ref in evidence})
        inferred = repos[0] if len(repos) == 1 else (repos or None)

        if table in ts_set:
            owner = "treesearch-service"
            basis = f"__tablename__ in {TREESEARCH_ORM.relative_to(SUN_ROOT).as_posix()}"
        elif re.search(r"_backup_2026\d*$", table):
            owner = "snapshot"
            basis = "name matches *_backup_2026* (a pg_dump-era snapshot table)"
        elif table.startswith("cognition_"):
            owner = "UNKNOWN"
            basis = "cognition_* prefix; no owning service established by scan"
        else:
            owner = "UNCLASSIFIED"
            basis = "matches neither the manifest nor any positive owner list"

        out.append({
            "table": table,
            "owner": owner,
            "owner_basis": basis,
            "inferred_owner": inferred if owner in {"UNKNOWN", "UNCLASSIFIED"} else None,
            "evidence": cap(evidence, ref_cap),
            "evidence_count": len(evidence),
            "row_count": row_counts.get(table),
            "disposition": "migrate" if owner == "treesearch-service" else "hold",
        })
    return out


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

REMEMBERED = {
    "append": 68,
    "upsert": 58,
    "mutable": 59,
    "reference": 10,
    "unreferenced": 46,
}


def summarize(ledger: dict) -> None:
    print(f"manifest tables      : {ledger['manifest_table_count']}")
    print(f"live tables          : {ledger['live_table_count']}")
    print(f"database reachable   : {ledger['database_reachable']}")
    if ledger["database_note"]:
        print(f"database note        : {ledger['database_note']}")

    print("\nshape histogram")
    total = 0
    for shape in SHAPES:
        count = ledger["shape_counts"].get(shape, 0)
        total += count
        old = REMEMBERED.get(shape)
        delta = ""
        if old is not None:
            delta = f"   (prior session: {old}, delta {count - old:+d})"
        print(f"  {shape:<14} {count:>4}{delta}")
    print(f"  {'TOTAL':<14} {total:>4}")
    print(
        "  note: the prior counts sum to "
        f"{sum(REMEMBERED.values())} over {ledger['manifest_table_count']} tables, so they "
        "overlapped;\n        they are printed for comparison and were NOT used to tune this "
        "classifier."
    )

    print("\ndisposition")
    for key, count in ledger["disposition_counts"].items():
        print(f"  {key:<14} {count:>4}")

    print(f"\nforeign tables       : {ledger['foreign_table_count']}")
    owners: dict[str, int] = defaultdict(int)
    for entry in ledger["foreign_tables"]:
        owners[entry["owner"]] += 1
    for owner, count in sorted(owners.items()):
        print(f"  {owner:<22} {count:>3}")

    unclassified = ledger["unclassified_foreign_tables"]
    unknown = [f for f in ledger["foreign_tables"] if f["owner"] == "UNKNOWN"]
    if unclassified or unknown:
        print("\n" + "!" * 72)
        print("!! LIVE TABLES WITH NO ESTABLISHED OWNER -- THESE BLOCK EVERY FUTURE DROP")
        print("!" * 72)
        for entry in ledger["foreign_tables"]:
            if entry["owner"] not in {"UNCLASSIFIED", "UNKNOWN"}:
                continue
            rows = entry["row_count"]
            rows = "?" if rows is None else f"{rows:,}"
            hint = entry["inferred_owner"] or "no code reference anywhere"
            print(f"  [{entry['owner']:<12}] {entry['table']:<38} rows={rows:>10}  hint: {hint}")
        print(
            f"  {len(unclassified)} UNCLASSIFIED + {len(unknown)} UNKNOWN. "
            "Name an owner before dropping any of these."
        )
    else:
        print("\nevery foreign table has an established owner.")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--no-db", action="store_true", help="skip live row counts")
    parser.add_argument("--dry-run", action="store_true", help="print the summary, write nothing")
    parser.add_argument("--ref-cap", type=int, default=20, help="max refs kept per list")
    args = parser.parse_args()

    ledger = build(use_db=not args.no_db, ref_cap=args.ref_cap)
    summarize(ledger)

    if args.dry_run:
        print("\n--dry-run: nothing written")
        return 0

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(ledger, indent=2, sort_keys=False) + "\n", encoding="utf-8")
    print(f"\nwrote {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
