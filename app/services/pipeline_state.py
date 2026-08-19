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
        try:
            events = mongo_store.read_pipeline_events(cycle_id)
            return events[:limit] if limit else events
        except Exception as mev_e:
            logger.error("[PipelineStateDB] Mongo events read failed for %s: %s", cycle_id, mev_e)
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
        try:
            results = []
            for doc in mongo_store.find_docs(
                "analysis_results", {"cycle_id": cycle_id},
                projection={"_id": 0, "ticker": 1, "result_json": 1},
            ):
                try:
                    res = doc.get("result_json")
                    if isinstance(res, str):
                        res = json.loads(res)
                    elif not isinstance(res, dict):
                        res = {}
                    if "ticker" not in res and doc.get("ticker"):
                        res["ticker"] = doc["ticker"]
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
            d = None
            docs = mongo_store.find_docs(
                "pipeline_state", {"singleton_id": cls.SINGLETON_ID}, limit=1
            )
            if docs:
                d = dict(docs[0])
                d.pop("_id", None)
                d.pop("singleton_id", None)

            if d:
                # Enrich with events and results when the caller wants the
                # full state; both readers are the ONE definition of the wire
                # shape (see the per-cycle readers above).
                cycle_id = d.get("cycle_id")
                if cycle_id and not summary_only:
                    d["events"] = cls.get_cycle_events(cycle_id)
                    d["results"] = cls.get_cycle_results(cycle_id)
                else:
                    d["events"] = []
                    d["results"] = []

                return d
        except Exception as e:
            # A READ FAILURE IS NOT AN IDLE PIPELINE.
            #
            # This used to fall through to default_state(), whose status is
            # "idle" — and "idle" is a member of IDLE_STATUSES in both deploy
            # interlocks (.claude/hooks/guard_deploy.py:35 and
            # trading-service/.claude/hooks/_check_cycle_running.py). So a
            # Mongo outage, or a singleton that has not caught up, told the
            # guard "no cycle is running, safe to restart" — and the symptom
            # of a read fault was a deploy killing a live cycle, which is how
            # EXLS/OWL/CARS/GM were lost on 2026-07-27.
            #
            # The conversion to pure Mongo removed the Postgres fallback AND
            # the handle_mongo_read_failure call that used to sit here, so
            # there is nothing left between the exception and the wrong
            # answer. `status: "unknown"` is not in IDLE_STATUSES, so the
            # guard now refuses the deploy instead of waving it through, and
            # a caller that genuinely wants "treat unknown as idle" has to say
            # so explicitly.
            logger.error("[PipelineStateDB] Failed to get state: %s", e)
            unknown = cls.default_state()
            unknown["status"] = "unknown"
            unknown["error"] = f"pipeline_state read failed: {e}"
            return unknown
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
