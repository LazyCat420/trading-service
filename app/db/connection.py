"""
PostgreSQL Connection — Thread-safe connection pool.

Uses psycopg3 (sync) with a connection pool. Schema auto-initialized on first use.

Public API:
    get_db()         → a CONTEXT MANAGER yielding a PooledCursor. Always
                       `with get_db() as db:` — the cursor exists only inside
                       the block, and the connection returns to the pool on
                       exit. `db = get_db()` hands back a
                       `_GeneratorContextManager`, which has no `.execute`.
    get_write_lock() → asyncio.Lock (kept for safety, Postgres handles concurrency natively)
    close_db()       → shutdown the pool

This docstring used to say "returns a PooledCursor that behaves like a standard
DB-API cursor", and that is what `dossier_service` and `research_queue_service`
were written against on 2026-08-07: both called `db = get_db()` then
`db.execute(...)`, so every method raised `AttributeError` on its first
statement. Nothing caught it because the autouse test fixture patched `get_db`
with `MagicMock(return_value=cursor)` — which satisfies BOTH the correct and the
incorrect usage. See tests/conftest.py.
"""

import asyncio
from contextlib import contextmanager
import json as _json
import logging
import os
import threading
import time
import traceback
from typing import Any

import psycopg
import psycopg.rows
from psycopg_pool import ConnectionPool

from app.config import settings
from app.db.pg_write_guard import check_pg_write

logger = logging.getLogger(__name__)


def safe_jsonb(val):
    """Normalize a JSONB value — always returns a dict/list regardless of
    whether _stringify_jsonb turned it into a string.

    Use this when reading JSONB columns that may have been stringified
    by the PooledCursor compatibility layer.
    """
    if isinstance(val, str):
        try:
            return _json.loads(val)
        except Exception:
            return val
    return val  # already a dict/list

_lock = threading.Lock()
_pool: ConnectionPool | None = None
_async_write_lock: asyncio.Lock | None = None


def get_write_lock() -> asyncio.Lock:
    """Return the shared asyncio.Lock for serializing write operations.

    PostgreSQL handles concurrent writes natively via MVCC, but we keep
    this lock for backward compatibility with code that already uses it.

    Usage in async code:
        async with get_write_lock():
            with get_db() as db:
                db.execute("INSERT ...")
    """
    global _async_write_lock
    if _async_write_lock is None:
        _async_write_lock = asyncio.Lock()
    return _async_write_lock


# ── Postgres strict mode (No placeholder translation) ─────────────────
# The codebase must explicitly use %s for placeholders, not ?.


class PooledCursor:
    """Wrapper around a psycopg connection that mimics standard DB-API cursor behavior.

    Key compatibility features:
    - execute(sql, params) translates %s → %s placeholders
    - fetchone() / fetchall() work identically
    - .description returns column metadata
    - Auto-commits after each execute (for legacy compatibility)
    """

    def __init__(self, conn: psycopg.Connection, pool=None):
        self._conn = conn
        self._cursor = conn.cursor()
        self._pool_ref = pool

        self.description: Any = None
        self._closed = False
        self._in_transaction = False
        self._created_at = time.monotonic()
        # Capture creation callsite for leak detection
        self._origin = "".join(traceback.format_stack(limit=4)[:-1])

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()

    def __del__(self):
        if getattr(self, "_closed", True):
            return
        # Connection was never closed — this is a LEAK
        age = time.monotonic() - getattr(self, "_created_at", 0)
        origin = getattr(self, "_origin", "unknown")
        logger.warning(
            "[DB LEAK] PooledCursor garbage-collected after %.1fs without close()!\n"
            "  Created at:\n%s",
            age,
            origin,
        )
        try:
            self.close()
        except Exception:
            pass

    def execute(self, sql: str, params=None) -> "PooledCursor":
        """Execute SQL with automatic placeholder translation."""

        # Fail closed on writes to tables that have cut over to Mongo.
        check_pg_write(sql)

        # Convert list params to tuple (psycopg requires tuples)
        if isinstance(params, list):
            params = tuple(params)

        try:
            self._cursor.execute(sql, params)
            self.description = self._cursor.description
        except Exception:
            # Rollback on error to avoid "in error state" blocking
            try:
                if not getattr(self, "_in_transaction", False):
                    self._conn.rollback()
            except Exception:
                pass
            raise
        else:
            # Auto-commit for write operations
            if not self._conn.autocommit and not getattr(self, "_in_transaction", False):
                try:
                    self._conn.commit()
                except Exception:
                    pass
        return self

    def executemany(self, sql: str, params_seq) -> "PooledCursor":
        """Execute SQL for a sequence of parameters."""

        check_pg_write(sql)

        # Ensure parameters are sequences (tuples)
        cleaned_seq = [tuple(p) if isinstance(p, list) else p for p in params_seq]

        try:
            self._cursor.executemany(sql, cleaned_seq)
            self.description = self._cursor.description
        except Exception:
            try:
                if not getattr(self, "_in_transaction", False):
                    self._conn.rollback()
            except Exception:
                pass
            raise
        else:
            if not self._conn.autocommit and not getattr(self, "_in_transaction", False):
                try:
                    self._conn.commit()
                except Exception:
                    pass
        return self

    @contextmanager
    def transaction(self):
        """Standard psycopg transaction management."""
        self._in_transaction = True
        try:
            with self._conn.transaction():
                yield
        finally:
            self._in_transaction = False

    def commit(self):
        """No-op: PooledCursor auto-commits after every execute().

        Kept for backward compatibility with legacy code
        which called db.commit() explicitly.
        """
        pass

    def rollback(self):
        """Rollback the current transaction."""
        try:
            self._conn.rollback()
        except Exception:
            pass

    def _stringify_jsonb(self, row):
        """Convert dicts/lists returned by psycopg back to JSON strings for legacy compatibility."""
        if row is None:
            return None
        changed = False
        new_row = []
        for item in row:
            if isinstance(item, (dict, list)):
                import orjson

                new_row.append(orjson.dumps(item).decode("utf-8"))
                changed = True
            else:
                new_row.append(item)
        return tuple(new_row) if changed else row

    def fetchone(self):
        return self._stringify_jsonb(self._cursor.fetchone())

    def fetchall(self):
        rows = self._cursor.fetchall()
        return [self._stringify_jsonb(r) for r in rows]

    def close(self):
        """Return the connection to the pool."""
        if getattr(self, "_closed", True):
            return
        self._closed = True
        try:
            self._cursor.close()
        except Exception:
            pass
        try:
            if self._pool_ref is not None:
                self._pool_ref.putconn(self._conn)
        except Exception:
            pass


def _ensure_pool() -> ConnectionPool:
    """Ensure the connection pool exists. Thread-safe init only."""
    global _pool
    if _pool is not None:
        return _pool
    with _lock:
        if _pool is None:
            if os.getenv("TRADING_BOT_TEST_DB") == "1":
                db_url = settings.TEST_DATABASE_URL
            else:
                db_url = settings.DATABASE_URL
            logger.info(
                f"[DB] Connecting to PostgreSQL: {db_url.split('@')[-1] if '@' in db_url else db_url}"
            )

            # Only initialize PG schema if explicitly requested (defuse auto-creation of dropped tables)
            if os.getenv("INIT_PG_SCHEMA") == "1" or os.getenv("TRADING_BOT_TEST_DB") == "1":
                _init_schema(db_url)

            def _configure_connection(conn):
                from pgvector.psycopg import register_vector

                try:
                    register_vector(conn)
                except Exception as e:
                    logger.warning(f"[DB] Failed to register pgvector: {e}")

            import sys
            is_tool = (
                os.getenv("IS_TOOL_PROCESS") == "true"
                or any("execute_tool.py" in arg for arg in sys.argv)
            )
            min_sz = 1 if is_tool else 10
            max_sz = 2 if is_tool else 50
            if is_tool:
                logger.info(f"[DB] Tool process detected. Scaling down ConnectionPool to size min={min_sz}, max={max_sz}")

            _pool = ConnectionPool(
                conninfo=db_url,
                min_size=min_sz,
                max_size=max_sz,
                kwargs={"autocommit": True},
                configure=_configure_connection,
            )
            # Wait for the pool to be ready
            _pool.wait()

            # Seed bot and run migrations using the pool
            _seed_and_migrate()
            logger.info("[DB] PostgreSQL connection pool initialized")
        return _pool


@contextmanager
def get_db():
    """Get a cursor-like object from the PostgreSQL connection pool.

    Returns a context manager yielding a PooledCursor that is API-compatible
    with standard DB-API cursors. The underlying connection is taken from the pool
    and will be returned when the block exits.
    """
    pool = _ensure_pool()
    try:
        conn = pool.getconn(timeout=5.0)
    except Exception as e:
        if "timeout" in str(e).lower() or "connection" in str(e).lower():
            import gc

            logger.warning(
                "[DB] Pool timeout! Forcing GC collection to reclaim leaked connections..."
            )
            gc.collect()
            conn = pool.getconn(timeout=5.0)
        else:
            raise

    cursor = PooledCursor(conn, pool=pool)
    try:
        yield cursor
    finally:
        cursor.close()


def split_sql_statements(sql: str) -> list[str]:
    """Split a SQL script into individual statements.

    `schema_pg.sql` contains no dollar-quoted bodies (no functions, no `DO`
    blocks), so splitting on `;` outside string literals and comments is exact
    for this file. Asserted by `tests/unit/test_schema_init_is_resilient.py`,
    which fails if a `$$` block is ever added — at which point this needs a real
    lexer, not a bigger regex.
    """
    statements: list[str] = []
    buf: list[str] = []
    in_single = in_double = in_line_comment = in_block_comment = False
    i = 0
    while i < len(sql):
        ch = sql[i]
        nxt = sql[i + 1] if i + 1 < len(sql) else ""

        if in_line_comment:
            buf.append(ch)
            if ch == "\n":
                in_line_comment = False
        elif in_block_comment:
            buf.append(ch)
            if ch == "*" and nxt == "/":
                buf.append(nxt)
                i += 1
                in_block_comment = False
        elif in_single:
            buf.append(ch)
            if ch == "'":
                in_single = False
        elif in_double:
            buf.append(ch)
            if ch == '"':
                in_double = False
        elif ch == "-" and nxt == "-":
            buf.append(ch)
            in_line_comment = True
        elif ch == "/" and nxt == "*":
            buf.append(ch)
            in_block_comment = True
        elif ch == "'":
            buf.append(ch)
            in_single = True
        elif ch == '"':
            buf.append(ch)
            in_double = True
        elif ch == ";":
            statements.append("".join(buf).strip())
            buf = []
        else:
            buf.append(ch)
        i += 1

    tail = "".join(buf).strip()
    if tail:
        statements.append(tail)
    return [s for s in statements if s and not _is_only_comments(s)]


def _is_only_comments(stmt: str) -> bool:
    for line in stmt.splitlines():
        line = line.strip()
        if line and not line.startswith("--"):
            return False
    return True


def _init_schema(db_url: str):
    """Run schema_pg.sql to create all tables, one statement at a time.

    STATEMENT-AT-A-TIME IS THE WHOLE POINT
    --------------------------------------
    This used to be a single `cur.execute(sql)` over the entire 320-statement
    file. Postgres treats that as one batch, so the FIRST failure discards every
    statement after it — silently, because the caller logs the error and boot
    continues by design.

    That is not hypothetical. `CREATE TABLE IF NOT EXISTS` is a no-op against an
    existing table, so any table that has drifted from the file keeps its old
    columns; `schema_pg.sql:1062` then does
    `CREATE INDEX ... ON congress_trades(bioguide_id)` and fails, because that
    column arrived later than the table did. On the isolated test database
    (`:5433/trading_bot_test`) this truncated the schema at **161 tables against
    production's 214** — and because every `real_db` test is gated behind
    `TRADING_BOT_TEST_DB`, which nobody could turn on while the schema was half
    built, the whole persistence-testing surface had been unusable for as long
    as the drift existed.

    Now each statement runs on its own and a failure is recorded rather than
    fatal, so the tail of the file still executes and the log names exactly what
    could not be applied. The function still raises if NOTHING applied — a
    database that rejected every statement is a connection or permission
    problem, not drift, and should not be mistaken for success.
    """
    schema_path = os.path.join(os.path.dirname(__file__), "schema_pg.sql")
    if not os.path.exists(schema_path):
        logger.warning(f"[DB] Schema file not found: {schema_path}")
        return

    with open(schema_path, encoding="utf-8") as f:
        sql = f.read()

    statements = split_sql_statements(sql)
    applied = 0
    failures: list[tuple[str, str]] = []

    with psycopg.connect(db_url, autocommit=True) as conn:
        for stmt in statements:
            try:
                with conn.cursor() as cur:
                    cur.execute(stmt)
                applied += 1
            except Exception as e:
                # First line only: these are 50-line CREATE TABLEs and the
                # first line names the object.
                failures.append((stmt.splitlines()[0][:120], str(e).splitlines()[0]))

    if failures:
        logger.error(
            "[DB] schema_pg.sql: %d/%d statements applied, %d FAILED. The "
            "database is now missing exactly these objects — each is drift "
            "between the file and the live schema, not a transient error:",
            applied, len(statements), len(failures),
        )
        for head, err in failures[:20]:
            logger.error("[DB]   %s  ->  %s", head, err)
        if len(failures) > 20:
            logger.error("[DB]   … and %d more", len(failures) - 20)
    else:
        logger.info(
            "[DB] Schema initialized from schema_pg.sql (%d statements)", applied
        )

    if applied == 0 and statements:
        raise RuntimeError(
            f"schema_pg.sql: every one of {len(statements)} statements failed — "
            "this is a connection or permission problem, not schema drift"
        )


def _seed_and_migrate():
    """Seed default bot and run migrations after pool is initialized."""
    # ── Seed default bot in MongoDB ──
    try:
        from app.db import mongo_query, mongo_store
        from app.config import settings as _s
        import datetime
        existing_bot = mongo_query.find_row('bots', {'bot_id': _s.BOT_ID}, ['bot_id'])
        if not existing_bot:
            mongo_store.insert_docs('bots', [{
                'bot_id': _s.BOT_ID,
                'display_name': 'Lazy Trader V4',
                'model_name': _s.ACTIVE_MODEL,
                'status': 'idle',
                'cash_balance': mongo_store.dec128(_s.STARTING_CASH),
                'starting_cash': mongo_store.dec128(_s.STARTING_CASH),
                'total_pnl': mongo_store.dec128(0.0),
                'win_rate': 0.0,
                'total_trades': 0,
                'is_active': True,
                'created_at': datetime.datetime.now(datetime.UTC),
            }])
            logger.info(f"[DB] Seeded default bot in MongoDB: {_s.BOT_ID}")
    except Exception as e:
        logger.info(f"[DB] Mongo bot seed skipped: {e}")

    # ── Auto-migrations for existing PostgreSQL databases (guarded) ──
    if os.getenv("RUN_PG_MIGRATIONS") == "1":
        try:
            from app.db.migrations import run_migrations
            conn = _pool.getconn()
            try:
                run_migrations(conn)
                conn.commit()
            except Exception as e:
                try:
                    conn.rollback()
                except Exception:
                    pass
                logger.warning(f"[DB] Migration warning: {e}")
            finally:
                _pool.putconn(conn)
        except Exception as e:
            logger.warning(f"[DB] Migration setup warning: {e}")

        try:
            from app.db.init_db import run_auto_migrations
            run_auto_migrations()
        except Exception as e:
            logger.warning(f"[DB] Inline auto-migrations warning: {e}")


def close_db():
    """Close the connection pool (for cleanup)."""
    global _pool
    with _lock:
        if _pool is not None:
            try:
                _pool.close()
                logger.info("[DB] PostgreSQL connection pool closed")
            except Exception as e:
                logger.warning(f"[DB] Pool close error: {e}")
            _pool = None
