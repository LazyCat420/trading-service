import pytest
import uuid
import json
from datetime import datetime, timezone
from app.db.connection import get_db
from app.services.pipeline_state import PipelineStateDB


import os


def test_pipeline_events_db_persistence(patch_real_get_db):
    """Round-trip pipeline events through a REAL database.

    This used to be gated on `TRADING_BOT_TEST_DB` alone while requesting no db
    fixture at all — so even with the flag set it ran against the autouse
    `get_db` MagicMock and asserted `retrieved["cycle_id"] == cycle_id` on a
    mock whose `fetchone()` returns None. It could not pass in either state, and
    the env-var gate implied a real connection the test never opened.

    `patch_real_get_db` — NOT `real_db` — is what makes the gate honest.
    `real_db` hands back a real cursor for the TEST's own queries but leaves
    `get_db` mocked for the code under test, so a test requesting only `real_db`
    still exercises a MagicMock. `patch_real_get_db` wraps it and patches
    `app.db.connection.get_db`; both skip unless `TRADING_BOT_TEST_DB` is set
    and `TEST_DATABASE_URL` actually answers.
    """
    cycle_id = f"test-events-{uuid.uuid4().hex[:6]}"
    
    # 1. Save core state first
    state = {
        "status": "running",
        "cycle_id": cycle_id,
        "tickers": ["AAPL", "MSFT"],
        "progress": "Testing state",
        "phase": "running"
    }
    PipelineStateDB.save_state(state)
    
    # 2. Append test events
    events = [
        {
            "ts": datetime.now(timezone.utc).isoformat(),
            "phase": "analyzing",
            "step": "test_step_1",
            "detail": "Test detail 1",
            "status": "ok",
            "data": {"some_key": "some_value"},
            "elapsed_ms": 123
        },
        {
            "ts": datetime.now(timezone.utc).isoformat(),
            "phase": "trading",
            "step": "test_step_2",
            "detail": "Test detail 2",
            "status": "error",
            "data": {"error_key": "error_value"},
            "elapsed_ms": 456
        }
    ]
    
    PipelineStateDB.append_events(cycle_id, events)
    
    # 3. Retrieve state with summary_only=False
    retrieved = PipelineStateDB.get_state(summary_only=False)
    
    assert retrieved["cycle_id"] == cycle_id
    assert "events" in retrieved
    assert len(retrieved["events"]) >= 2
    
    # Verify first event
    ev1 = [e for e in retrieved["events"] if e["step"] == "test_step_1"][0]
    assert ev1["phase"] == "analyzing"
    assert ev1["detail"] == "Test detail 1"
    assert ev1["status"] == "ok"
    assert ev1["data"]["some_key"] == "some_value"
    assert ev1["elapsed_ms"] == 123
    
    # Verify second event
    ev2 = [e for e in retrieved["events"] if e["step"] == "test_step_2"][0]
    assert ev2["phase"] == "trading"
    assert ev2["detail"] == "Test detail 2"
    assert ev2["status"] == "error"
    assert ev2["data"]["error_key"] == "error_value"
    assert ev2["elapsed_ms"] == 456
    
    # 4. Retrieve state with summary_only=True and assert events are empty
    retrieved_summary = PipelineStateDB.get_state(summary_only=True)
    assert retrieved_summary["cycle_id"] == cycle_id
    assert "events" not in retrieved_summary or len(retrieved_summary["events"]) == 0
    
    # Clean up test events and state
    with get_db() as db:
        db.execute("DELETE FROM pipeline_events WHERE cycle_id = %s", [cycle_id])
        db.execute("UPDATE pipeline_state SET cycle_id = NULL, status = 'idle' WHERE singleton_id = 'current'")
    
    print("Database events persistence and retrieval tests passed successfully!")

if __name__ == "__main__":
    test_pipeline_events_db_persistence()
