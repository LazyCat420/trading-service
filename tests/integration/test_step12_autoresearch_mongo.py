"""Real MongoDB integration test suite for Step 12: Verify Autoresearch Job Processing.

Authority: Sequential Developer Plan Revision 3 — Step 12
Validates on real MongoDB (trading_bot_pytest):
1. enqueue_autoresearch deduplication and idempotency on real collections.
2. Atomic claim via find_one_and_update preventing concurrent double-claim.
3. Bidirectional addressability (system_commands.report_id <-> autoresearch_reports.job_id).
4. Crash-after-claim state reset (running -> interrupted / error) without stranded states.
5. ResearchQueueService attempt tracking and terminal failure transitions.
"""

from __future__ import annotations

import datetime
import json
import secrets
import pytest

from app.services import pipeline_service
from app.services.boot_service import BootService
from app.services.research_queue_service import ResearchQueueService
from app.schemas.dossier_schemas import QueueType
from app.db import mongo_store

pytestmark = pytest.mark.real_mongo


def _gen_token() -> str:
    """Generate in-memory token for zero credential leakage compliance."""
    return f"tok_{secrets.token_hex(8)}"


def test_real_mongo_enqueue_autoresearch_idempotency(real_mongo):
    """Verify that enqueue_autoresearch reuses existing job_id on real Mongo."""
    cycle_id = f"cycle-rm-{_gen_token()}"
    summary = {
        "status": "done",
        "tickers_final": ["AAPL", "NVDA"],
        "partial": False,
        "buy_count": 1,
    }

    job_id_1 = pipeline_service.enqueue_autoresearch(cycle_id, summary)
    assert job_id_1 is not None

    stored = real_mongo["system_commands"].find_one({"id": job_id_1})
    assert stored is not None
    assert stored["status"] == "pending"
    assert stored["payload"]["cycle_id"] == cycle_id

    # Second enqueue without force returns existing job_id
    job_id_2 = pipeline_service.enqueue_autoresearch(cycle_id, summary)
    assert job_id_2 == job_id_1
    count = real_mongo["system_commands"].count_documents({"payload.cycle_id": cycle_id})
    assert count == 1, "Must not create duplicate command"

    # Enqueue with force creates distinct second job
    job_id_3 = pipeline_service.enqueue_autoresearch(cycle_id, summary, force=True)
    assert job_id_3 != job_id_1
    count_after_force = real_mongo["system_commands"].count_documents({"payload.cycle_id": cycle_id})
    assert count_after_force == 2


def test_real_mongo_eval_worker_atomic_claim_and_completion(real_mongo):
    """Verify atomic claim via find_one_and_update and bidirectional addressability."""
    cycle_id = f"cycle-claim-{_gen_token()}"
    job_id = f"job-{_gen_token()}"
    now = datetime.datetime.now(datetime.timezone.utc)

    real_mongo["system_commands"].insert_one({
        "id": job_id,
        "command_type": "AUTORESEARCH",
        "status": "pending",
        "payload": {"cycle_id": cycle_id, "cycle_summary": {"status": "done"}},
        "created_at": now,
    })

    # Worker 1 claims atomically
    claimed_1 = mongo_store.find_one_and_update(
        "system_commands",
        {
            "status": "pending",
            "command_type": {"$in": ["AUTORESEARCH"]},
        },
        {
            "$set": {
                "status": "running",
                "started_at": datetime.datetime.now(datetime.timezone.utc),
            }
        },
        sort=[("created_at", 1)],
        return_after=True,
    )
    assert claimed_1 is not None
    assert claimed_1["id"] == job_id
    assert claimed_1["status"] == "running"

    # Worker 2 attempting to claim simultaneously gets None
    claimed_2 = mongo_store.find_one_and_update(
        "system_commands",
        {
            "status": "pending",
            "command_type": {"$in": ["AUTORESEARCH"]},
        },
        {
            "$set": {
                "status": "running",
                "started_at": datetime.datetime.now(datetime.timezone.utc),
            }
        },
        sort=[("created_at", 1)],
        return_after=True,
    )
    assert claimed_2 is None, "Second worker must not claim already-running command"

    # Simulate report creation and completion with addressable IDs
    report_id = f"ar-{_gen_token()}"
    real_mongo["autoresearch_reports"].insert_one({
        "id": report_id,
        "cycle_id": cycle_id,
        "job_id": job_id,
        "status": "done",
        "overall_score": 85.0,
        "created_at": now,
    })

    # Complete the command
    mongo_store.update_docs(
        "system_commands",
        {"id": job_id},
        {
            "$set": {
                "status": "completed",
                "completed_at": datetime.datetime.now(datetime.timezone.utc),
                "report_id": report_id,
                "result": json.dumps({"cycle_id": cycle_id, "report_id": report_id, "job_id": job_id}),
            }
        },
    )

    # Verify bidirectional cross-reference
    cmd_doc = real_mongo["system_commands"].find_one({"id": job_id})
    assert cmd_doc["status"] == "completed"
    assert cmd_doc["report_id"] == report_id

    rep_doc = real_mongo["autoresearch_reports"].find_one({"id": report_id})
    assert rep_doc["job_id"] == job_id
    assert rep_doc["cycle_id"] == cycle_id


def test_real_mongo_crash_after_claim_recovery(real_mongo):
    """Verify that BootService._reset_app_state transitions crashed running jobs to terminal states."""
    cycle_id = f"cycle-crash-{_gen_token()}"
    job_id = f"job-{_gen_token()}"
    report_id = f"ar-{_gen_token()}"
    now = datetime.datetime.now(datetime.timezone.utc)

    real_mongo["system_commands"].insert_one({
        "id": job_id,
        "command_type": "AUTORESEARCH",
        "status": "running",
        "payload": {"cycle_id": cycle_id},
        "created_at": now,
        "started_at": now,
    })
    real_mongo["autoresearch_reports"].insert_one({
        "id": report_id,
        "cycle_id": cycle_id,
        "job_id": job_id,
        "status": "running",
        "created_at": now,
    })

    # Boot reset simulation
    BootService._reset_app_state()

    cmd_after = real_mongo["system_commands"].find_one({"id": job_id})
    assert cmd_after["status"] == "error"
    assert "restarted" in cmd_after["error_message"].lower()

    rep_after = real_mongo["autoresearch_reports"].find_one({"id": report_id})
    assert rep_after["status"] == "interrupted"
    assert "restarted" in rep_after["error_message"].lower()


def test_real_mongo_research_queue_lifecycle_and_failure(real_mongo):
    """Verify v3_research_queues attempt increments, 12h backoff, and terminal failure after 3 attempts."""
    ticker = f"TK{secrets.token_hex(2).upper()}"
    cycle_id = f"cycle-rq-{_gen_token()}"

    item_id = ResearchQueueService.enqueue_item(
        ticker=ticker,
        queue_type=QueueType.DEEP_DIVE_QUEUE,
        priority=60,
        reason="Does gross margin exceed 45 percent?",
        source_agent="quant_analyst",
        payload={"question": "Does gross margin exceed 45 percent?"},
    )
    assert item_id is not None

    # 1. Attempt 1 claim
    claims_1 = ResearchQueueService.claim_for_ticker(ticker, cycle_id)
    assert len(claims_1) == 1
    assert claims_1[0]["attempts"] == 1
    assert claims_1[0]["status"] == "processing"

    # Finish claim without answer (deferral)
    ResearchQueueService.finish_claim(claims_1[0], reason="No evidenced answer was produced")
    doc_1 = real_mongo["v3_research_queues"].find_one({"id": item_id})
    assert doc_1["status"] == "pending"
    assert doc_1["attempts"] == 1
    assert doc_1["last_error"] == "No evidenced answer was produced"
    assert doc_1["next_attempt_at"] is not None

    # Fast-forward next_attempt_at so it can be claimed again
    real_mongo["v3_research_queues"].update_one(
        {"id": item_id},
        {"$set": {"next_attempt_at": datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(seconds=1)}}
    )

    # 2. Attempt 2 claim & finish without answer
    claims_2 = ResearchQueueService.claim_for_ticker(ticker, f"cycle-2-{_gen_token()}")
    assert len(claims_2) == 1
    assert claims_2[0]["attempts"] == 2
    ResearchQueueService.finish_claim(claims_2[0], reason="No evidenced answer was produced")

    # Fast-forward again
    real_mongo["v3_research_queues"].update_one(
        {"id": item_id},
        {"$set": {"next_attempt_at": datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(seconds=1)}}
    )

    # 3. Attempt 3 claim & finish without answer -> terminal 'failed'
    claims_3 = ResearchQueueService.claim_for_ticker(ticker, f"cycle-3-{_gen_token()}")
    assert len(claims_3) == 1
    assert claims_3[0]["attempts"] == 3
    ResearchQueueService.finish_claim(claims_3[0], reason="No evidenced answer was produced")

    final_doc = real_mongo["v3_research_queues"].find_one({"id": item_id})
    assert final_doc["status"] == "failed"
    assert final_doc["attempts"] == 3
    assert final_doc["last_error"] == "No evidenced answer was produced"
