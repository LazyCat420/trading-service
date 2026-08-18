import asyncio
import uuid
import pytest
from unittest.mock import MagicMock, patch
from cycle_main import poll_system_commands

pytestmark = pytest.mark.asyncio


async def test_legacy_checkpoint_commands_are_noops():
    job_id_1 = f"job-{uuid.uuid4().hex[:8]}"
    job_id_2 = f"job-{uuid.uuid4().hex[:8]}"

    # The poller CLAIMS a command with a find_one_and_update on the
    # v3_system_commands collection and RETURNS the claimed document. Hand it
    # job 1, then job 2, then nothing forever.
    def side_effect_claim(*_a, **_kw):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return {"id": job_id_1, "command_type": "DISCARD_CHECKPOINT", "payload": {}}
        elif call_count == 2:
            return {"id": job_id_2, "command_type": "FORCE_CHECKPOINT", "payload": {}}
        return None

    call_count = 0

    coll = MagicMock()
    coll.find_one_and_update.side_effect = side_effect_claim
    doc_db = MagicMock()
    doc_db.__getitem__.return_value = coll

    upserts = []

    def _upsert(collection, key, doc, **_kw):
        upserts.append((collection, key, doc))

    from app.db import mongo_store

    with patch.object(mongo_store, "get_doc_db", return_value=doc_db), \
         patch.object(mongo_store, "upsert_doc", side_effect=_upsert), \
         patch("cycle_main.drain_schedule_refreshes", lambda: None):
        shutdown_event = asyncio.Event()
        poller_task = asyncio.create_task(poll_system_commands(shutdown_event))

        # Let it poll multiple times (cycle_main sleeps 1.0s per loop)
        await asyncio.sleep(2.5)
        shutdown_event.set()

        try:
            await asyncio.wait_for(poller_task, timeout=2.0)
        except (asyncio.CancelledError, asyncio.TimeoutError):
            pass

    # The command row is updated to status='completed' with the expected
    # result. The status is parameterized (status-truth fix: bounced
    # START_CYCLEs get 'skipped'); checkpoint no-ops return {"status": "ok"}
    # → 'completed'.
    #
    # This used to match a SQL substring and index into a positional parameter
    # list. Reading the upsert's key and document instead means a completion
    # written against the wrong job id — or into the wrong collection — fails
    # here rather than matching on text.
    completions = [
        (key, doc) for collection, key, doc in upserts
        if collection == "v3_system_commands" and doc.get("status") == "completed"
    ]

    # We expect 2 completions
    assert len(completions) >= 2

    # Find the write for job 1.
    _key1, doc1 = next(kd for kd in completions if kd[0] == {"id": job_id_1})
    res1 = doc1["result"]
    assert res1.get("status") == "ok"
    assert "No checkpoint system active" in res1.get("message", "")

    # Find the write for job 2.
    _key2, doc2 = next(kd for kd in completions if kd[0] == {"id": job_id_2})
    res2 = doc2["result"]
    assert res2.get("status") == "ok"
    assert "No checkpoint system active" in res2.get("message", "")
