import json
import logging
from datetime import datetime, timezone
from app.db import connection

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
            with connection.get_db() as db:
                db.execute(
                    """
                    INSERT INTO pipeline_state (
                        singleton_id, status, cycle_id, started_at, finished_at,
                        tickers, progress, error, phase, agent_locale,
                        collect_flag, analyze_flag, trade_flag, requested_pipeline_version,
                        updated_at
                    ) VALUES (
                        %s, %s, %s, %s, %s,
                        %s::jsonb, %s, %s, %s, %s,
                        %s, %s, %s, %s,
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
            from app.db import mongo_store
            if mongo_store.writes_mongo("pipeline_events"):
                mongo_store.insert_docs("pipeline_events", [dict(r) for r in records])
        except Exception as me:
            logger.error("[PipelineStateDB] Mongo dual-write failed (non-fatal): %s", me)

    @classmethod
    def get_state(cls, summary_only: bool = False) -> dict:
        try:
            with connection.get_db() as db:
                row = db.execute("SELECT * FROM pipeline_state WHERE singleton_id = %s", [cls.SINGLETON_ID]).fetchone()
                if row:
                    cols = [desc[0] for desc in db.description]
                    d = dict(zip(cols, row))
                    if isinstance(d.get("tickers"), str):
                        d["tickers"] = json.loads(d["tickers"])
                    d.pop("singleton_id", None)

                    # Enrich with events and results if summary_only is False and cycle_id exists
                    cycle_id = d.get("cycle_id")
                    if cycle_id and not summary_only:
                        # After cutover (MONGO_STORE_BACKEND=pipeline_events:mongo)
                        # read events from Mongo; identical dict shape either way.
                        from app.db import mongo_store
                        _events_from_mongo = mongo_store.reads_mongo("pipeline_events")
                        if _events_from_mongo:
                            try:
                                d["events"] = mongo_store.read_pipeline_events(cycle_id)
                            except Exception as mev_e:
                                logger.error("[PipelineStateDB] Mongo events read failed: %s", mev_e)
                                d["events"] = []
                        else:
                            try:
                                ev_rows = db.execute(
                                    "SELECT timestamp, phase, step, detail, status, data_json, elapsed_ms "
                                    "FROM pipeline_events WHERE cycle_id = %s ORDER BY timestamp ASC",
                                    [cycle_id],
                                ).fetchall()
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
                                d["events"] = events
                            except Exception as ev_e:
                                logger.error("[PipelineStateDB] Failed to fetch events for state: %s", ev_e)
                                d["events"] = []

                        try:
                            ar_rows = db.execute(
                                "SELECT ticker, result_json FROM analysis_results WHERE cycle_id = %s",
                                [cycle_id],
                            ).fetchall()
                            results = []
                            for ar in ar_rows:
                                try:
                                    res = json.loads(ar[1])
                                    if "ticker" not in res:
                                        res["ticker"] = ar[0]
                                    results.append(res)
                                except Exception:
                                    pass
                            d["results"] = results
                        except Exception as ar_e:
                            logger.error("[PipelineStateDB] Failed to fetch results for state: %s", ar_e)
                            d["results"] = []
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
