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
    """Backend mode for a table: 'pg' (default), 'dual', or 'mongo'."""
    return _BACKENDS.get(table, "pg")


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


# ── Connection (own DB, shared client) ─────────────────────────────────────
TRADING_MONGO_DB = getattr(settings, "TRADING_MONGO_DB", None) or os.getenv("TRADING_MONGO_DB", "trading_bot")


def get_doc_db() -> "pymongo.database.Database":
    """The trading document database (default `trading_bot`), on the shared client."""
    return get_mongo_client()[TRADING_MONGO_DB]


_indexes_ready = False


# Natural unique key per migrated collection. `id` keys are partial-unique
# ($type-guarded) because older mirror docs may lack the field or carry null.
_ID_UNIQUE_COLLECTIONS = (
    "execution_errors",
    "cycle_audit_log",
    "llm_audit_logs",
    "agent_traces",
    "agent_tool_telemetry",
    "v3_agent_telemetry",
    "trade_results",
    "ticker_reports",
    "analysis_results",
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
            db[coll].create_index(*args, **kwargs)
        except Exception as e:
            logger.warning("[mongo_store] index on %s failed (non-fatal): %s", coll, e)

    # pipeline_events: read by cycle_id ordered by timestamp; id is the natural PK.
    _try("pipeline_events", "id", unique=True)
    _try("pipeline_events", [("cycle_id", pymongo.ASCENDING), ("timestamp", pymongo.ASCENDING)])

    for coll in _ID_UNIQUE_COLLECTIONS:
        _try(coll, "id", unique=True,
             partialFilterExpression={"id": {"$type": _ID_TYPES}})
    _try("agent_audit_log", "request_id", unique=True,
         partialFilterExpression={"request_id": {"$type": "string"}})
    # NOTE: PG does NOT actually age llm_audit_logs out (AUDIT_LOG_TTL_DAYS
    # only rotates log *files*), so this TTL makes the mirror a 14-day window
    # while PG keeps full history. Before flipping llm_audit_logs to full
    # `mongo`, either drop this TTL or accept losing >14d of history — the
    # dashboard/box_scorecard/strategy_auditor readers use older rows.
    _try("llm_audit_logs", "created_at", expireAfterSeconds=14 * 86400)
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
    _indexes_ready = True


# ── Generic document ops (used by callers behind the backend flags) ────────
def insert_docs(collection: str, docs: list[dict[str, Any]]) -> int:
    """Append documents (idempotent on the natural `id` via ordered=False upsert-on-dup).
    Returns the number the caller handed us (Mongo is best-effort in dual mode)."""
    if not docs:
        return 0
    ensure_indexes()
    db = get_doc_db()
    try:
        db[collection].insert_many(docs, ordered=False)
    except pymongo.errors.BulkWriteError as bwe:
        # Duplicate-key (re-run of the same cycle) is not an error for append-logs.
        non_dupe = [e for e in bwe.details.get("writeErrors", []) if e.get("code") != 11000]
        if non_dupe:
            raise
    return len(docs)


def upsert_doc(collection: str, key: dict[str, Any], doc: dict[str, Any],
               insert_only: bool = False) -> None:
    """Upsert `doc` by the `key` filter (the natural key). $set semantics.
    insert_only=True mirrors PG's ON CONFLICT DO NOTHING: existing docs are
    left untouched (use for immutable, content-addressed rows)."""
    ensure_indexes()
    update = {"$setOnInsert": doc} if insert_only else {"$set": doc}
    get_doc_db()[collection].update_one(key, update, upsert=True)


def bulk_upsert(collection: str, docs: list[dict[str, Any]], key_field: str = "id") -> int:
    """Upsert many docs in ONE round-trip, keyed on `key_field`. Orders of
    magnitude faster than per-doc upsert — use for backfills / big tables.
    Returns the number of docs submitted."""
    if not docs:
        return 0
    ensure_indexes()
    ops = [pymongo.UpdateOne({key_field: d[key_field]}, {"$set": d}, upsert=True) for d in docs]
    get_doc_db()[collection].bulk_write(ops, ordered=False)
    return len(docs)


def find_docs(collection: str, query: dict[str, Any], sort: Optional[list] = None,
              projection: Optional[dict] = None, limit: int = 0) -> list[dict[str, Any]]:
    cur = get_doc_db()[collection].find(query, projection)
    if sort:
        cur = cur.sort(sort)
    if limit:
        cur = cur.limit(limit)
    return list(cur)


def aggregate(collection: str, pipeline: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Run an aggregation pipeline (the Mongo replacement for SQL GROUP BY /
    DISTINCT ON readers)."""
    return list(get_doc_db()[collection].aggregate(pipeline, allowDiskUse=True))


def count_docs(collection: str, query: Optional[dict] = None) -> int:
    return get_doc_db()[collection].count_documents(query or {})


def distinct_values(collection: str, field: str, query: Optional[dict] = None) -> list:
    return get_doc_db()[collection].distinct(field, query or {})


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
