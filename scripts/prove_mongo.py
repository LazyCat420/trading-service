#!/usr/bin/env python3
"""The per-table evidence bundle that gates a Postgres→Mongo promotion.

Every promotion so far has rested on evidence gathered by hand: a grep here, a
`--verify-fields 500` there, a count comparison pasted into a handoff. That is
not reproducible, and worse, three of those instruments have already been caught
reporting a clean that was not clean:

  * a SAMPLED parity check scored `context_blobs` OK three runs out of four
    while an exhaustive sweep of the same unchanged data found 117 drifted
    `created_at` values and 2 permanently missing documents;
  * six Mongo mirror-failure sites logged at DEBUG under an INFO root logger,
    so a 48-hour soak grep returned zero hits — not because nothing failed, but
    because nothing could print (fixed in ``cd44c0b``);
  * the flag map lived in a gitignored, repo-shared `.env.deploy` that stood at
    30 tables at `mongo` while the containers ran 13.

Each of those is the same defect class: a check that cannot fail is read as a
check that passed. So this tool emits, for ONE table, every fact a promotion
needs, and it reports INSUFFICIENT EVIDENCE — never PASS — for anything it could
not actually measure. A missing input must never read as a pass.

What it emits (see ``CHECKS`` below):

  flags       the declared mode in app/db/mongo_backends.env vs the ledger's
              `mode_now`, plus any ambient MONGO_STORE_BACKEND that disagrees
              with the committed map.
  guard       what app/db/pg_write_guard.py ACTUALLY does to a write and to a
              read of this table at this mode. The verdicts come from calling
              `check_pg_write` / `check_pg_read`, never from re-deriving the
              rule here — a second copy of the rule can drift from the guard and
              would then certify the wrong thing.
  counts      Postgres rows, Mongo documents, and the collection `collection_for`
              resolves the table to (the rename map is still inert; when it is
              activated, this is the line that proves which collection was read).
  provenance  the millisecond-alignment oracle, with its false-positive rate
              MEASURED on this table rather than assumed. See `check_provenance`.
  parity      the EXHAUSTIVE `pg_to_mongo_backfill.py <table> --verify-all`
              verdict. A sampled verify is never run here and its output is
              never printed as a parity statement.
  logs        mirror-failure evidence WITH ITS POSITIVE CONTROL: the count of
              mirror-failure lines is meaningless without the total WARN/ERROR
              volume of the same window, which is what proves the stream was
              alive. A zero from a dead feed is labelled as such.

STRICTLY READ-ONLY, on both stores, by construction:
  * the Postgres session is opened and then pinned with
    `SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY`, so the server itself
    rejects a write from this process;
  * Mongo is only ever `count_documents` / `find` / `aggregate`. Nothing here
    calls `mongo_store.ensure_indexes()` — that one creates indexes, which is a
    write;
  * it deliberately does NOT go through `app.db.connection`. The pooled cursor
    runs the write guard on every statement, and for a table at `mongo` with
    MONGO_GUARD_BLOCK_READS=1 that guard would (correctly) refuse this tool's own
    `SELECT count(*)`. Auditing a frozen table must not require disarming the
    thing being audited, so the counts use their own connection and the guard is
    interrogated separately.

Usage:
    python scripts/prove_mongo.py --table embeddings
    python scripts/prove_mongo.py --table trade_results --json signoff.json
    python scripts/prove_mongo.py --all --json documentation/signoff/
    python scripts/prove_mongo.py --table pipeline_events --skip-parity   # fast

Exit codes (house convention, see scripts/check_backend_map.py):
    0  PASS         — every check ran and every check agreed
    1  FAIL         — a check ran and contradicted the promotion
    2  INSUFFICIENT — a check could not run; there is no verdict to give
    3  the tool could not start (no .env, unknown table, import failure)
"""

from __future__ import annotations

import argparse
import ast
import json
import logging
import os
import re
import shlex
import subprocess
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

SCHEMA_VERSION = 1

PASS = "PASS"
FAIL = "FAIL"
INSUFFICIENT = "INSUFFICIENT"

EXIT_BY_STATUS = {PASS: 0, FAIL: 1, INSUFFICIENT: 2}
EXIT_CANNOT_START = 3

CHECKS = ("flags", "guard", "counts", "provenance", "parity", "logs")

# Timestamp columns worth sampling, best first. A table's "when did this row
# happen" column is the one whose precision carries the provenance signal.
_TS_PREFERENCE = (
    "created_at", "timestamp", "called_at", "resolved_at", "updated_at",
    "started_at", "finished_at", "ts",
)

# Log lines that mean "the mirror dropped something" — the same three strings
# the promotion soaks have always grepped for, plus the two that the guard and
# the store emit by name.
_MIRROR_PATTERNS = (
    r"mirror failed",
    r"mirror write failed",
    r"PG fallback",
    r"\[PG GUARD\]",
    r"dual-write failed",
    r"mongo read failed",
)
_MIRROR_RE = re.compile("|".join(_MIRROR_PATTERNS), re.IGNORECASE)

# cycle_main.py formats every line as
#   %(asctime)s [%(name)s] %(levelname)s %(message)s
# so the level is a bare word. Matching the word anywhere on the line also
# catches uvicorn/gunicorn formats, which is what we want: this is a liveness
# measurement of the stream, not a parse of it.
_WARNERR_RE = re.compile(r"\b(WARNING|WARN|ERROR|CRITICAL|EXCEPTION|Traceback)\b")

# Words that mark a log call as migration evidence, and the vendor-API
# "fallback" sites that are genuinely routine. Kept in step with
# tests/unit/test_mirror_failures_are_visible.py, which enforces the same rule
# at build time; this tool reports the same population at promotion time and
# adds the levels, because "how many evidence sites exist and how many of them
# can actually print" is the positive control for check `logs`.
_EVIDENCE_WORDS = ("mirror", "pg fallback", "pg guard", "dual-write", "dual write")
_ALLOWED_DEBUG_SUBSTRINGS = ("fmp_api_key", "polygon", "duckduckgo", "skipping")


# ── result plumbing ────────────────────────────────────────────────────────
@dataclass
class Check:
    """One measurement. `status` is the only thing the verdict reads."""

    name: str
    status: str
    headline: str
    lines: list[str] = field(default_factory=list)
    data: dict = field(default_factory=dict)


def _insufficient(name: str, why: str, **data) -> Check:
    return Check(name=name, status=INSUFFICIENT, headline=f"could not run: {why}",
                 data=data)


def worst(statuses) -> str:
    """FAIL beats INSUFFICIENT beats PASS.

    A contradicted check is more informative than an unrun one, so it wins the
    headline; but any unrun check is still enough to deny a PASS.
    """
    statuses = list(statuses)
    if FAIL in statuses:
        return FAIL
    if INSUFFICIENT in statuses or not statuses:
        return INSUFFICIENT
    return PASS


# ── environment ────────────────────────────────────────────────────────────
def find_env_file(explicit: str | None) -> Path | None:
    """The .env holding DATABASE_URL / PRISM_MONGO_URI.

    `.env` is gitignored, so a git WORKTREE does not have one — and phase-0 work
    happens in worktrees by house rule. Falling back to the main worktree's copy
    is what lets this tool run from a worktree at all; a bare `load_dotenv()`
    finds nothing there and every check would report "database unreachable",
    which is the failure mode this tool exists to not produce.
    """
    if explicit:
        p = Path(explicit).expanduser().resolve()
        return p if p.exists() else None
    local = REPO_ROOT / ".env"
    if local.exists():
        return local
    try:
        common = subprocess.run(
            ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
            cwd=REPO_ROOT, capture_output=True, text=True, timeout=10,
        )
        if common.returncode == 0:
            main_root = Path(common.stdout.strip()).parent / ".env"
            if main_root.exists():
                return main_root
    except Exception:  # noqa: BLE001 - a missing git is not fatal here
        pass
    return None


def load_environment(env_path: Path | None) -> dict:
    """Load .env, then force the COMMITTED backend map into the environment.

    Order matters. `mongo_store` parses MONGO_STORE_BACKEND once at import, and
    the guard's table map is built from that. If an ambient value (a sourced
    `.env.deploy`, an operator's shell) reached the import first, every guard
    verdict below would describe a flag state the containers do not run — which
    is exactly the drift that put 30 tables at `mongo` in the shared deploy file
    while the containers ran 13. The committed map wins here, and the ambient
    value is reported as a finding rather than silently used.
    """
    info: dict = {"env_file": str(env_path) if env_path else None}
    if env_path:
        try:
            from dotenv import load_dotenv
            load_dotenv(str(env_path), override=True)
        except ImportError:
            info["dotenv_error"] = "python-dotenv is not installed"
    info["ambient_backend_map"] = os.environ.get("MONGO_STORE_BACKEND")
    info["database_url_present"] = bool(os.environ.get("DATABASE_URL"))
    info["mongo_uri_present"] = bool(os.environ.get("PRISM_MONGO_URI"))
    info["read_guard_env"] = os.environ.get("MONGO_GUARD_BLOCK_READS", "")
    info["allow_pg_env"] = os.environ.get("MONGO_GUARD_ALLOW_PG", "")
    return info


def committed_backend_map() -> dict[str, str]:
    """The flag map both containers run, parsed from the committed file.

    Reuses `check_backend_map.parse_map` rather than re-implementing the split:
    two parsers of one file is how the two files drifted in the first place.
    """
    sys.path.insert(0, str(REPO_ROOT / "scripts"))
    from check_backend_map import parse_map  # noqa: E402

    return parse_map(REPO_ROOT / "app" / "db" / "mongo_backends.env")


def ledger_rows() -> dict[str, dict]:
    path = REPO_ROOT / "app" / "db" / "migration_ledger.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    return {r["table"]: r for r in data["tables"]}


def git_provenance() -> dict:
    """Which commit produced this bundle. A signoff artifact that cannot be
    traced to a tree is a screenshot."""
    out: dict = {}
    for key, cmd in (("commit", ["git", "rev-parse", "HEAD"]),
                     ("branch", ["git", "rev-parse", "--abbrev-ref", "HEAD"])):
        try:
            r = subprocess.run(cmd, cwd=REPO_ROOT, capture_output=True,
                               text=True, timeout=10)
            out[key] = r.stdout.strip() if r.returncode == 0 else None
        except Exception:  # noqa: BLE001
            out[key] = None
    try:
        r = subprocess.run(["git", "status", "--porcelain"], cwd=REPO_ROOT,
                           capture_output=True, text=True, timeout=15)
        out["dirty"] = bool(r.stdout.strip()) if r.returncode == 0 else None
    except Exception:  # noqa: BLE001
        out["dirty"] = None
    return out


# ── Postgres, read-only ────────────────────────────────────────────────────
class ReadOnlyPG:
    """A psycopg connection the server itself will not let us write through.

    Not `app.db.connection`: that pool runs the write guard on every statement,
    and at mode `mongo` with the read guard armed it would refuse this tool's own
    count. The audit must not require disarming the thing it is auditing.
    """

    def __init__(self, dsn: str, timeout: int = 15):
        import psycopg

        self.conn = psycopg.connect(dsn, connect_timeout=timeout, autocommit=True)
        self.conn.execute("SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY")

    def close(self) -> None:
        try:
            self.conn.close()
        except Exception:  # noqa: BLE001
            pass

    def one(self, sql: str, params=None):
        return self.conn.execute(sql, params or []).fetchone()

    def all(self, sql: str, params=None):
        return self.conn.execute(sql, params or []).fetchall()

    def table_exists(self, table: str) -> bool:
        row = self.one(
            "SELECT 1 FROM information_schema.tables "
            "WHERE table_schema NOT IN ('pg_catalog','information_schema') "
            "AND table_name = %s LIMIT 1", [table])
        return bool(row)

    def timestamp_columns(self, table: str) -> list[str]:
        rows = self.all(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = %s AND data_type LIKE 'timestamp%%' "
            "ORDER BY ordinal_position", [table])
        return [r[0] for r in rows]


# ── check 1: the declared mode ─────────────────────────────────────────────
def check_flags(table: str, env_info: dict) -> Check:
    """Does the committed map agree with the ledger about where this table is?

    A disagreement is not cosmetic: if the ledger says `mongo` and the map says
    `pg`, the migration reports the table finished while Postgres still serves
    it, and the promotion evidence below describes a mode nobody is running.
    """
    try:
        flags = committed_backend_map()
        rows = ledger_rows()
    except Exception as exc:  # noqa: BLE001
        return _insufficient("flags", f"{type(exc).__name__}: {exc}")

    declared = flags.get(table)  # absent from the map == pg, by mongo_store's default
    row = rows.get(table)
    ledger_mode = row.get("mode_now") if row else None
    effective = declared or "pg"

    lines = [
        f"mongo_backends.env : {declared!r}" + ("" if declared else "  (absent → 'pg')"),
        f"migration_ledger   : {ledger_mode!r}",
    ]
    data = {
        "declared_mode": declared,
        "effective_mode": effective,
        "ledger_mode_now": ledger_mode,
        "in_ledger": row is not None,
        "ledger_row_count": (row or {}).get("row_count"),
        "ledger_shape": (row or {}).get("shape"),
        "ledger_key_field": (row or {}).get("key_field"),
        "ledger_natural_key": (row or {}).get("natural_key"),
        "ambient_backend_map_differs": False,
    }

    if row is None:
        return Check("flags", FAIL,
                     f"{table!r} is not in migration_ledger.json at all", lines, data)

    ambient = env_info.get("ambient_backend_map")
    if ambient:
        ambient_map = {}
        for pair in ambient.split(","):
            if ":" in pair:
                k, v = pair.split(":", 1)
                ambient_map[k.strip()] = v.strip()
        if ambient_map.get(table, "pg") != effective:
            data["ambient_backend_map_differs"] = True
            data["ambient_mode"] = ambient_map.get(table, "pg")
            lines.append(
                f"WARNING: the ambient MONGO_STORE_BACKEND says "
                f"{ambient_map.get(table, 'pg')!r} for this table. The committed map "
                "is used here (deploy.sh drops the ambient value unless "
                "MONGO_STORE_ALLOW_ENV_OVERRIDE=1), but something in this shell "
                "disagrees with what the containers run."
            )

    if declared is not None and ledger_mode != declared:
        return Check("flags", FAIL,
                     f"map says {declared!r}, ledger says {ledger_mode!r}", lines, data)
    if declared is None and ledger_mode != "pg":
        return Check("flags", FAIL,
                     f"ledger claims {ledger_mode!r} but the table is absent from the "
                     "map, so the containers run it at 'pg'", lines, data)
    return Check("flags", PASS, f"map and ledger agree: {effective}", lines, data)


# ── check 2: what the guard actually does ──────────────────────────────────
class _Capture(logging.Handler):
    def __init__(self):
        super().__init__(level=logging.DEBUG)
        self.records: list[tuple[str, str]] = []

    def emit(self, record):
        self.records.append((record.levelname, record.getMessage()))


def _probe(fn, sql: str, capture: _Capture) -> dict:
    """Run one guard call and record what it did — raise, log, or nothing."""
    before = len(capture.records)
    try:
        fn(sql)
        raised = None
    except Exception as exc:  # noqa: BLE001 - the raise IS the measurement
        raised = f"{type(exc).__name__}: {exc}"
    return {
        "sql": sql,
        "raised": raised,
        "logged": [f"{lvl}: {msg}" for lvl, msg in capture.records[before:]],
    }


def check_guard(table: str, mode: str) -> Check:
    """Ask the guard, do not re-derive it.

    The rule ("mongo raises, mongo_read logs, dual is unguarded, reads only when
    MONGO_GUARD_BLOCK_READS=1") is written down in pg_write_guard.py and in its
    tests. A copy of it here could drift from the guard and would then certify a
    protection that is not running. So every verdict below is the observed
    behaviour of `check_pg_write` / `check_pg_read` on a probe statement — the
    probes are strings, they never reach a database.
    """
    try:
        from app.db import pg_write_guard as guard
    except Exception as exc:  # noqa: BLE001
        return _insufficient("guard", f"cannot import the guard: "
                                      f"{type(exc).__name__}: {exc}")

    guard.reset_cache()
    capture = _Capture()
    guard.logger.addHandler(capture)
    previously_armed = guard._read_guard_enabled()
    saved = os.environ.get("MONGO_GUARD_BLOCK_READS")
    try:
        write_insert = _probe(guard.check_pg_write,
                              f"INSERT INTO {table} (id) VALUES (1)", capture)
        write_delete = _probe(guard.check_pg_write,
                              f"DELETE FROM {table} WHERE id = 1", capture)
        read_ambient = _probe(guard.check_pg_read, f"SELECT 1 FROM {table}", capture)
        # The counterfactual: the read guard is opt-in and usually off, so
        # "nothing raised" is not evidence that it WOULD refuse a stale read.
        # Force it on and measure that separately.
        os.environ["MONGO_GUARD_BLOCK_READS"] = "1"
        read_armed = _probe(guard.check_pg_read, f"SELECT 1 FROM {table}", capture)
    finally:
        if saved is None:
            os.environ.pop("MONGO_GUARD_BLOCK_READS", None)
        else:
            os.environ["MONGO_GUARD_BLOCK_READS"] = saved
        guard.logger.removeHandler(capture)

    guarded = guard._guarded_tables()
    data = {
        "guard_sees_mode": guarded.get(table.lower()),
        "read_guard_armed_here": previously_armed,
        "script_context": guard._is_script_context(),
        "allow_pg_override": guard._allow_override(),
        "write_insert": write_insert,
        "write_delete": write_delete,
        "read_ambient": read_ambient,
        "read_forced_armed": read_armed,
    }

    lines = [
        f"guard's own table map     : {table} → {guarded.get(table.lower())!r}",
        f"PG write (INSERT) raises  : {'YES — ' + write_insert['raised'] if write_insert['raised'] else 'no'}",
        f"PG delete raises          : {'YES' if write_delete['raised'] else 'no'}"
        + (f"  (logged {len(write_delete['logged'])} line(s))" if write_delete["logged"] else ""),
        f"read guard armed in THIS env (MONGO_GUARD_BLOCK_READS={saved or ''!r}): "
        f"{'YES' if previously_armed else 'NO'}",
        f"PG read raises as configured : {'YES' if read_ambient['raised'] else 'no'}",
        f"PG read raises if armed      : {'YES' if read_armed['raised'] else 'no'}",
    ]
    for entry in (write_delete, write_insert):
        for logged in entry["logged"]:
            lines.append(f"  guard logged → {logged}")

    if guarded.get(table.lower()) != (mode if mode in ("mongo", "mongo_read") else None):
        return Check("guard", FAIL,
                     f"the guard's live map says {guarded.get(table.lower())!r} for "
                     f"{table}, the committed map says {mode!r}", lines, data)

    if mode == "mongo":
        # At `mongo` both protections must be real: PG is frozen, so an
        # unguarded write is lost and an unguarded read is stale-as-current.
        if not write_insert["raised"]:
            return Check("guard", FAIL,
                         "table is at 'mongo' but a Postgres INSERT does NOT raise",
                         lines, data)
        if not read_armed["raised"]:
            return Check("guard", FAIL,
                         "table is at 'mongo' but a Postgres SELECT does not raise "
                         "even with the read guard armed", lines, data)
        if not previously_armed:
            lines.append(
                "NOTE: the read guard is OFF in this environment, so a surviving "
                "Postgres reader would return frozen rows silently right now. It "
                "raises when armed (measured above); arming it is a deploy-time "
                "choice, not a property of the code."
            )
        return Check("guard", PASS,
                     "writes raise; reads raise when armed", lines, data)

    if mode == "mongo_read":
        if write_insert["raised"]:
            return Check("guard", FAIL,
                         "table is at 'mongo_read' — Postgres is still dual-written, "
                         "so a raising INSERT would break the live writer", lines, data)
        return Check("guard", PASS,
                     "PG writes allowed (correct at mongo_read); a script-context "
                     "DELETE is logged, not blocked", lines, data)

    if write_insert["raised"] or read_ambient["raised"]:
        return Check("guard", FAIL,
                     f"table is at {mode!r} but the guard interferes with Postgres",
                     lines, data)
    return Check("guard", PASS, f"unguarded, which is correct at {mode!r}", lines, data)


# ── check 3: counts and collection resolution ──────────────────────────────
def check_counts(table: str, mode: str, pg: "ReadOnlyPG | None",
                 pg_error: str | None) -> Check:
    """Rows on one side, documents on the other, and WHICH collection was read.

    The collection is resolved through `collections.collection_for` — the same
    call the store makes — because a count taken from a name typed by hand is a
    count of whatever collection that name happened to create.
    """
    data: dict = {}
    lines: list[str] = []
    try:
        from app.db import mongo_store
        from app.db.collections import collection_for, renames_active, target_collection_for
    except Exception as exc:  # noqa: BLE001
        return _insufficient("counts", f"import failed: {type(exc).__name__}: {exc}")

    collection = collection_for(table)
    data["collection_for"] = collection
    data["target_collection"] = target_collection_for(table)
    data["renames_active"] = renames_active()
    lines.append(f"collection_for({table!r}) → {collection!r}"
                 + ("" if renames_active() else
                    f"  (rename map inert; target name would be "
                    f"{target_collection_for(table)!r})"))

    if pg is None:
        data["pg_rows"] = None
        data["pg_error"] = pg_error
        lines.append(f"Postgres: UNREACHABLE ({pg_error})")
    elif not pg.table_exists(table):
        data["pg_rows"] = None
        data["pg_table_exists"] = False
        lines.append("Postgres: table does not exist "
                     + ("(expected after teardown at mode 'mongo')" if mode == "mongo"
                        else "— which at this mode is a defect"))
    else:
        data["pg_table_exists"] = True
        from app.db.table_spec import quote_ident
        data["pg_rows"] = int(pg.one(f"SELECT count(*) FROM {quote_ident(table)}")[0])
        lines.append(f"Postgres rows : {data['pg_rows']:,}")

    try:
        data["mongo_docs"] = int(mongo_store.count_docs(table))
        lines.append(f"Mongo docs    : {data['mongo_docs']:,}")
    except Exception as exc:  # noqa: BLE001
        data["mongo_docs"] = None
        data["mongo_error"] = f"{type(exc).__name__}: {exc}"
        lines.append(f"Mongo: UNREACHABLE ({data['mongo_error']})")

    pg_rows, mongo_docs = data.get("pg_rows"), data.get("mongo_docs")
    if mongo_docs is None:
        return Check("counts", INSUFFICIENT, "Mongo could not be counted", lines, data)
    if pg_rows is None:
        if mode == "mongo" and data.get("pg_table_exists") is False:
            return Check("counts", PASS,
                         f"Mongo holds {mongo_docs:,}; the Postgres table is gone, "
                         "which is the end state for this mode", lines, data)
        return Check("counts", INSUFFICIENT, "Postgres could not be counted", lines, data)

    delta = mongo_docs - pg_rows
    data["delta"] = delta
    if delta < 0:
        return Check("counts", FAIL,
                     f"Mongo is {-delta:,} document(s) SHORT of Postgres", lines, data)
    if delta > 0:
        if mode == "mongo":
            lines.append(
                f"Mongo holds {delta:,} more document(s) than Postgres. At mode "
                "'mongo' that is EXPECTED, not a defect: Postgres has been frozen "
                "since the cutover while Mongo kept taking writes. It is also why "
                "the parity check below cannot be read as a live-data comparison "
                "for this mode — see that section."
            )
            return Check("counts", PASS,
                         f"pg={pg_rows:,} mongo={mongo_docs:,} (+{delta:,}, expected "
                         "at 'mongo': PG is frozen)", lines, data)
        lines.append(
            f"Mongo holds {delta:,} document(s) with no Postgres row. Both stores "
            f"are written at {mode!r}, so these are orphans or rows deleted from "
            "Postgres only."
        )
        return Check("counts", FAIL,
                     f"pg={pg_rows:,} mongo={mongo_docs:,} (+{delta:,} unexplained)",
                     lines, data)
    return Check("counts", PASS, f"pg={pg_rows:,} mongo={mongo_docs:,} (equal)",
                 lines, data)


# ── check 4: the read-provenance oracle ────────────────────────────────────
def check_provenance(table: str, pg: "ReadOnlyPG | None", pg_error: str | None,
                     sample: int) -> Check:
    """Can a returned timestamp name the store that served it?

    BSON datetimes are millisecond-precision; Postgres timestamps keep
    microseconds. So a value whose `microsecond % 1000 == 0` came from Mongo —
    probably. Three limits decide whether that inference is worth anything, and
    all three are MEASURED here rather than assumed:

    1. It is a per-timestamp false positive, not a verdict. A Postgres value
       lands on a whole millisecond about 1 time in 1000 by chance, so ONE
       aligned timestamp proves nothing. About 20 aligned values in a row is
       where the coincidence explanation dies (1e-60).
    2. That 1-in-1000 assumes Postgres microseconds are uniformly distributed on
       THIS table. If the column is written from a millisecond-precision source,
       the Postgres side is aligned too and the oracle has no power at all — a
       discriminator whose two classes look identical is not a discriminator.
       So the Postgres alignment rate below IS the false-positive rate, sampled
       from the real column.
    3. It says nothing whatsoever about documents that carry no timestamp. The
       count of sampled documents missing the field is reported for exactly that
       reason.
    """
    try:
        from app.db import mongo_store
        from app.db.collections import collection_for
    except Exception as exc:  # noqa: BLE001
        return _insufficient("provenance", f"import failed: {type(exc).__name__}: {exc}")

    data: dict = {"sample_size": sample}
    lines: list[str] = []

    if pg is None:
        return _insufficient("provenance",
                             f"Postgres unreachable, so the false-positive rate "
                             f"cannot be measured ({pg_error})")
    if not pg.table_exists(table):
        return _insufficient("provenance",
                             "the Postgres table is gone, so the oracle's "
                             "false-positive rate cannot be measured on it")

    ts_cols = pg.timestamp_columns(table)
    data["timestamp_columns"] = ts_cols
    if not ts_cols:
        return _insufficient("provenance", f"{table} has no timestamp column",
                             timestamp_columns=[])
    field_name = next((c for c in _TS_PREFERENCE if c in ts_cols), ts_cols[0])
    data["field"] = field_name
    lines.append(f"field sampled : {field_name!r} (of {', '.join(ts_cols)})")

    from app.db.table_spec import quote_ident
    rows = pg.all(
        f"SELECT {quote_ident(field_name)} FROM {quote_ident(table)} "
        f"WHERE {quote_ident(field_name)} IS NOT NULL "
        f"ORDER BY random() LIMIT %s", [sample])
    pg_values = [r[0] for r in rows if isinstance(r[0], datetime)]
    pg_aligned = sum(1 for v in pg_values if v.microsecond % 1000 == 0)

    try:
        docs = mongo_store.aggregate(table, [
            {"$sample": {"size": sample}},
            {"$project": {field_name: 1, "_id": 0}},
        ])
    except Exception as exc:  # noqa: BLE001
        return _insufficient("provenance",
                             f"Mongo sample failed: {type(exc).__name__}: {exc}",
                             field=field_name)
    mongo_values = [d.get(field_name) for d in docs]
    present = [v for v in mongo_values if isinstance(v, datetime)]
    absent = len(mongo_values) - len(present)
    mongo_aligned = sum(1 for v in present if v.microsecond % 1000 == 0)

    data.update({
        "collection": collection_for(table),
        "pg_sampled": len(pg_values), "pg_ms_aligned": pg_aligned,
        "mongo_sampled": len(mongo_values), "mongo_with_field": len(present),
        "mongo_without_field": absent, "mongo_ms_aligned": mongo_aligned,
    })

    pg_rate = (pg_aligned / len(pg_values)) if pg_values else None
    mongo_rate = (mongo_aligned / len(present)) if present else None
    data["pg_alignment_rate"] = pg_rate
    data["mongo_alignment_rate"] = mongo_rate

    lines += [
        f"Postgres side : {pg_aligned}/{len(pg_values)} millisecond-aligned"
        + (f" ({pg_rate:.2%})" if pg_rate is not None else " (no rows sampled)")
        + "   ← this is the FALSE-POSITIVE RATE, measured, not assumed",
        f"Mongo side    : {mongo_aligned}/{len(present)} millisecond-aligned"
        + (f" ({mongo_rate:.2%})" if mongo_rate is not None else " (field never present)"),
        f"Mongo docs sampled with NO {field_name!r}: {absent}/{len(mongo_values)}"
        " — the oracle is silent about every one of them",
    ]

    if not pg_values or not present:
        return Check("provenance", INSUFFICIENT,
                     "one side produced no timestamps to compare", lines, data)

    # Power: the two populations must be distinguishable. If Postgres is itself
    # mostly aligned, an aligned value carries almost no information.
    usable = pg_rate is not None and pg_rate <= 0.05 and mongo_rate >= 0.95
    data["oracle_usable"] = bool(usable)
    lines.append("")
    lines.append("HOW TO USE THIS ORACLE, and what it does not say:")
    lines.append("  * It identifies the store that SERVED a read, from the")
    lines.append(f"    precision of the {field_name!r} values in the response.")
    lines.append(f"  * One aligned value is a {pg_rate:.3%} coincidence on this table.")
    lines.append("    Require ~20 aligned values before calling it proof; a single")
    lines.append("    value, or five, is not evidence of anything.")
    lines.append("  * It is silent on documents carrying no timestamp "
                 f"({absent} of {len(mongo_values)} sampled here).")
    lines.append("  * A Postgres fallback cannot fake alignment, but a reader that")
    lines.append("    re-serialises through a millisecond format can. Sample the")
    lines.append("    STORED value, not a JSON round-trip, when using this.")

    if not usable:
        return Check("provenance", INSUFFICIENT,
                     f"the oracle is BLIND on this table: Postgres is "
                     f"{pg_rate:.1%} aligned and Mongo {mongo_rate:.1%}, so an "
                     "aligned value does not name a store", lines, data)
    return Check("provenance", PASS,
                 f"oracle usable: pg {pg_rate:.2%} aligned vs mongo {mongo_rate:.2%}",
                 lines, data)


# ── check 5: parity, exhaustive only ───────────────────────────────────────
_VERIFY_LINE = re.compile(
    r"VERIFY-ALL: pg_rows=([\d,]+) mongo_docs=([\d,]+) "
    r"missing-in-mongo=([\d,]+) drifted-fields=([\d,]+)")

# --verify-all prints example offending rows, and one of those rows can be a
# 1536-dimension pgvector. Verbatim, `embeddings` produced a 162 KB signoff
# artifact of which 160 KB was one column printed five times. Truncate the
# captured lines: the counts are the evidence, the examples are a pointer.
_EXAMPLE_LINE_CAP = 300


def _cap(line: str) -> str:
    return line if len(line) <= _EXAMPLE_LINE_CAP else (
        line[:_EXAMPLE_LINE_CAP] + f"… [+{len(line) - _EXAMPLE_LINE_CAP} chars]")


def check_parity(table: str, mode: str, timeout: int, skip: bool) -> Check:
    """The EXHAUSTIVE verdict, or none at all.

    `pg_to_mongo_backfill.py --verify-fields N` draws N random rows, so its
    verdict depends on the draw: it scored `context_blobs` OK three runs out of
    four while a full sweep of the same data found 117 drifted timestamps and 2
    missing documents. This tool therefore never runs the sampled path and never
    prints its output as a parity statement. It shells out to `--verify-all`,
    which writes nothing, and reports the mode that produced the number.
    """
    if skip:
        return _insufficient("parity", "--skip-parity was passed; no exhaustive "
                                       "verify was run, so there is no parity verdict",
                             mode_run="skipped")
    cmd = [sys.executable, str(REPO_ROOT / "scripts" / "pg_to_mongo_backfill.py"),
           table, "--verify-all"]
    env = dict(os.environ)
    env["PYTHONPATH"] = str(REPO_ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    try:
        proc = subprocess.run(cmd, cwd=str(REPO_ROOT), env=env, capture_output=True,
                              text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return _insufficient("parity",
                             f"--verify-all did not finish within {timeout}s. A "
                             "truncated exhaustive sweep is not a partial pass — "
                             "raise --parity-timeout and re-run",
                             mode_run="exhaustive (--verify-all)", timed_out=True,
                             command=shlex.join(cmd))
    except Exception as exc:  # noqa: BLE001
        return _insufficient("parity", f"{type(exc).__name__}: {exc}",
                             command=shlex.join(cmd))

    out = (proc.stdout or "") + (proc.stderr or "")
    data = {
        "mode_run": "exhaustive (--verify-all)",
        "command": shlex.join(cmd),
        "returncode": proc.returncode,
        "output_tail": [_cap(ln) for ln in out.strip().splitlines()[-25:]],
    }
    m = _VERIFY_LINE.search(out)
    if m:
        pg_rows, mongo_docs, missing, drifted = (int(x.replace(",", "")) for x in m.groups())
        data.update({"pg_rows": pg_rows, "mongo_docs": mongo_docs,
                     "missing_in_mongo": missing, "drifted_fields": drifted})
    lines = [f"verdict source: {data['mode_run']} — every row compared, no sampling",
             f"command       : {data['command']}"]
    lines += [f"  {ln}" for ln in data["output_tail"]]

    if m is None:
        return Check("parity", INSUFFICIENT,
                     f"--verify-all produced no VERIFY-ALL line (rc={proc.returncode}); "
                     "the table may have no derivable spec", lines, data)

    if missing or drifted:
        # A defect count is only as good as the key it joined on and the
        # encodings its comparator understands, and both have already been wrong
        # here: `embeddings` reports 701 "missing" rows that are present under a
        # different `id` (the store re-keys on re-embed; its stable identity is
        # source_table+source_id), and 27,956 "drifted" vectors that are
        # byte-identical once unpacked — Postgres hands back a pgvector list,
        # Mongo a BSON Binary, and `_values_equal` cannot equate the two. So the
        # verdict is FAIL, which is correct, but resolve the classes before
        # acting: a promotion blocked for a comparator artefact is as expensive
        # as one waved through for a real one.
        try:
            key_field = (ledger_rows().get(table) or {}).get("key_field")
        except Exception:  # noqa: BLE001
            key_field = None
        lines.append(
            f"Before acting on these counts, check the join key (ledger says "
            f"{key_field!r}) is stable for this table, and that every mismatched "
            "field is one the comparator can compare — a list vs a BSON Binary "
            "never compares equal even when the bytes agree."
        )
        return Check("parity", FAIL,
                     f"{missing:,} row(s) missing from Mongo, {drifted:,} drifted "
                     "field(s)", lines, data)
    if mongo_docs > pg_rows:
        if mode == "mongo":
            lines.append(
                f"NOTE: --verify-all reports MISMATCH because Mongo holds "
                f"{mongo_docs - pg_rows:,} more documents than Postgres. At mode "
                "'mongo' Postgres is frozen by design, so that surplus is the "
                "post-cutover writes, not a parity defect. Every PG row that DOES "
                "exist was found in Mongo and matched field for field, which is "
                "the whole claim parity can make once PG stops being written."
            )
            return Check("parity", PASS,
                         f"every one of {pg_rows:,} frozen Postgres rows is present "
                         f"and identical in Mongo (+{mongo_docs - pg_rows:,} "
                         "post-cutover docs)", lines, data)
        return Check("parity", FAIL,
                     f"Mongo holds {mongo_docs - pg_rows:,} document(s) with no "
                     f"Postgres row while both stores are written at {mode!r}",
                     lines, data)
    return Check("parity", PASS,
                 f"exhaustive: {pg_rows:,} rows, 0 missing, 0 drifted", lines, data)


# ── check 6: mirror-failure evidence, with its positive control ────────────
def _message_of(call: ast.Call) -> str | None:
    if not call.args:
        return None
    first = call.args[0]
    if isinstance(first, ast.Constant) and isinstance(first.value, str):
        return first.value
    if isinstance(first, ast.JoinedStr):
        return "".join(v.value for v in first.values
                       if isinstance(v, ast.Constant) and isinstance(v.value, str))
    return None


def _logger_calls(tree: ast.AST):
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        level = node.func.attr
        if level not in {"debug", "info", "warning", "error", "critical", "exception"}:
            continue
        target = node.func.value
        name = getattr(target, "id", None) or getattr(target, "attr", None)
        if name not in {"logger", "log", "logging", "_logger"}:
            continue
        yield level, node


def mirror_log_sites(app_dir: Path) -> list[dict]:
    """Every log site that would report a mirror failure, and its LEVEL.

    Walks the AST instead of grepping, so a reformatted or multi-line call
    cannot hide. The level is the point: six of these sat at DEBUG under an INFO
    root logger, so the line could not be emitted at all and 48 hours of soak
    grep returned a zero that meant nothing.
    """
    sites: list[dict] = []
    for path in sorted(app_dir.rglob("*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        for level, call in _logger_calls(tree):
            msg = _message_of(call)
            if not msg:
                continue
            low = msg.lower()
            if not any(w in low for w in _EVIDENCE_WORDS):
                continue
            if any(ok in low for ok in _ALLOWED_DEBUG_SUBSTRINGS):
                continue
            sites.append({
                "file": str(path.relative_to(app_dir.parent)),
                "line": call.lineno,
                "level": level,
                "message": msg[:90],
                "emittable": level not in {"debug"},
            })
    return sites


def check_logs(table: str, logs_cmd: str | None, logs_file: str | None,
               window: str) -> Check:
    """Mirror-failure hits, and the WARN/ERROR volume that proves the feed lives.

    "Zero mirror failures" is worth exactly as much as the stream it was counted
    in. The service log carried 9,816 WARN/ERROR lines in 48 hours, so its zero
    was real; the client log carried 1 in 63,178 lines, so its zero was worthless
    and nobody could tell the two apart by reading the greps. This check
    therefore never reports a count of failures without the total volume beside
    it, and a zero drawn from a silent stream is reported as INSUFFICIENT with
    the words DEAD FEED, not as a clean.
    """
    lines: list[str] = []
    data: dict = {"window": window}

    sites = mirror_log_sites(REPO_ROOT / "app")
    invisible = [s for s in sites if not s["emittable"]]
    data["evidence_sites"] = len(sites)
    data["evidence_sites_below_warning"] = len(invisible)
    data["invisible_sites"] = invisible
    lines.append(f"static: {len(sites)} mirror/fallback/guard log site(s) in app/; "
                 f"{len(invisible)} below WARNING and therefore unprintable under "
                 "the INFO root logger")
    for s in invisible:
        lines.append(f"  INVISIBLE {s['file']}:{s['line']} logger.{s['level']}"
                     f"({s['message']!r})")

    text: str | None = None
    if logs_file:
        try:
            text = Path(logs_file).read_text(encoding="utf-8", errors="replace")
            data["source"] = f"file:{logs_file}"
        except Exception as exc:  # noqa: BLE001
            data["source_error"] = f"{type(exc).__name__}: {exc}"
    else:
        cmd = logs_cmd or f"docker logs --since {window} trading-service"
        data["source"] = f"cmd:{cmd}"
        try:
            proc = subprocess.run(cmd, shell=True, capture_output=True, text=True,
                                  timeout=180)
            text = (proc.stdout or "") + (proc.stderr or "")
            data["source_returncode"] = proc.returncode
        except Exception as exc:  # noqa: BLE001
            data["source_error"] = f"{type(exc).__name__}: {exc}"

    if text is None:
        lines.append(f"log stream: UNAVAILABLE ({data.get('source_error')})")
        return Check("logs", INSUFFICIENT,
                     "no log stream was read, so there is no soak evidence "
                     "either way", lines, data)

    all_lines = [ln for ln in text.splitlines() if ln.strip()]
    warnerr = [ln for ln in all_lines if _WARNERR_RE.search(ln)]
    hits = [ln for ln in all_lines if _MIRROR_RE.search(ln)]
    table_hits = [ln for ln in hits if table in ln]
    data.update({
        "total_lines": len(all_lines),
        "warn_error_lines": len(warnerr),
        "mirror_failure_lines": len(hits),
        "mirror_failure_lines_naming_table": len(table_hits),
        "examples": [_cap(h) for h in hits[:5]],
    })
    lines += [
        f"source        : {data['source']}",
        f"lines read    : {len(all_lines):,}",
        f"WARN/ERROR    : {len(warnerr):,}   ← POSITIVE CONTROL: this is what "
        "proves the stream is alive",
        f"mirror failures: {len(hits):,} ({len(table_hits):,} naming {table!r})",
    ]
    for ex in data["examples"]:
        lines.append(f"  {ex}")

    if len(all_lines) == 0 or len(warnerr) == 0:
        lines.append(
            "DEAD FEED: this window produced no WARN/ERROR line at all. A stream "
            "that never carries a warning cannot be used to show that no mirror "
            "failure was warned about. The zero above is an artefact of the feed, "
            "not a fact about the mirror."
        )
        return Check("logs", INSUFFICIENT,
                     f"DEAD FEED — {len(hits)} mirror hits in a stream carrying "
                     f"{len(warnerr)} WARN/ERROR lines over {len(all_lines)} lines",
                     lines, data)
    if hits:
        return Check("logs", FAIL,
                     f"{len(hits):,} mirror-failure line(s) in a live stream "
                     f"({len(warnerr):,} WARN/ERROR over {len(all_lines):,} lines)",
                     lines, data)
    if invisible:
        return Check("logs", INSUFFICIENT,
                     f"0 mirror-failure lines, but {len(invisible)} log site(s) sit "
                     "below WARNING and could not have printed — this zero is not "
                     "falsifiable", lines, data)
    return Check("logs", PASS,
                 f"0 mirror-failure lines in a demonstrably live stream "
                 f"({len(warnerr):,} WARN/ERROR over {len(all_lines):,} lines)",
                 lines, data)


# ── the bundle ─────────────────────────────────────────────────────────────
def prove(table: str, args, env_info: dict) -> dict:
    checks: list[Check] = []

    flags = check_flags(table, env_info)
    checks.append(flags)
    mode = flags.data.get("effective_mode") or "pg"

    checks.append(check_guard(table, mode))

    pg = None
    pg_error = None
    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        pg_error = "DATABASE_URL is not set"
    else:
        try:
            pg = ReadOnlyPG(dsn)
        except Exception as exc:  # noqa: BLE001
            pg_error = f"{type(exc).__name__}: {exc}"
    try:
        checks.append(check_counts(table, mode, pg, pg_error))
        checks.append(check_provenance(table, pg, pg_error, args.sample))
    finally:
        if pg is not None:
            pg.close()

    checks.append(check_parity(table, mode, args.parity_timeout, args.skip_parity))
    checks.append(check_logs(table, args.logs_cmd, args.logs_file, args.log_window))

    verdict = worst(c.status for c in checks)
    return {
        "table": table,
        "mode": mode,
        "verdict": verdict,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "checks": [asdict(c) for c in checks],
    }


def render(bundle: dict) -> str:
    out: list[str] = []
    bar = "=" * 78
    out.append(bar)
    out.append(f"PROVE MONGO — {bundle['table']}   (mode: {bundle['mode']})")
    out.append(f"generated {bundle['generated_at']}")
    out.append(bar)
    for c in bundle["checks"]:
        out.append("")
        out.append(f"[{c['status']:12}] {c['name']}: {c['headline']}")
        for line in c["lines"]:
            out.append(f"    {line}")
    out.append("")
    out.append("-" * 78)
    out.append(f"VERDICT: {bundle['verdict']}  ({bundle['table']} @ {bundle['mode']})")
    if bundle["verdict"] == INSUFFICIENT:
        unrun = [c["name"] for c in bundle["checks"] if c["status"] == INSUFFICIENT]
        out.append(f"  INSUFFICIENT EVIDENCE — these checks did not produce a "
                   f"verdict: {', '.join(unrun)}.")
        out.append("  This is not a pass. Nothing here says the table is wrong; it "
                   "says the")
        out.append("  evidence to promote it was not gathered.")
    elif bundle["verdict"] == FAIL:
        bad = [c["name"] for c in bundle["checks"] if c["status"] == FAIL]
        out.append(f"  FAILED checks: {', '.join(bad)}")
    out.append("-" * 78)
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--table", help="the Postgres table name to prove")
    g.add_argument("--all", action="store_true",
                   help="every table flagged in app/db/mongo_backends.env")
    ap.add_argument("--json", metavar="PATH",
                    help="write the machine-readable bundle here (a directory, or "
                         "a path ending in '/', gets one file per table)")
    ap.add_argument("--env-file", help="path to the .env holding DATABASE_URL and "
                                       "PRISM_MONGO_URI (default: repo .env, then "
                                       "the main worktree's)")
    ap.add_argument("--sample", type=int, default=200, metavar="N",
                    help="timestamps sampled per side for the provenance oracle")
    ap.add_argument("--parity-timeout", type=int, default=1800, metavar="SECONDS",
                    help="give up on --verify-all after this long (the result is "
                         "then INSUFFICIENT, never a partial pass)")
    ap.add_argument("--skip-parity", action="store_true",
                    help="do not run the exhaustive verify (the parity check then "
                         "reports INSUFFICIENT — it is never inferred)")
    ap.add_argument("--logs-cmd", metavar="CMD",
                    help="shell command producing the service log window "
                         "(default: docker logs --since WINDOW trading-service)")
    ap.add_argument("--logs-file", metavar="PATH",
                    help="read the log window from a captured file instead")
    ap.add_argument("--log-window", default="48h", metavar="DURATION",
                    help="soak window passed to the default docker logs command")
    args = ap.parse_args(argv)

    env_path = find_env_file(args.env_file)
    if env_path is None:
        print("FAIL: no .env found (looked for the repo root and the main "
              "worktree). Pass --env-file.", file=sys.stderr)
        return EXIT_CANNOT_START
    env_info = load_environment(env_path)

    try:
        flags = committed_backend_map()
    except Exception as exc:  # noqa: BLE001
        print(f"FAIL: cannot read app/db/mongo_backends.env: "
              f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return EXIT_CANNOT_START

    # The committed map is the flag state the containers run; force it into the
    # environment BEFORE app.db.mongo_store is first imported, or every guard
    # verdict below describes some other configuration. See load_environment.
    os.environ["MONGO_STORE_BACKEND"] = ",".join(
        f"{t}:{m}" for t, m in sorted(flags.items()))

    if args.all:
        tables = sorted(flags)
    else:
        tables = [args.table]
        if args.table not in flags and args.table not in ledger_rows():
            print(f"FAIL: {args.table!r} is neither flagged in mongo_backends.env "
                  "nor present in migration_ledger.json", file=sys.stderr)
            return EXIT_CANNOT_START

    print(f"# .env: {env_info['env_file']}")
    print(f"# backend map: app/db/mongo_backends.env ({len(flags)} flagged tables)")
    if env_info.get("ambient_backend_map"):
        print("# NOTE: an ambient MONGO_STORE_BACKEND was present and has been "
              "overridden by the committed map")

    bundles = []
    for t in tables:
        bundle = prove(t, args, env_info)
        bundles.append(bundle)
        print(render(bundle))

    if args.json:
        target = Path(args.json)
        per_table = args.json.endswith(("/", os.sep)) or target.is_dir()
        doc_meta = {"schema_version": SCHEMA_VERSION, "tool": "scripts/prove_mongo.py",
                    "git": git_provenance(), "env_file": env_info["env_file"]}
        if per_table:
            target.mkdir(parents=True, exist_ok=True)
            for b in bundles:
                out = dict(doc_meta, **b)
                (target / f"prove_mongo_{b['table']}.json").write_text(
                    json.dumps(out, indent=2, default=str) + "\n", encoding="utf-8")
            print(f"\nwrote {len(bundles)} bundle(s) to {target}/")
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            out = dict(doc_meta, tables=bundles,
                       verdict=worst(b["verdict"] for b in bundles))
            target.write_text(json.dumps(out, indent=2, default=str) + "\n",
                              encoding="utf-8")
            print(f"\nwrote {target}")

    overall = worst(b["verdict"] for b in bundles)
    if len(bundles) > 1:
        print(f"\nOVERALL: {overall} over {len(bundles)} table(s)")
        for b in bundles:
            print(f"  {b['verdict']:12} {b['table']} ({b['mode']})")
    return EXIT_BY_STATUS[overall]


if __name__ == "__main__":
    raise SystemExit(main())
