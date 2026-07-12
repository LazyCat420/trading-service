import asyncio
import json
import uuid
import pytest
from unittest.mock import MagicMock
from datetime import datetime, timezone
from cycle_main import poll_system_commands

pytestmark = pytest.mark.asyncio

async def test_legacy_checkpoint_commands_are_noops(mock_db):
    job_id_1 = f"job-{uuid.uuid4().hex[:8]}"
    job_id_2 = f"job-{uuid.uuid4().hex[:8]}"
    
    # We will simulate fetchone returning rows for our commands, then None
    # We yield job 1, then job 2, then None forever.
    def side_effect_fetchone():
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return (job_id_1, "DISCARD_CHECKPOINT", json.dumps({}))
        elif call_count == 2:
            return (job_id_2, "FORCE_CHECKPOINT", json.dumps({}))
        else:
            return None

    call_count = 0
    mock_db.fetchone.side_effect = side_effect_fetchone

    shutdown_event = asyncio.Event()
    poller_task = asyncio.create_task(poll_system_commands(shutdown_event))
    
    # Let it poll multiple times (cycle_main sleeps 1.0s per loop)
    await asyncio.sleep(2.5)
    shutdown_event.set()
    
    try:
        await asyncio.wait_for(poller_task, timeout=2.0)
    except (asyncio.CancelledError, asyncio.TimeoutError):
        pass

    # Verify that the DB was updated with status='completed' and the expected result
    # We look through all the calls to mock_db.execute and check the UPDATEs.
    update_calls = [call for call in mock_db.execute.mock_calls if "UPDATE v3_system_commands SET status = 'completed'" in str(call)]
    
    # We expect 2 completions
    assert len(update_calls) >= 2
    
    # Find the call for job 1
    call1 = next(c for c in update_calls if c.args[1][1] == job_id_1)
    res1_json = call1.args[1][0]
    res1 = json.loads(res1_json)
    assert res1.get("status") == "ok"
    assert "No checkpoint system active" in res1.get("message", "")
    
    # Find the call for job 2
    call2 = next(c for c in update_calls if c.args[1][1] == job_id_2)
    res2_json = call2.args[1][0]
    res2 = json.loads(res2_json)
    assert res2.get("status") == "ok"
    assert "No checkpoint system active" in res2.get("message", "")
