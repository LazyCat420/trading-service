"""
Document-store layer for the Postgres → MongoDB consolidation
(.agents/PLAN-mongodb-consolidation.md).

Most of the trading Postgres is document-shaped (id + scalars + JSONB, written
by idempotent upsert, read by key). This module is the Mongo home for those
tables, with a **per-table backend flag** so each table can be cut over and
rolled back independently:

    pg         → write/read Postgres only (default — behaviour is UNCHANGED)
    dual       → write BOTH; read Postgres (parity-check / soak phase)
    mongo_read → write BOTH; read Mongo (trading-service reads flipped, but PG
                 stays fresh for trading-client, which still reads PG directly)
    mongo      → write/read Mongo only (cutover complete, PG table droppable)

Backends are set via the MONGO_STORE_BACKEND env var, a comma-separated list of
`table:mode` pairs, e.g.  MONGO_STORE_BACKEND="pipeline_events:dual,trade_results:mongo".
Anything unlisted defaults to "pg", so importing this module changes nothing
until a flag is flipped. A Mongo failure in `dual` mode never breaks the
Postgres path — the callers wrap the Mongo side in try/except and log.

Trading documents live in their OWN Mongo database (TRADING_MONGO_DB, default
"trading_bot"), NOT prism's `prism` DB — the Civilization Council collections in
app/db/mongo.py stay where they are.
"""

import logging
import os
from contextlib import contextmanager
from typing import Any, Iterable, Optional

import pymongo

from app.config import settings
from app.db.mongo import get_mongo_client

logger = logging.getLogger(__name__)

# ── Per-table backend flags ────────────────────────────────────────────────
_VALID_MODES = {"pg", "dual", "mongo_read", "mongo"}


def _parse_backends() -> dict[str, str]:
    raw = os.getenv("MONGO_STORE_BACKEND", "") or ""
    out: dict[str, str] = {}
    for pair in raw.split(","):
        pair = pair.strip()
        if not pair or ":" not in pair:
            continue
        table, _, mode = pair.partition(":")
        table, mode = table.strip(), mode.strip().lower()
        if mode in _VALID_MODES:
            out[table] = mode
        else:
            logger.warning("[mongo_store] ignoring bad backend %r (mode must be one of %s)", pair, _VALID_MODES)
    if out:
        logger.info("[mongo_store] table backends: %s", out)
    return out


_BACKENDS = _parse_backends()


def backend_for(table: str) -> str:
    """Backend mode for a table: 'pg' (default), 'dual', 'mongo_read', or 'mongo'."""
    if table in _BACKENDS:
        return _BACKENDS[table]
    if "*" in _BACKENDS:
        return _BACKENDS["*"]
    if "default" in _BACKENDS:
        return _BACKENDS["default"]
    return "pg" 


def writes_mongo(table: str) -> bool:
    """True when writes must ALSO (or ONLY) go to Mongo."""
    return backend_for(table) in ("dual", "mongo_read", "mongo")


def reads_mongo(table: str) -> bool:
    """True when reads must come from Mongo."""
    return backend_for(table) in ("mongo_read", "mongo")


def writes_pg(table: str) -> bool:
    """True when writes must still go to Postgres (everything except full
    cutover — mongo_read keeps PG fresh for direct-PG readers like
    trading-client)."""
    return backend_for(table) in ("pg", "dual", "mongo_read")


def pg_fallback_allowed(table: str) -> bool:
    """May a reader that failed against Mongo fall back to SQL for this table?

    Only while PG is still being written (pg/dual/mongo_read): there the
    fallback is *correct*, just logged for soak visibility. At full `mongo`
    the PG table is stale — a silent fallback would serve old data as if it
    were current, which is worse than failing. Every `try Mongo → except →
    SQL` reader must gate its except-branch on this and raise when False."""
    return writes_pg(table)


def handle_mongo_read_failure(table: str, context: str, exc: Exception) -> None:
    """The one gate every `try Mongo → except → SQL` reader calls FIRST in its
    except-branch, before touching SQL. While PG is fresh (pg/dual/mongo_read)
    it logs and returns — the caller proceeds to its SQL path. At full `mongo`
    it re-raises: PG is stale there, and silently serving old rows as current
    is strictly worse than failing loudly."""
    if pg_fallback_allowed(table):
        logger.warning("%s: mongo read failed, PG fallback: %s", context, exc)
        return
    logger.critical(
        "%s: mongo read failed and PG is STALE for %r (mode=mongo) — refusing the fallback",
        context, table,
    )
    raise exc


# ── Connection (own DB, shared client) ─────────────────────────────────────
TRADING_MONGO_DB = getattr(settings, "TRADING_MONGO_DB", None) or os.getenv("TRADING_MONGO_DB", "trading_bot")


def get_doc_db() -> "pymongo.database.Database":
    """The trading document database (default `trading_bot`), on the shared client."""
    return get_mongo_client()[TRADING_MONGO_DB]


def _coll(table: str):
    """The pymongo Collection for a POSTGRES TABLE NAME.

    Every helper below takes a table name and resolves it here, exactly once.
    That is what lets the physical collections be renamed without touching the
    ~245 call sites that name a table, and what keeps the flags, the write
    guard, the ledger and the specs -- all of which key on the table name -- in
    agreement with the store.

    Never take a collection name from a caller. Mongo creates a collection on
    first write, so a name that bypasses this function does not error; it
    silently starts a second, invisible collection.
    """
    from app.db.collections import collection_for

    return get_doc_db()[collection_for(table)]


_indexes_ready = False


# Natural unique key per migrated collection. `id` keys are partial-unique
# ($type-guarded) because older mirror docs may lack the field or carry null.
_ID_UNIQUE_COLLECTIONS = (
    "tool_usage_stats",
    "execution_errors",
    "cycle_audit_log",
    "llm_audit_logs",
    "agent_traces",
    "agent_tool_telemetry",
    "v3_agent_telemetry",
    "trade_results",
    "ticker_reports",
    "analysis_results",
    "decision_outcomes",
    "v3_system_commands",
    "system_commands",
    "news_articles",
    "reddit_posts",
    "youtube_transcripts",
    "cycle_benchmarks",
    "cycle_ticker_benchmarks",
    "bots",
    "positions",
    "position_lots",
    "lot_closures",
    "orders",
    "trade_fills",
    "portfolio_snapshots",
    "pipeline_state",
    "freshness_gate_config",
)
_ID_TYPES = ["string", "int", "long", "double"]


def ensure_indexes() -> None:
    """Idempotently create indexes for migrated collections. Safe to call often;
    guarded so it only touches Mongo once per process. Per-collection failures
    (e.g. pre-existing duplicates blocking a unique build) are logged and do not
    stop the rest."""
    global _indexes_ready
    if _indexes_ready:
        return
    try:
        db = get_doc_db()
    except Exception as e:
        logger.error("[mongo_store] ensure_indexes failed (non-fatal): %s", e)
        return

    def _try(coll: str, *args, **kwargs) -> None:
        try:
            _coll(coll).create_index(*args, **kwargs)
        except Exception as e:
            logger.warning("[mongo_store] index on %s failed (non-fatal): %s", coll, e)

    def _drop_ttl(coll: str, field: str) -> None:
        # Drop a TTL variant of a single-field index so a plain one can replace
        # it (create_index on the same key with different options errors, it
        # does not modify).
        try:
            for info in _coll(coll).list_indexes():
                if "expireAfterSeconds" in info and list(info["key"]) == [field]:
                    _coll(coll).drop_index(info["name"])
                    logger.info("[mongo_store] dropped TTL index %s on %s", info["name"], coll)
        except Exception as e:
            logger.warning("[mongo_store] TTL drop on %s failed (non-fatal): %s", coll, e)

    # pipeline_events: read by cycle_id ordered by timestamp; id is the natural PK.
    _try("pipeline_events", "id", unique=True)
    _try("pipeline_events", [("cycle_id", pymongo.ASCENDING), ("timestamp", pymongo.ASCENDING)])

    for coll in _ID_UNIQUE_COLLECTIONS:
        _try(coll, "id", unique=True,
             partialFilterExpression={"id": {"$type": _ID_TYPES}})
    _try("agent_audit_log", "request_id", unique=True,
         partialFilterExpression={"request_id": {"$type": "string"}})
    # NO TTL on llm_audit_logs: PG keeps full history (AUDIT_LOG_TTL_DAYS only
    # rotates log *files*), and the dashboard/box_scorecard/strategy_auditor
    # readers use rows older than any short window. A 14-day TTL lived here
    # until 2026-08-16 — with the table at mongo_read it silently truncated
    # what those readers saw; the expired rows were re-backfilled from PG when
    # it was removed. The index itself stays (created_at is a read key), and
    # ensure_indexes drops the TTL variant if it finds one so a stale deploy
    # cannot resurrect it.
    _drop_ttl("llm_audit_logs", "created_at")
    _try("llm_audit_logs", "created_at")
    # trade_results is written and read by (cycle_id, ticker) — same as
    # ticker_reports/analysis_results below.
    _try("trade_results", [("cycle_id", pymongo.ASCENDING), ("ticker", pymongo.ASCENDING)])
    _try("context_blobs", "context_hash", unique=True,
         partialFilterExpression={"context_hash": {"$type": "string"}})
    # Read-path keys used by the report/replay UIs after cutover.
    _try("ticker_reports", [("cycle_id", pymongo.ASCENDING), ("ticker", pymongo.ASCENDING)])
    _try("analysis_results", [("cycle_id", pymongo.ASCENDING), ("ticker", pymongo.ASCENDING)])
    _try("agent_tool_telemetry", [("agent_name", pymongo.ASCENDING), ("created_at", pymongo.DESCENDING)])
    _try("v3_agent_telemetry", [("agent_name", pymongo.ASCENDING), ("created_at", pymongo.DESCENDING)])
    _try("tool_usage_stats", [("tool_name", pymongo.ASCENDING), ("called_at", pymongo.DESCENDING)])
    _try("tool_usage_stats", "called_at")
    _try("tool_usage_stats", [("agent_name", pymongo.ASCENDING), ("cycle_id", pymongo.ASCENDING)])
    _try("agent_tool_optimization", [("agent_name", pymongo.ASCENDING), ("tool_name", pymongo.ASCENDING)], unique=True)
    _try("decision_outcomes", "resolved_at")
    _try("decision_outcomes", "created_at")
    _try("decision_outcomes", [("cycle_id", pymongo.ASCENDING), ("ticker", pymongo.ASCENDING)])
    _indexes_ready = True


# ── Generic document ops (used by callers behind the backend flags) ────────
def insert_docs(collection: str, docs: list[dict[str, Any]]) -> int:
    """Append documents (idempotent on the natural `id`, ordered=False).

    Returns the number ACTUALLY inserted, which is not always the number handed
    in. This used to `return len(docs)` unconditionally, so a duplicate-key
    rejection -- swallowed just below as a legitimate re-run -- was reported to
    the caller as a successful write of every document. A mirror that
    under-writes while reporting success is indistinguishable from one that
    works, and the only way to notice was a row-count comparison against
    Postgres days later.
    """
    if not docs:
        return 0
    ensure_indexes()
    db = get_doc_db()
    try:
        res = _coll(collection).insert_many(docs, ordered=False)
        return len(res.inserted_ids)
    except pymongo.errors.BulkWriteError as bwe:
        # Duplicate-key (re-run of the same cycle) is not an error for append-logs.
        write_errors = bwe.details.get("writeErrors", [])
        non_dupe = [e for e in write_errors if e.get("code") != 11000]
        if non_dupe:
            raise
        # nInserted is what the server actually committed; the rest were dups.
        return int(bwe.details.get("nInserted", 0))


def upsert_doc(collection: str, key: dict[str, Any], doc: dict[str, Any],
               insert_only: bool = False) -> None:
    """Upsert `doc` by the `key` filter (the natural key). $set semantics.
    insert_only=True mirrors PG's ON CONFLICT DO NOTHING: existing docs are
    left untouched (use for immutable, content-addressed rows)."""
    ensure_indexes()
    update = {"$setOnInsert": doc} if insert_only else {"$set": doc}
    _coll(collection).update_one(key, update, upsert=True)


def bulk_upsert(collection: str, docs: list[dict[str, Any]], key_field: str = "id") -> int:
    """Upsert many docs in ONE round-trip, keyed on `key_field`. Orders of
    magnitude faster than per-doc upsert — use for backfills / big tables.
    Returns the number of docs submitted."""
    if not docs:
        return 0
    ensure_indexes()
    ops = [pymongo.UpdateOne({key_field: d[key_field]}, {"$set": d}, upsert=True) for d in docs]
    _coll(collection).bulk_write(ops, ordered=False)
    return len(docs)


def find_docs(collection: str, query: dict[str, Any], sort: Optional[list] = None,
              projection: Optional[dict] = None, limit: int = 0) -> list[dict[str, Any]]:
    cur = _coll(collection).find(query, projection)
    if sort:
        cur = cur.sort(sort)
    if limit:
        cur = cur.limit(limit)
    return list(cur)


def aggregate(collection: str, pipeline: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Run an aggregation pipeline (the Mongo replacement for SQL GROUP BY /
    DISTINCT ON readers)."""
    return list(_coll(collection).aggregate(pipeline, allowDiskUse=True))


def count_docs(collection: str, query: Optional[dict] = None) -> int:
    return _coll(collection).count_documents(query or {})


def distinct_values(collection: str, field: str, query: Optional[dict] = None) -> list:
    return _coll(collection).distinct(field, query or {})


def delete_docs(collection: str, query: dict[str, Any],
                session: Optional[Any] = None) -> int:
    """Delete every doc matching `query`; returns the deleted count. The Mongo
    analogue of `DELETE FROM t WHERE ...` for tables whose flag says Mongo is
    (also) authoritative — callers still gate on writes_mongo()."""
    if not query:
        raise ValueError("delete_docs with an empty query would empty the collection; "
                         "pass an explicit filter (or use {'_id': {'$exists': True}} deliberately)")
    res = _coll(collection).delete_many(query, session=session)
    return res.deleted_count


def update_docs(collection: str, query: dict[str, Any], update: dict[str, Any],
                upsert: bool = False, session: Optional[Any] = None) -> int:
    """update_many with `$set`-style semantics; returns the modified count.
    `update` may be a plain doc (wrapped in $set) or already contain
    operators ($set/$inc/...)."""
    if not any(k.startswith("$") for k in update):
        update = {"$set": update}
    res = _coll(collection).update_many(query, update, upsert=upsert, session=session)
    return res.modified_count


def find_one_and_update(collection: str, query: dict[str, Any], update: dict[str, Any],
                        sort: Optional[list] = None, return_after: bool = True,
                        upsert: bool = False, session: Optional[Any] = None) -> Optional[dict[str, Any]]:
    """Atomically claim-and-mutate ONE doc — the Mongo equivalent of
    `SELECT ... FOR UPDATE SKIP LOCKED` + UPDATE for queue tables
    (v3_system_commands / system_commands claims): a doc matched by `query`
    is mutated in the same atomic step, so two concurrent claimants can never
    both see it in the claimable state. Returns the doc (post-update by
    default) or None when nothing matched."""
    if not any(k.startswith("$") for k in update):
        update = {"$set": update}
    return _coll(collection).find_one_and_update(
        query, update, sort=sort, upsert=upsert,
        return_document=pymongo.ReturnDocument.AFTER if return_after else pymongo.ReturnDocument.BEFORE,
        session=session,
    )


@contextmanager
def with_txn():
    """Multi-document transaction on the rs0 replica set: yields a session to
    pass as `session=` to the helpers above (and to raw collection ops). On a
    clean exit the transaction commits; on an exception it aborts and the
    exception propagates. This is the atomicity primitive for the money-ledger
    and whiteboard phases — the same contract as PooledCursor.transaction()."""
    client = get_mongo_client()
    with client.start_session() as session:
        with session.start_transaction():
            yield session


def dec128(value: Any) -> "Any":
    """Money → bson.Decimal128 via str() (never through float arithmetic —
    Decimal128(str(x)) preserves the printed value, which is the best a float
    source can offer). The ledger phase maps every money field through this;
    do NOT store money as float in new collections."""
    from bson import Decimal128
    from decimal import Decimal

    if isinstance(value, Decimal128):
        return value
    if isinstance(value, Decimal):
        return Decimal128(value)
    return Decimal128(str(value))


def mirror_pipeline_event(record: dict[str, Any]) -> None:
    """Best-effort dual-write of ONE pipeline_events record (the rare error-path
    inserts in result_saver / battle_royale). No-op unless pipeline_events is
    dual/mongo. Never raises — a Mongo failure must not break the PG error path."""
    if not writes_mongo("pipeline_events"):
        return
    try:
        insert_docs("pipeline_events", [dict(record)])
    except Exception as e:
        logger.error("[mongo_store] mirror_pipeline_event failed (non-fatal): %s", e)


# ── pipeline_events convenience (matches the PG read shape exactly) ─────────
def read_pipeline_events(cycle_id: str) -> list[dict[str, Any]]:
    """Return a cycle's events in the SAME dict shape the Postgres read builds
    (keys: ts isoformat str, phase, step, detail, status, data dict, elapsed_ms),
    so get_state() is agnostic to which store served them."""
    out: list[dict[str, Any]] = []
    for d in find_docs("pipeline_events", {"cycle_id": cycle_id},
                        sort=[("timestamp", pymongo.ASCENDING)]):
        ts_val = d.get("timestamp")
        out.append({
            "ts": ts_val.isoformat() if hasattr(ts_val, "isoformat") else (str(ts_val) if ts_val else None),
            "phase": d.get("phase"),
            "step": d.get("step"),
            "detail": d.get("detail"),
            "status": d.get("status"),
            "data": d.get("data") or {},
            "elapsed_ms": d.get("elapsed_ms") or 0,
        })
    return out
