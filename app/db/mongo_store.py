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
from collections.abc import Sequence
from typing import Any, Iterable, Optional

import pymongo

from app.config import settings
from app.db import date_fields
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


def ensure_indexes(session: Optional[Any] = None) -> None:
    """Idempotently create indexes for migrated collections. Safe to call often;
    guarded so it only touches Mongo once per process. Per-collection failures
    (e.g. pre-existing duplicates blocking a unique build) are logged and do not
    stop the rest.

    NEVER RUNS WHILE A TRANSACTION IS OPEN
    --------------------------------------
    `create_index` on a collection that does not exist yet CREATES it, and a
    catalog write aborts any transaction in flight against that namespace:

        Collection namespace 'trading_bot.lot_closures' is already in use.
        ... Transaction with { txnNumber: N } has been aborted. (code 251)

    Every write helper below calls this first, and `paper_trader.sell()` calls
    those helpers from inside `with_txn()`. So on a store where the collections
    do not exist yet, the FIRST transactional SELL of a process aborts — the
    money path failing on a fresh deployment, in the one code path that is
    wrapped in a transaction precisely because it must not half-apply.

    Passing the active session makes the helper SKIP the DDL rather than
    perform it: the write proceeds (Mongo creates the collection implicitly,
    which a transaction is allowed to do), `_indexes_ready` stays False, and
    the next call outside a transaction builds the indexes for real. Deferring
    is safe; running it here is not.

    Why this survived so long: `_indexes_ready` is a process global, so in a
    long-lived container the first write is usually NOT in a transaction and
    the flag is already set by the time a SELL runs. And both pure-Mongo E2E
    tests stubbed this function out entirely
    (`monkeypatch.setattr(mongo_store, "ensure_indexes", lambda: None)`),
    which disabled the only failure mode it has. See
    tests/unit/test_index_creation_inside_a_transaction.py.
    """
    global _indexes_ready
    if _indexes_ready:
        return
    if session is not None and getattr(session, "in_transaction", False):
        logger.debug(
            "[mongo_store] deferring index creation: a transaction is open "
            "(creating a collection here would abort it)"
        )
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
    # price_history peer-session probe (`_trading_day_age`): its filter is a
    # `$not/$regex` on ticker + a range on date, which the (ticker, date,
    # source) natural key cannot serve — measured 2026-08-25 as a 24-28s FULL
    # SCAN of 15.77M docs on EVERY get_market_data call (p50 20.9s, 24% of
    # calls aborted at the 30s bridge deadline). A plain date index turns the
    # same command into 0.01-0.04s. Declared here so a reseed/rebuild gets it
    # back — an index created only by hand dies with the next backfill.
    _try("price_history", [("date", pymongo.ASCENDING)], name="date_1")
    _try("pipeline_events", "id", unique=True)
    _try("pipeline_events", [("cycle_id", pymongo.ASCENDING), ("timestamp", pymongo.ASCENDING)])

    for coll in _ID_UNIQUE_COLLECTIONS:
        _try(coll, "id", unique=True,
             partialFilterExpression={"id": {"$type": _ID_TYPES}})
        # ...and a PLAIN index on the same field, which is the one the planner
        # can actually use. A partial index is only considered when the query
        # predicate is provably a subset of its filter, and `{"id": <value>}`
        # is not a subset of `{"id": {"$type": [...]}}` — MongoDB does not
        # infer type membership from a literal. So every keyed read and every
        # upsert filter on these collections was a COLLSCAN: measured
        # 2026-08-19, `{"id": "err_..."}` against execution_errors (173,005
        # docs) planned COLLSCAN, and the backfill crawled at 22-83 rows/s
        # against 5,900-7,100 for the collections that happened to carry a
        # plain index beside the partial one.
        #
        # A separate NAME is required: create_index with the same key and
        # different options is an error, not a modification. `agent_audit_log`
        # has carried exactly this pair (`request_id_plain_1`) since someone
        # hit the problem there and fixed the one collection in front of them.
        _try(coll, "id", name="id_plain_1")
    _try("agent_audit_log", "request_id", unique=True,
         partialFilterExpression={"request_id": {"$type": "string"}})
    _try("agent_audit_log", "request_id", name="request_id_plain_1")
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
    _try("context_blobs", "context_hash", name="context_hash_plain_1")
    # Read-path keys used by the report/replay UIs after cutover.
    _try("ticker_reports", [("cycle_id", pymongo.ASCENDING), ("ticker", pymongo.ASCENDING)])
    _try("analysis_results", [("cycle_id", pymongo.ASCENDING), ("ticker", pymongo.ASCENDING)])
    _try("agent_tool_telemetry", [("agent_name", pymongo.ASCENDING), ("created_at", pymongo.DESCENDING)])
    _try("v3_agent_telemetry", [("agent_name", pymongo.ASCENDING), ("created_at", pymongo.DESCENDING)])
    _try("tool_usage_stats", [("tool_name", pymongo.ASCENDING), ("called_at", pymongo.DESCENDING)])
    _try("tool_usage_stats", "called_at")

    # technicals: (ticker, date DESC) — the screener's latest-per-ticker read.
    #
    # This collection had NO index but `_id` until 2026-08-19, because it is
    # the one collection nothing seeds through the backfill: it is rebuilt in
    # place by scripts/recompute_technicals.py, which writes straight through
    # this module, and `ensure_key_index()` (which creates every other
    # collection's natural key) only runs on the backfill path.
    #
    # The cost was not theoretical. `_latest_per_ticker("technicals", "date")`
    # in trading-client's screener_snapshot sorts the WHOLE collection by
    # (ticker, date) — an in-memory sort with no index. On 2026-08-19 that made
    # the screener take 53.2s and 500 behind the proxy's timeout; with this
    # index the same aggregation is 0.11s and the endpoint 0.98s. It would also
    # have got worse on its own: the recompute is refilling this collection
    # toward ~1.37M documents, where an unindexed sort passes Mongo's 100MB
    # in-memory sort limit and starts failing outright rather than merely
    # being slow.
    _try("technicals", [("ticker", pymongo.ASCENDING), ("date", pymongo.DESCENDING)],
         name="ticker_date")
    _try("tool_usage_stats", [("agent_name", pymongo.ASCENDING), ("cycle_id", pymongo.ASCENDING)])
    _try("agent_tool_optimization", [("agent_name", pymongo.ASCENDING), ("tool_name", pymongo.ASCENDING)], unique=True)
    _try("decision_outcomes", "resolved_at")
    _try("decision_outcomes", "created_at")
    _try("decision_outcomes", [("cycle_id", pymongo.ASCENDING), ("ticker", pymongo.ASCENDING)])
    _indexes_ready = True


# ── Generic document ops (used by callers behind the backend flags) ────────
def insert_docs(collection: str, docs: list[dict[str, Any]],
                session: Optional[Any] = None) -> int:
    """Append documents (idempotent on the natural `id`, ordered=False).

    Returns the number ACTUALLY inserted, which is not always the number handed
    in.
    """
    if not docs:
        return 0
    ensure_indexes(session)
    docs = date_fields.coerce_docs(collection, docs)
    try:
        res = _coll(collection).insert_many(docs, ordered=False, session=session)
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
               insert_only: bool = False, session: Optional[Any] = None) -> None:
    """Upsert `doc` by the `key` filter (the natural key). $set semantics.
    insert_only=True mirrors PG's ON CONFLICT DO NOTHING: existing docs are
    left untouched (use for immutable, content-addressed rows)."""
    ensure_indexes(session)
    key = date_fields.coerce_filter(collection, key)
    doc = date_fields.coerce_doc(collection, doc)
    update = {"$setOnInsert": doc} if insert_only else {"$set": doc}
    _coll(collection).update_one(key, update, upsert=True, session=session)


def bulk_upsert(collection: str, docs: list[dict[str, Any]],
                key_field: "str | Sequence[str]" = "id") -> int:
    """Upsert many docs in ONE round-trip, keyed on `key_field`. Orders of
    magnitude faster than per-doc upsert — use for backfills / big tables.
    Returns the number of docs submitted.

    `key_field` takes a sequence for a COMPOSITE natural key. Several tables
    the migration touches are keyed on a pair — `technicals` on
    (ticker, date), `price_history` on (ticker, date, source) — and a
    single-field version pushed those callers back to per-document upserts:
    technical_processor was issuing 287 round-trips to write one ticker's
    indicators, which is the 22.6s/ticker shape its own test exists to pin.
    """
    if not docs:
        return 0
    ensure_indexes()
    docs = date_fields.coerce_docs(collection, docs)
    keys = [key_field] if isinstance(key_field, str) else list(key_field)
    ops = [
        pymongo.UpdateOne({k: d[k] for k in keys}, {"$set": d}, upsert=True)
        for d in docs
    ]
    _coll(collection).bulk_write(ops, ordered=False)
    return len(docs)


def find_docs(collection: str, query: dict[str, Any], sort: Optional[list] = None,
              projection: Optional[dict] = None, limit: int = 0,
              session: Optional[Any] = None) -> list[dict[str, Any]]:
    query = date_fields.coerce_filter(collection, query)
    cur = _coll(collection).find(query, projection, session=session)
    if sort:
        cur = cur.sort(sort)
    if limit:
        cur = cur.limit(limit)
    return list(cur)


def aggregate(collection: str, pipeline: list[dict[str, Any]],
              session: Optional[Any] = None) -> list[dict[str, Any]]:
    """Run an aggregation pipeline (the Mongo replacement for SQL GROUP BY /
    DISTINCT ON readers)."""
    pipeline = date_fields.coerce_pipeline(collection, pipeline)
    return list(_coll(collection).aggregate(pipeline, allowDiskUse=True, session=session))


def count_docs(collection: str, query: Optional[dict] = None) -> int:
    return _coll(collection).count_documents(date_fields.coerce_filter(collection, query or {}))


def distinct_values(collection: str, field: str, query: Optional[dict] = None) -> list:
    return _coll(collection).distinct(field, date_fields.coerce_filter(collection, query or {}))


def delete_docs(collection: str, query: dict[str, Any],
                session: Optional[Any] = None) -> int:
    """Delete every doc matching `query`; returns the deleted count. The Mongo
    analogue of `DELETE FROM t WHERE ...` for tables whose flag says Mongo is
    (also) authoritative — callers still gate on writes_mongo()."""
    if not query:
        raise ValueError("delete_docs with an empty query would empty the collection; "
                         "pass an explicit filter (or use {'_id': {'$exists': True}} deliberately)")
    res = _coll(collection).delete_many(
        date_fields.coerce_filter(collection, query), session=session)
    return res.deleted_count


def update_docs(collection: str, query: dict[str, Any], update: dict[str, Any],
                upsert: bool = False, session: Optional[Any] = None) -> int:
    """update_many with `$set`-style semantics; returns the modified count.
    `update` may be a plain doc (wrapped in $set) or already contain
    operators ($set/$inc/...)."""
    if not any(k.startswith("$") for k in update):
        update = {"$set": update}
    query = date_fields.coerce_filter(collection, query)
    update = date_fields.coerce_update(collection, update)
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
    query = date_fields.coerce_filter(collection, query)
    update = date_fields.coerce_update(collection, update)
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
