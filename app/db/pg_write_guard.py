"""Fail-closed backstop against Postgres writes to already-migrated tables.

The Postgres→MongoDB migration moves each table through
``pg → dual → mongo_read → mongo``.  At ``mongo`` there is **no legitimate
Postgres write left from anybody**: the app reads and writes Mongo only, and a
stray ``DELETE``/``UPDATE``/``INSERT`` against the dead SQL table is, by
definition, a write nobody will ever read.  That is not a hypothetical — it is
exactly what happened to ``embeddings``: a scrub script deleted from the store
nobody reads and left the live Mongo docs poisoned (fixed in ``920123c``; see
``scripts/scrub_poisoned_memories.py``).

This module turns that silent class of bug into a loud one:

* table at ``mongo``      → the write **raises** (fail closed)
* table at ``mongo_read`` → a ``DELETE``/``TRUNCATE`` from *script* context is
  logged at ERROR, because PG is still dual-written there and a PG-only delete
  leaves the live Mongo copy alive.  It is **not** blocked: at ``mongo_read``
  the application legitimately writes both stores, and the connection layer
  cannot tell an app dual-write from a rogue script write.

Deliberately NOT guarded: ``dual`` (both stores are live and correct), and DDL
(``CREATE``/``ALTER``/``DROP``) — the teardown phase drops these tables on
purpose.

Escape hatch: set ``MONGO_GUARD_ALLOW_PG=1`` for migration/teardown tooling
that must legitimately touch a cut-over table (e.g. a final archive dump).
"""

from __future__ import annotations

import contextvars
import logging
import os
import re
import sys
from contextlib import contextmanager

logger = logging.getLogger(__name__)

# Set by callers that delete from BOTH stores (scripts/store_delete.py). Their
# PG delete is half of a correct pair, so the `mongo_read` warning below would
# be a false accusation — and a warning that cries wolf on correct code is how
# operators learn to ignore it.
_dual_store_delete: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "pg_guard_dual_store_delete", default=False
)


@contextmanager
def dual_store_delete():
    """Mark the enclosed PG deletes as one half of a dual-store delete."""
    token = _dual_store_delete.set(True)
    try:
        yield
    finally:
        _dual_store_delete.reset(token)

# One pass finds every write target in a statement — including writes hidden in
# a CTE (``WITH x AS (DELETE FROM t ...) SELECT ...``), which an anchored
# ``^INSERT`` match would miss entirely.  Schema qualification is stripped:
# ``public.foo`` and ``"foo"`` both yield ``foo``.
_WRITE_TARGET_RE = re.compile(
    r"\b(INSERT\s+INTO|UPDATE|DELETE\s+FROM|TRUNCATE(?:\s+TABLE)?)\s+"
    r"(?:ONLY\s+)?"
    r'(?:"?[A-Za-z_][A-Za-z0-9_$]*"?\s*\.\s*)?'
    r'"?([A-Za-z_][A-Za-z0-9_$]*)"?',
    re.IGNORECASE,
)

_DESTRUCTIVE_VERBS = ("DELETE", "TRUNCATE")

_guarded_cache: dict[str, str] | None = None


def _allow_override() -> bool:
    return os.getenv("MONGO_GUARD_ALLOW_PG", "").strip().lower() in ("1", "true", "yes")


def _guarded_tables() -> dict[str, str]:
    """Tables at ``mongo`` or ``mongo_read``, by name → mode.

    Backend flags are parsed once per process from ``MONGO_STORE_BACKEND``, so
    this is cached.  When nothing is migrated the map is empty and the guard
    short-circuits to a single dict truth test per statement.
    """
    global _guarded_cache
    if _guarded_cache is None:
        guarded: dict[str, str] = {}
        try:
            from app.db import mongo_store

            # `_BACKENDS` is mongo_store's parsed flag map. Read through
            # `getattr` so this file stays byte-identical in both repos and
            # keeps working if the map is later exposed publicly.
            backends = getattr(mongo_store, "_BACKENDS", None) or {}
            for table, mode in backends.items():
                if mode in ("mongo", "mongo_read"):
                    guarded[table.lower()] = mode
        except Exception as exc:
            # DO NOT cache this. Caching an empty map here would disable the
            # guard for the entire process on one transient import failure —
            # a guard that fails open, which is the exact defect class it
            # exists to catch. Leaving the cache unset costs one retry per
            # statement while broken, and restores protection the moment the
            # import works.
            logger.error(
                "[PG GUARD] could not read backend flags (guard INACTIVE this "
                "call, will retry): %s",
                exc,
            )
            return {}
        _guarded_cache = guarded
    return _guarded_cache


def reset_cache() -> None:
    """Drop the cached flag map (tests flip ``MONGO_STORE_BACKEND`` at runtime)."""
    global _guarded_cache
    _guarded_cache = None


def _is_script_context() -> bool:
    """True when running under a maintenance script rather than the service."""
    if os.getenv("IS_TOOL_PROCESS") == "true":
        return True
    argv0 = sys.argv[0] if sys.argv else ""
    return "/scripts/" in argv0 or "scripts" in argv0.split("/")[:-1]


def check_pg_write(sql: str) -> None:
    """Raise if ``sql`` writes Postgres for a table that has cut over to Mongo.

    Called on every ``PooledCursor.execute``/``executemany``.  Reads (the vast
    majority of traffic) cost one failed regex scan.
    """
    guarded = _guarded_tables()
    if not guarded or not sql:
        return

    for match in _WRITE_TARGET_RE.finditer(sql):
        verb = match.group(1).upper()
        table = match.group(2).lower()
        mode = guarded.get(table)
        if mode is None:
            continue

        if mode == "mongo":
            if _allow_override():
                logger.warning(
                    "[PG GUARD] %s on '%s' allowed by MONGO_GUARD_ALLOW_PG "
                    "(table is at backend 'mongo')",
                    verb,
                    table,
                )
                continue
            raise RuntimeError(
                f"[PG GUARD] refusing {verb} on '{table}': the table is at backend "
                "'mongo', so Postgres is no longer read by anyone and this write "
                "would be silently lost. Write to Mongo via app.db.mongo_store "
                "instead. If this really is migration/teardown tooling, set "
                "MONGO_GUARD_ALLOW_PG=1."
            )

        # mode == "mongo_read": PG is still dual-written, so app writes are
        # correct here.  Only a script-context destructive statement is
        # suspicious — it is the shape that desyncs the live store.
        if (
            verb.startswith(_DESTRUCTIVE_VERBS)
            and _is_script_context()
            and not _dual_store_delete.get()
        ):
            logger.error(
                "[PG GUARD] %s on '%s' hit Postgres only — that table is at "
                "'mongo_read', so readers are served from Mongo and the Mongo "
                "documents are STILL THERE. Delete from both stores "
                "(see scripts/scrub_poisoned_memories.py for the pattern).",
                verb,
                table,
            )
