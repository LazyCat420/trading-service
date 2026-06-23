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
                        tickers, progress, error, phase,
                        updated_at
                    ) VALUES (
                        %s, %s, %s, %s, %s,
                        %s::jsonb, %s, %s, %s,
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
                    ],
                )
        except Exception as e:
            logger.error("[PipelineStateDB] Failed to save DB core state: %s", e)

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
