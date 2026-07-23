"""
V3 Telemetry — Per-agent metrics, phase outcomes, and pipeline summary.

Records telemetry to:
1. Standard Python logger (container logs)
2. Existing log_manager.log_v2_cycle() for cycle-level tracking
3. PostgreSQL v3_agent_telemetry table for dashboard queries
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

from app.v3.shared_desk import SharedDesk

logger = logging.getLogger(__name__)

_TABLE_ENSURED = False


def _ensure_telemetry_table() -> None:
    """Create the v3_agent_telemetry table if it doesn't exist."""
    global _TABLE_ENSURED
    if _TABLE_ENSURED:
        return

    from app.db.connection import get_db

    try:
        with get_db() as db:
            db.execute("""
                CREATE TABLE IF NOT EXISTS v3_agent_telemetry (
                    id SERIAL PRIMARY KEY,
                    cycle_id TEXT NOT NULL,
                    ticker TEXT NOT NULL,
                    agent_name TEXT NOT NULL,
                    phase TEXT NOT NULL,
                    outcome TEXT NOT NULL,
                    elapsed_ms INTEGER DEFAULT 0,
                    loops_used INTEGER DEFAULT 0,
                    token_usage INTEGER DEFAULT 0,
                    artifact_size_bytes INTEGER DEFAULT 0,
                    quality_score INTEGER DEFAULT -1,
                    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
                )
            """)
            # Add quality_score column to existing tables (idempotent)
            db.execute("""
                DO $$ BEGIN
                    ALTER TABLE v3_agent_telemetry ADD COLUMN IF NOT EXISTS quality_score INTEGER DEFAULT -1;
                EXCEPTION WHEN others THEN NULL;
                END $$;
            """)
            db.execute("""
                CREATE INDEX IF NOT EXISTS idx_v3_telemetry_cycle
                ON v3_agent_telemetry (cycle_id)
            """)
            db.execute("""
                CREATE INDEX IF NOT EXISTS idx_v3_telemetry_agent
                ON v3_agent_telemetry (agent_name, created_at)
            """)
        _TABLE_ENSURED = True
        logger.debug("[V3Telemetry] Table v3_agent_telemetry ensured")
    except Exception as e:
        logger.warning("[V3Telemetry] Failed to ensure table: %s", e)


def persist_telemetry(desk: SharedDesk) -> None:
    """Persist all agent telemetry from a SharedDesk to the DB.

    Called once after the pipeline completes. Writes all accumulated
    telemetry entries from desk.agent_telemetry to PostgreSQL.
    """
    _ensure_telemetry_table()

    if not desk.agent_telemetry:
        return

    from app.db.connection import get_db

    try:
        _recs = [
            {
                "cycle_id": desk.cycle_id, "ticker": desk.ticker,
                "agent_name": entry.get("agent_name", "?"), "phase": entry.get("phase", "?"),
                "outcome": entry.get("outcome", "?"), "elapsed_ms": entry.get("elapsed_ms", 0),
                "loops_used": entry.get("loops_used", 0), "token_usage": entry.get("token_usage", 0),
                "quality_score": entry.get("quality_score", -1),
                "artifact_size_bytes": entry.get("artifact_size_bytes", 0),
            }
            for entry in desk.agent_telemetry
        ]
        with get_db() as db:
            for r in _recs:
                db.execute(
                    """
                    INSERT INTO v3_agent_telemetry
                        (cycle_id, ticker, agent_name, phase, outcome,
                         elapsed_ms, loops_used, token_usage, quality_score,
                         artifact_size_bytes)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    [r["cycle_id"], r["ticker"], r["agent_name"], r["phase"], r["outcome"],
                     r["elapsed_ms"], r["loops_used"], r["token_usage"], r["quality_score"],
                     r["artifact_size_bytes"]],
                )
        try:
            import uuid as _uuid
            from datetime import datetime, timezone
            from app.db import mongo_store
            if _recs and mongo_store.writes_mongo("v3_agent_telemetry"):
                # PG assigns a serial id + default created_at the mirror can't
                # see; give Mongo docs their own id and a real timestamp so the
                # collection has a usable key. (Post-cutover this id IS the id.)
                _now = datetime.now(timezone.utc)
                mongo_store.insert_docs(
                    "v3_agent_telemetry",
                    [{**r, "id": str(_uuid.uuid4()), "created_at": _now} for r in _recs],
                )
        except Exception as me:
            logger.warning("[V3Telemetry] Mongo mirror failed (non-fatal): %s", me)
        logger.info(
            "[V3Telemetry] Persisted %d telemetry entries for %s/%s",
            len(desk.agent_telemetry),
            desk.cycle_id[:12] if desk.cycle_id else "?",
            desk.ticker,
        )
    except Exception as e:
        logger.warning("[V3Telemetry] Failed to persist telemetry: %s", e)


def get_pipeline_summary(desk: SharedDesk) -> dict[str, Any]:
    """Build a summary of the pipeline's telemetry for logging/display."""
    total_ms = sum(e.get("elapsed_ms", 0) for e in desk.agent_telemetry)
    total_tokens = sum(e.get("token_usage", 0) for e in desk.agent_telemetry)
    agents_run = [e.get("agent_name", "?") for e in desk.agent_telemetry]
    outcomes = {
        e.get("agent_name", "?"): e.get("outcome", "?")
        for e in desk.agent_telemetry
    }

    return {
        "cycle_id": desk.cycle_id,
        "ticker": desk.ticker,
        "final_phase": desk.phase.value,
        "agents_run": agents_run,
        "agent_count": len(agents_run),
        "total_elapsed_ms": total_ms,
        "total_tokens": total_tokens,
        "outcomes": outcomes,
        "phase_outcomes": desk.phase_outcomes,
    }
