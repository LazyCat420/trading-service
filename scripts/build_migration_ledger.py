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
import io
import subprocess
import tarfile
import tempfile
from collections import Counter, defaultdict
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

LEDGER_FORMAT_VERSION = "1.1"

# ---------------------------------------------------------------------------
# Tables the manifest never saw
# ---------------------------------------------------------------------------
#
# schema.sql declares 183 tables; the database holds 214. Most of that gap is
# other products sharing the instance, but a handful of tables are trading's
# own: created at runtime by a script instead of being declared, so the
# manifest never saw them and the ledger left them out of scope entirely.
# A table a live writer still fills has to be IN scope -- otherwise removing
# Postgres removes the store that writer depends on, and nothing in the
# migration ever notices.
#
# Adoption follows the same rule as ownership below: POSITIVE lists only. Each
# entry names the write site that proves the claim, and declares its key, since
# there is no manifest constraint block to read one from.
ADOPTED: dict[str, dict] = {
    "agent_registry": {
        "owner": "trading-client",
        "proof": "trading-client/app/client_agents/base_agent.py:75:upsert",
        "key_field": "agent_id",
    },
    "agent_tasks": {
        "owner": "trading-client",
        "proof": "trading-client/app/client_agents/base_agent.py:138:insert",
        "key_field": "task_id",
    },
    "autofix_runs": {
        "owner": "trading-service",
        "proof": "trading-service/scripts/autofix/run_autofix.py:272:insert",
        "key_field": "id",
    },
    "box_benchmark_runs": {
        "owner": "trading-service",
        "proof": "trading-service/scripts/jetson_benchmark.py:486:insert",
        "key_field": "id",
    },
}

# Live tables with a trading past and no writer today. They keep their rows for
# history and take `archive-only`, which is a disposition the migration already
# understands -- but they are recorded here rather than left UNCLASSIFIED, so a
# future DROP consults a decision instead of a silence.
RETIRED: dict[str, dict] = {
    "llm_traces": {
        "owner": "trading-service",
        "basis": "last write 2026-06-25, zero code references (replay-only archive)",
        "key_field": "id",
    },
    "context_telemetry": {
        "owner": "trading-service",
        "basis": "last write 2026-06-25, zero code references",
        "key_field": "id",
    },
    "fallback_data": {
        "owner": "trading-service",
        "basis": "0 rows, zero code references",
        "key_field": "id",
    },
}

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


_SNAPSHOT_TMP: tempfile.TemporaryDirectory | None = None
_SNAPSHOTS: dict[Path, Path] = {}
DIRTY_REPOS: list[str] = []
UNSNAPSHOTTABLE: list[str] = []


def repo_is_dirty(repo: Path) -> bool:
    proc = subprocess.run(
        ["git", "-C", str(repo), "status", "--porcelain"],
        capture_output=True, text=True, check=False,
    )
    return proc.returncode == 0 and bool(proc.stdout.strip())


def snapshot_head(repo: Path) -> Path:
    """Export `repo`'s committed HEAD to a temp dir, once per run.

    The ledger describes what SHIPS, and what ships is committed. This scanner
    reads every repo in sun, so before this, one unrelated session's
    uncommitted edit changed a trading table's classification: regenerating
    with a dirty trading-client flipped `llm_audit_logs` from mutable to
    append, and shape drives both disposition and collection prefix. Reading
    HEAD makes the ledger a function of committed state -- reproducible, and
    identical no matter who is mid-edit.

    Falls back to the working tree for anything that is not a git repo, and
    records that it did so rather than passing it off as HEAD.
    """
    global _SNAPSHOT_TMP
    if repo in _SNAPSHOTS:
        return _SNAPSHOTS[repo]

    if _SNAPSHOT_TMP is None:
        _SNAPSHOT_TMP = tempfile.TemporaryDirectory(prefix="ledger-head-")

    dest = Path(_SNAPSHOT_TMP.name) / repo.name
    proc = subprocess.run(
        ["git", "-C", str(repo), "archive", "--format=tar", "HEAD"],
        capture_output=True, check=False,
    )
    if proc.returncode != 0 or not proc.stdout:
        UNSNAPSHOTTABLE.append(repo.name)
        _SNAPSHOTS[repo] = repo
        return repo

    dest.mkdir(parents=True, exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(proc.stdout)) as tf:
        # Only the files this scanner would read anyway -- keeps the export
        # small enough to hold in memory for every repo in sun.
        members = [
            m for m in tf.getmembers()
            if m.isfile()
            and Path(m.name).suffix in SCAN_SUFFIXES
            and not (SKIP_DIR_PARTS & set(Path(m.name).parts))
        ]
        tf.extractall(dest, members=members, filter="data")

    if repo_is_dirty(repo):
        DIRTY_REPOS.append(repo.name)
    _SNAPSHOTS[repo] = dest
    return dest


def scan_roots(known: set[str]) -> tuple[dict[str, list], dict[str, dict[str, int]]]:
    """Scan every configured root; return refs bucketed by role, plus signal counts.

    Buckets follow the migration's actual question -- who writes, who reads,
    and from which side of the service/client split -- because that is what
    decides who has to be cut over together.

    Every root is read at committed HEAD, never from the working tree -- see
    snapshot_head().
    """
    # (bucket-role, scan path, label prefix). `role` picks the destination
    # list; SQL op then splits writers from readers. The scan path points into
    # a HEAD export; the label keeps evidence readable as `<repo>/<path>`.
    roots: list[tuple[str, Path, str]] = []

    def add(role: str, repo: Path, rel: str = "") -> None:
        base = snapshot_head(repo)
        roots.append((role, base / rel if rel else base,
                      f"{repo.name}/{rel}" if rel else repo.name))

    for rel in ("app", "cycle_main.py", ".claude/hooks"):
        add("service", SERVICE_REPO, rel)
    for rel in ("scripts", "tools"):
        add("script", SERVICE_REPO, rel)
    for rel in ("app", "frontend/src"):
        add("client", CLIENT_REPO, rel)
    for rel in ("scripts", "tools"):
        add("script", CLIENT_REPO, rel)

    # Every other sun repo: a foreign reader of a trading table is exactly the
    # kind of coupling a migration breaks silently.
    for child in sorted(SUN_ROOT.iterdir()):
        if not child.is_dir() or child.name in {"trading-service", "trading-client"}:
            continue
        if child.name.startswith(".") or child.name == "node_modules":
            continue
        add("external", child)

    by_table: dict[str, list[tuple[str, str, str]]] = defaultdict(list)  # table -> (role, ref, op)
    signals: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))

    for role, root, label in roots:
        for path in _walk(root):
            try:
                text = path.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            if not text:
                continue
            for table, op, line in scan_text(text, known):
                # Label against the logical repo path, not the temp export, so
                # evidence stays clickable.
                rel = label if path == root else f"{label}/{path.relative_to(root).as_posix()}"
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


VALID_MODES = frozenset({"pg", "dual", "mongo_read", "mongo"})


def parse_mongo_modes(backends_env: Path) -> dict[str, str]:
    """Read MONGO_STORE_BACKEND out of app/db/mongo_backends.env.

    This used to read MONGO_STORE_DEFAULT out of deploy.sh. Phase 0.1 (99da42f)
    moved the canonical map into a committed app/db/mongo_backends.env and
    deleted that variable, so the grep matched nothing and this returned {} --
    which is indistinguishable from "every table is pg". `mode_now` is the
    ledger's only record of which tables have been promoted, so a regeneration
    silently erased all 13 flagged tables. It is the state machine; losing it
    loses the migration's progress.

    So this now fails CLOSED. A ledger with no mode data is worse than no
    ledger, because it reads as an authoritative "nothing has been migrated".
    """
    if not backends_env.exists():
        raise SystemExit(
            f"{backends_env} is missing -- it is the canonical per-table backend "
            "map. Refusing to build a ledger that would record every table as "
            "`pg` and erase the record of which tables are already promoted."
        )
    for line in backends_env.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped.startswith("MONGO_STORE_BACKEND="):
            continue
        value = stripped.split("=", 1)[1].strip().strip('"').strip("'")
        modes: dict[str, str] = {}
        for pair in value.split(","):
            if ":" not in pair:
                continue
            table, mode = pair.split(":", 1)
            table, mode = table.strip(), mode.strip()
            if mode not in VALID_MODES:
                raise SystemExit(
                    f"{backends_env}: table {table!r} has unknown mode {mode!r} "
                    f"(expected one of {sorted(VALID_MODES)})"
                )
            modes[table] = mode
        if not modes:
            raise SystemExit(
                f"{backends_env}: MONGO_STORE_BACKEND= parsed to zero tables."
            )
        return modes
    raise SystemExit(f"{backends_env} has no MONGO_STORE_BACKEND= line.")


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
    modes = parse_mongo_modes(REPO_ROOT / "app" / "db" / "mongo_backends.env")

    # Scope is the manifest plus the tables it never saw (ADOPTED/RETIRED), but
    # only those actually present in the database. With --no-db there is no
    # liveness evidence, so nothing is judged absent on a guess.
    db_ok = db_error is None
    live_set = set(live_tables)
    adopted_scope = [t for t in (ADOPTED | RETIRED) if not db_ok or t in live_set]

    records = []
    for table in sorted(set(manifest_tables) | set(adopted_scope)):
        adopted = ADOPTED.get(table)
        retired = RETIRED.get(table)
        signals = signals_by_table.get(table, {})
        shape = classify(table, signals)
        buckets = bucket_refs(refs_by_table.get(table, []))
        if adopted or retired:
            # No manifest constraint block exists for these; the key is declared
            # in the adoption entry and checked against the live PK by
            # tests/unit/test_adopted_tables.py.
            pk, single_unique, uniques = (adopted or retired)["key_field"], None, []
        else:
            pk, single_unique, uniques = key_fields(
                constraints.get(table, []), manifest.get("indexes", {}).get(table, [])
            )

        if retired:
            # Rows worth keeping, no writer left. A decision, not a silence.
            disposition = "archive-only"
        elif db_ok and table not in live_set:
            # Declared in schema.sql, absent from the database. Migrating it is
            # impossible and counting it as scope overstates the work left.
            disposition = "absent"
        elif shape == "unreferenced":
            disposition = "archive-only"
        else:
            disposition = "migrate"

        record = {
            "table": table,
            "shape": shape,
            "scope_basis": "adopted" if adopted else "retired" if retired else "manifest",
            "scope_evidence": (adopted or {}).get("proof") or (retired or {}).get("basis"),
            "owner": (adopted or retired or {}).get("owner"),
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
            "disposition": disposition,
        }
        for key, values in buckets.items():
            record[key] = cap(values, ref_cap)
        records.append(record)

    # Anything now carried in `tables` is no longer foreign to this migration.
    foreign = build_foreign(
        live_tables,
        set(manifest_tables) | set(ADOPTED) | set(RETIRED),
        ts_tables,
        refs_by_table,
        row_counts,
        ref_cap,
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
        # `tables` is no longer the manifest: it is the manifest plus the live
        # trading tables schema.sql never declared, minus anything the manifest
        # declares that the database does not actually have.
        "scope_table_count": len(records),
        "scope_basis_counts": dict(sorted(Counter(r["scope_basis"] for r in records).items())),
        "manifest_format_version": manifest.get("manifest_format_version"),
        "external_refs_caveat": (
            "external_refs match on table NAME across sun repos, not on database. "
            "HTML-Notes/app/database.py is sqlite3 and the vendored mnemopi engine has its "
            "own store, so hits there are name collisions. Refs in "
            "postgres-service/scripts/schema_pg.sql are the ones that share this instance."
        ),
        # Provenance: code refs come from committed HEAD, so a parallel
        # session's work-in-progress cannot move a table's shape. Repos that
        # were dirty at scan time are named -- their uncommitted work is NOT
        # represented here, which is the point.
        "scanned_at": "HEAD",
        "dirty_repos_ignored": sorted(set(DIRTY_REPOS)),
        "working_tree_fallback_repos": sorted(set(UNSNAPSHOTTABLE)),
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
