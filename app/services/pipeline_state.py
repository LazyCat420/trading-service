import json
import logging
import uuid
from datetime import datetime, timezone
from app.db import mongo_query
from app.db import mongo_store

logger = logging.getLogger(__name__)


def _stringify_timestamp(value):
    if not value:
        return None
    if isinstance(value, str):
        return value
    if hasattr(value, "tzinfo") and value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.isoformat() if hasattr(value, "isoformat") else str(value)


class PipelineStateDB:
    SINGLETON_ID = "current"

    @classmethod
    def save_state(cls, state: dict):
        try:
            now_utc = datetime.now(timezone.utc)
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
                },
            )
        except Exception as e:
            logger.error("[PipelineStateDB] Failed to save DB core state: %s", e)

    @classmethod
    def append_events(cls, cycle_id: str, events: list[dict]):
        if not cycle_id or not events:
            return
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
            docs = []
            for r in records:
                d = dict(r)
                ts = d.get("timestamp")
                if isinstance(ts, str):
                    try:
                        d["timestamp"] = datetime.fromisoformat(ts)
                    except ValueError:
                        pass
                docs.append(d)
            mongo_store.insert_docs("pipeline_events", docs)
        except Exception as me:
            logger.error("[PipelineStateDB] Mongo events append failed: %s", me)

    @classmethod
    def get_state(cls, summary_only: bool = False) -> dict:
        try:
            d = None
            docs = mongo_store.find_docs(
                "pipeline_state", {"singleton_id": cls.SINGLETON_ID}, limit=1
            )
            if docs:
                d = dict(docs[0])
                d.pop("_id", None)
                d.pop("singleton_id", None)

            if d:
                cycle_id = d.get("cycle_id")
                if cycle_id and not summary_only:
                    try:
                        d["events"] = mongo_store.read_pipeline_events(cycle_id)
                    except Exception as mev_e:
                        logger.error("[PipelineStateDB] Mongo events read failed: %s", mev_e)
                        d["events"] = []

                    try:
                        ar_docs = mongo_store.find_docs(
                            "analysis_results",
                            {"cycle_id": cycle_id},
                            projection={"_id": 0, "ticker": 1, "result_json": 1},
                        )
                        results = []
                        for ar in ar_docs:
                            try:
                                res = ar.get("result_json")
                                if isinstance(res, str):
                                    res = json.loads(res)
                                elif not isinstance(res, dict):
                                    res = {}
                                if "ticker" not in res and ar.get("ticker"):
                                    res["ticker"] = ar["ticker"]
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
