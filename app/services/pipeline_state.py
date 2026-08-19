import json
import logging
from datetime import datetime, timezone
from app.db import connection
from app.db.mongo_store import handle_mongo_read_failure

logger = logging.getLogger(__name__)

def _stringify_timestamp(value):
    if not value: return None
    if isinstance(value, str): return value
    if hasattr(value, "tzinfo") and value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.isoformat() if hasattr(value, "isoformat") else str(value)

class PipelineStateDB:
    SINGLETON_ID = "current"

    @classmethod
    def save_state(cls, state: dict):
        try:
            from app.db import mongo_store
            now_utc = datetime.now(timezone.utc)
            if mongo_store.writes_mongo("pipeline_state"):
                mongo_store.upsert_doc(
                    "pipeline_state",
                    {"singleton_id": cls.SINGLETON_ID},
                    {
                        "singleton_id": cls.SINGLETON_ID,
                        "status": state.get("status", "idle"),
                        "cycle_id": state.get("cycle_id"),
                        "started_at": state.get("started_at"),
                        "finished_at": state.get("finished_at"),
                        "tickers": state.get("tickers", []),
                        "progress": state.get("progress", ""),
                        "error": state.get("error"),
                        "phase": state.get("phase", ""),
                        "agent_locale": state.get("agent_locale", "default"),
                        "collect_flag": state.get("collect_flag"),
                        "analyze_flag": state.get("analyze_flag"),
                        "trade_flag": state.get("trade_flag"),
                        "requested_pipeline_version": state.get("requested_pipeline_version"),
                        "effective_pipeline_version": state.get("effective_pipeline_version"),
                        "execution_mode": state.get("execution_mode"),
                        "updated_at": now_utc,
                    }
                )
            if mongo_store.writes_pg("pipeline_state"):
                with connection.get_db() as db:
                    db.execute(
                        """
                        INSERT INTO pipeline_state (
                            singleton_id, status, cycle_id, started_at, finished_at,
                            tickers, progress, error, phase, agent_locale,
                            collect_flag, analyze_flag, trade_flag, requested_pipeline_version,
                            effective_pipeline_version, execution_mode,
                            updated_at
                        ) VALUES (
                            %s, %s, %s, %s, %s,
                            %s::jsonb, %s, %s, %s, %s,
                            %s, %s, %s, %s,
                            %s, %s,
                            CURRENT_TIMESTAMP
                        )
                    ON CONFLICT (singleton_id) DO UPDATE SET
                        status = EXCLUDED.status,
                        cycle_id = EXCLUDED.cycle_id,
                        started_at = EXCLUDED.started_at,
                        finished_at = EXCLUDED.finished_at,
                        tickers = EXCLUDED.tickers,
                        progress = EXCLUDED.progress,
                        error = EXCLUDED.error,
                        phase = EXCLUDED.phase,
                        agent_locale = EXCLUDED.agent_locale,
                        collect_flag = EXCLUDED.collect_flag,
                        analyze_flag = EXCLUDED.analyze_flag,
                        trade_flag = EXCLUDED.trade_flag,
                        requested_pipeline_version = EXCLUDED.requested_pipeline_version,
                        effective_pipeline_version = EXCLUDED.effective_pipeline_version,
                        execution_mode = EXCLUDED.execution_mode,
                        updated_at = CURRENT_TIMESTAMP
                    """,
                        [
                            cls.SINGLETON_ID,
                            state.get("status", "idle"),
                            state.get("cycle_id"),
                            state.get("started_at"),
                            state.get("finished_at"),
                            json.dumps(state.get("tickers", [])),
                            state.get("progress", ""),
                            state.get("error"),
                            state.get("phase", ""),
                            state.get("agent_locale", "default"),
                            state.get("collect_flag"),
                            state.get("analyze_flag"),
                            state.get("trade_flag"),
                            state.get("requested_pipeline_version"),
                            state.get("effective_pipeline_version"),
                            state.get("execution_mode"),
                        ],
                    )
        except Exception as e:
            logger.error("[PipelineStateDB] Failed to save DB core state: %s", e)

    @classmethod
    def append_events(cls, cycle_id: str, events: list[dict]):
        if not cycle_id or not events:
            return
        import uuid
        # Build records ONCE so the Postgres row and the Mongo document share an id.
        records = [
            {
                "id": f"evt_{uuid.uuid4().hex[:8]}",
                "cycle_id": cycle_id,
                "timestamp": e.get("ts") or datetime.now(timezone.utc),
                "phase": e.get("phase"),
                "step": e.get("step"),
                "detail": e.get("detail"),
                "status": e.get("status", "ok"),
                "data": e.get("data", {}) or {},
                "elapsed_ms": e.get("elapsed_ms", 0),
            }
            for e in events
        ]
        try:
            with connection.get_db() as db:
                rows = [
                    (r["id"], r["cycle_id"], r["timestamp"], r["phase"], r["step"],
                     r["detail"], r["status"], json.dumps(r["data"]), r["elapsed_ms"])
                    for r in records
                ]
                db.executemany(
                    """
                    INSERT INTO pipeline_events
                    (id, cycle_id, timestamp, phase, step, detail, status, data_json, elapsed_ms)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s)
                    """,
                    rows,
                )
        except Exception as e:
            logger.error("[PipelineStateDB] Failed to append SQL events: %s", e)

        # Dual-write to Mongo while pipeline_events is being migrated
        # (MONGO_STORE_BACKEND=pipeline_events:dual|mongo). Best-effort — a Mongo
        # failure must NEVER break the Postgres append above.
        try:
            # Never re-import datetime inside this function: a local import
            # makes the name local to the WHOLE scope, and the ts-less
            # fallback above then raises before any row is written.
            from app.db import mongo_store
            if mongo_store.writes_mongo("pipeline_events"):
                docs = []
                for r in records:
                    d = dict(r)
                    # PG stores ISO strings; Mongo must get real dates — string
                    # timestamps sort before BSON dates and break the
                    # read_pipeline_events sort for cycles straddling both.
                    ts = d.get("timestamp")
                    if isinstance(ts, str):
                        try:
                            d["timestamp"] = datetime.fromisoformat(ts)
                        except ValueError:
                            pass
                    docs.append(d)
                mongo_store.insert_docs("pipeline_events", docs)
        except Exception as me:
            logger.error("[PipelineStateDB] Mongo dual-write failed (non-fatal): %s", me)

    # ── Per-cycle readers ─────────────────────────────────────────────────
    #
    # Extracted out of get_state so the SAME read can serve a cycle that is no
    # longer the singleton. `/run-cycle/status` only ever carried the ONE cycle
    # named in `pipeline_state`, which is why the dashboard's pipeline grid lost
    # every asset row the moment the next cycle started. `pipeline_events` is
    # append-only and nothing was ever deleted — there was simply no reader that
    # took a cycle_id.
    #
    # These are the ONE definition of the wire shape. The cycle-replay endpoint
    # delegates here rather than re-deriving it: a second copy that drifted by a
    # single key would not raise, it would render an empty grid, which looks
    # exactly like a cycle that processed nothing.

    @classmethod
    def get_cycle_events(cls, cycle_id: str, limit: int | None = None) -> list[dict]:
        """A cycle's events, oldest-first, as {ts, phase, step, detail, status,
        data, elapsed_ms}. Identical shape from either store."""
        if not cycle_id:
            return []
        from app.db import mongo_store
        # After cutover (MONGO_STORE_BACKEND=pipeline_events:mongo) read events
        # from Mongo; identical dict shape either way.
        if mongo_store.reads_mongo("pipeline_events"):
            try:
                events = mongo_store.read_pipeline_events(cycle_id)
                return events[:limit] if limit else events
            except Exception as mev_e:
                logger.error("[PipelineStateDB] Mongo events read failed: %s", mev_e)
                return []
        try:
            # A fresh connection: the `db` from the state read in get_state is
            # out of scope here whenever the state came from Mongo, and closed
            # even when it did not.
            sql = (
                "SELECT timestamp, phase, step, detail, status, data_json, elapsed_ms "
                "FROM pipeline_events WHERE cycle_id = %s ORDER BY timestamp ASC"
            )
            params: list = [cycle_id]
            if limit:
                sql += " LIMIT %s"
                params.append(limit)
            with connection.get_db() as ev_db:
                ev_rows = ev_db.execute(sql, params).fetchall()
            events = []
            for erow in ev_rows:
                ts_val = erow[0]
                ts_str = ts_val.isoformat() if hasattr(ts_val, 'isoformat') else str(ts_val) if ts_val else None
                data_parsed = {}
                if erow[5]:
                    try:
                        data_parsed = json.loads(erow[5]) if isinstance(erow[5], str) else erow[5]
                    except Exception:
                        pass
                events.append({
                    "ts": ts_str,
                    "phase": erow[1],
                    "step": erow[2],
                    "detail": erow[3],
                    "status": erow[4],
                    "data": data_parsed,
                    "elapsed_ms": erow[6] or 0,
                })
            return events
        except Exception as ev_e:
            logger.error("[PipelineStateDB] Failed to fetch events for %s: %s", cycle_id, ev_e)
            return []

    @classmethod
    def get_cycle_results(cls, cycle_id: str) -> list[dict]:
        """A cycle's analysis results (the dicts carrying action / confidence /
        trade_executed).

        The pipeline grid's OUTPUT column needs these: the only phase='trading'
        event carries {kind, ticker, side, qty, price} and NO action, so a grid
        row without a matching result falls through to its `|| 'HOLD'` and
        `|| 0` defaults and renders a confident-looking "HOLD 0%" for a decision
        nobody made. Any consumer that shows a past cycle must read these too.
        """
        if not cycle_id:
            return []
        from app.db import mongo_store
        try:
            ar_rows = None
            if mongo_store.reads_mongo("analysis_results"):
                try:
                    ar_rows = [
                        (doc.get("ticker"), doc.get("result_json"))
                        for doc in mongo_store.find_docs(
                            "analysis_results", {"cycle_id": cycle_id},
                            projection={"_id": 0, "ticker": 1, "result_json": 1},
                        )
                    ]
                except Exception as me:
                    handle_mongo_read_failure("analysis_results", "[PipelineStateDB] mongo results read", me)
            if ar_rows is None:
                with connection.get_db() as ar_db:
                    ar_rows = ar_db.execute(
                        "SELECT ticker, result_json FROM analysis_results WHERE cycle_id = %s",
                        [cycle_id],
                    ).fetchall()
            results = []
            for ar in ar_rows:
                try:
                    res = ar[1] if isinstance(ar[1], dict) else json.loads(ar[1])
                    if "ticker" not in res:
                        res["ticker"] = ar[0]
                    results.append(res)
                except Exception:
                    pass
            return results
        except Exception as ar_e:
            logger.error("[PipelineStateDB] Failed to fetch results for %s: %s", cycle_id, ar_e)
            return []

    @classmethod
    def get_state(cls, summary_only: bool = False) -> dict:
        try:
            from app.db import mongo_store
            d = None
            if mongo_store.reads_mongo("pipeline_state"):
                try:
                    docs = mongo_store.find_docs(
                        "pipeline_state", {"singleton_id": cls.SINGLETON_ID}, limit=1
                    )
                    if docs:
                        d = dict(docs[0])
                        d.pop("_id", None)
                        d.pop("singleton_id", None)
                except Exception as me:
                    handle_mongo_read_failure(
                        "pipeline_state", "[PipelineStateDB] mongo state read", me
                    )

            # Fall back to Postgres whenever it is still being written, which
            # covers both a Mongo read error and a Mongo copy that has no
            # document yet. Guarding this on `not reads_mongo(...)` instead
            # would mean a stale or missing Mongo singleton silently reports
            # the pipeline as idle -- and an idle reading is what the deploy
            # interlock treats as "safe to restart the container".
            # At mode `mongo` writes_pg() is False, so a missing document
            # surfaces as an empty state rather than a stale one.
            if d is None and mongo_store.writes_pg("pipeline_state"):
                with connection.get_db() as db:
                    row = db.execute("SELECT * FROM pipeline_state WHERE singleton_id = %s", [cls.SINGLETON_ID]).fetchone()
                    if row:
                        cols = [desc[0] for desc in db.description]
                        d = dict(zip(cols, row))
                        if isinstance(d.get("tickers"), str):
                            d["tickers"] = json.loads(d["tickers"])
                        d.pop("singleton_id", None)

            if d:

                    # Enrich with events and results if summary_only is False and cycle_id exists
                    cycle_id = d.get("cycle_id")
                    if cycle_id and not summary_only:
                        d["events"] = cls.get_cycle_events(cycle_id)
                        d["results"] = cls.get_cycle_results(cycle_id)
                    else:
                        d["events"] = []
                        d["results"] = []

                    return d
        except Exception as e:
            logger.error("[PipelineStateDB] Failed to get state: %s", e)
        return cls.default_state()

    @classmethod
    def default_state(cls) -> dict:
        return {
            "status": "idle",
            "cycle_id": None,
            "started_at": None,
            "finished_at": None,
            "tickers": [],
            "progress": "",
            "error": None,
            "phase": "",
        }
