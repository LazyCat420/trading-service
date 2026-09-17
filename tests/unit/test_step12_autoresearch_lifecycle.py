"""Unit tests for Step 12: Autoresearch Job Processing Lifecycle.

Covers:
1. Idempotent enqueue preventing duplicate jobs.
2. Atomic claim preventing double-processing across workers.
3. Missing summary and payload error transitions (zero false success).
4. Addressable cycle/job/report IDs.
5. Lifecycle traces across 6 cycle profiles (normal, stopped, aborted, empty, missing-data, model-failure).
"""
import asyncio
import json
import secrets
from unittest.mock import AsyncMock, MagicMock, patch
import pytest

from app.services import pipeline_service
from app.autoresearch import eval_worker, core, eval_engine
from app.autoresearch.eval_engine import TraceRecord


def test_enqueue_autoresearch_idempotency_prevents_duplicate_jobs():
    """Verify that calling enqueue_autoresearch multiple times for the same cycle

    returns the existing job_id rather than inserting duplicate commands.
    """
    cycle_id = f"cycle-test-{secrets.token_hex(4)}"
    summary = {"status": "done", "tickers_final": ["AAPL"], "partial": False}

    stored_cmds = []

    def mock_find_docs(col, query, **kwargs):
        if col == "system_commands":
            matching = []
            for doc in stored_cmds:
                if doc.get("command_type") == query.get("command_type"):
                    p_cid = (doc.get("payload") or {}).get("cycle_id")
                    if p_cid == cycle_id and doc.get("status") in query.get("status", {}).get("$in", []):
                        matching.append(doc)
            return matching
        return []

    def mock_upsert_doc(col, filt, doc):
        if col == "system_commands":
            stored_cmds.append(doc)
            return 1
        return 0

    with patch.object(pipeline_service.mongo_store, "find_docs", side_effect=mock_find_docs), \
         patch.object(pipeline_service.mongo_store, "upsert_doc", side_effect=mock_upsert_doc):

        job1 = pipeline_service.enqueue_autoresearch(cycle_id, summary)
        assert job1 is not None
        assert len(stored_cmds) == 1

        # Second enqueue for same cycle_id
        job2 = pipeline_service.enqueue_autoresearch(cycle_id, summary)
        assert job2 == job1
        assert len(stored_cmds) == 1, "Duplicate command must not be inserted"

        # Force enqueue allows new job
        job3 = pipeline_service.enqueue_autoresearch(cycle_id, summary, force=True)
        assert job3 is not None
        assert job3 != job1
        assert len(stored_cmds) == 2


def test_eval_worker_atomic_claim_prevents_double_processing():
    """Verify that poll_system_commands atomically claims via find_one_and_update."""
    job_id = f"job-{secrets.token_hex(4)}"
    cmd_doc = {
        "id": job_id,
        "command_type": "AUTORESEARCH",
        "status": "pending",
        "payload": {"cycle_id": "cycle-atomic-1", "cycle_summary": {"status": "done"}},
    }

    claim_count = 0

    def mock_find_one_and_update(col, query, update, **kwargs):
        nonlocal claim_count
        if col == "system_commands" and query.get("status") == "pending" and claim_count == 0:
            claim_count += 1
            doc = dict(cmd_doc)
            doc["status"] = "running"
            return doc
        return None

    runner = AsyncMock()

    class _Stop(Exception):
        pass

    async def _sleep(_):
        raise _Stop()

    with patch.object(eval_worker.mongo_store, "find_one_and_update", side_effect=mock_find_one_and_update), \
         patch.object(eval_worker, "run_autoresearch", runner), \
         patch.object(eval_worker.asyncio, "sleep", _sleep):

        with pytest.raises(_Stop):
            asyncio.run(eval_worker.poll_system_commands())

    assert claim_count == 1
    runner.assert_awaited_once()
    assert runner.await_args[0][0] == job_id


def test_missing_cycle_summary_marks_command_error_not_completed():
    """A job missing cycle_summary must fail with status='error' and not stamp completed."""
    job_id = f"job-{secrets.token_hex(4)}"
    payload_no_summary = {"cycle_id": "cycle-bad"}

    updates = []

    def mock_update_docs(col, filt, update):
        if col == "system_commands":
            updates.append((filt, update))
        return 1

    cmd_doc = {
        "id": job_id,
        "command_type": "AUTORESEARCH",
        "status": "running",
        "payload": payload_no_summary,
    }

    class _Stop(Exception):
        pass

    async def _sleep(_):
        raise _Stop()

    with patch.object(eval_worker.mongo_store, "find_one_and_update", return_value=cmd_doc), \
         patch.object(eval_worker.mongo_store, "update_docs", side_effect=mock_update_docs), \
         patch.object(eval_worker.asyncio, "sleep", _sleep):

        with pytest.raises(_Stop):
            asyncio.run(eval_worker.poll_system_commands())

    # Verify that status became 'error'
    error_updates = [u for u in updates if u[1].get("$set", {}).get("status") == "error"]
    assert len(error_updates) >= 1, "Missing cycle_summary must produce terminal error"
    assert "Missing cycle_summary" in error_updates[0][1]["$set"]["error_message"]

    completed_updates = [u for u in updates if u[1].get("$set", {}).get("status") == "completed"]
    assert len(completed_updates) == 0, "Missing cycle_summary must NEVER produce false completed success"


def test_addressable_cycle_job_report_ids():
    """Verify bidirectional addressability between system_commands and autoresearch_reports."""
    job_id = f"job-{secrets.token_hex(4)}"
    cycle_id = f"cycle-addr-{secrets.token_hex(4)}"
    summary = {"status": "done", "tickers_final": ["AAPL"]}

    reports_inserted = []
    reports_updated = []
    commands_updated = []

    with patch.object(core.mongo_store, "insert_docs", side_effect=lambda col, docs: reports_inserted.extend(docs) or len(docs)), \
         patch.object(core.mongo_store, "update_docs", side_effect=lambda col, filt, upd: reports_updated.append((filt, upd)) or 1), \
         patch.object(eval_worker.mongo_store, "update_docs", side_effect=lambda col, filt, upd: commands_updated.append((filt, upd)) or 1), \
         patch.object(eval_worker, "process_pending_traces", return_value=0), \
         patch.object(core, "_reflect", AsyncMock(return_value={"summary": "ok", "recommendations": []})), \
         patch.object(core, "refresh_pending_outcome_prices", AsyncMock(return_value={"refreshed": 0})), \
         patch.object(core, "resolve_pending_outcomes", return_value={"resolved": 0}), \
         patch.object(core, "_audit_data_quality", return_value={"avg_score": 1.0, "gaps": []}), \
         patch.object(core, "_audit_decisions", return_value={"score": 1.0, "score_version": "v6", "issues": []}), \
         patch.object(core, "_audit_llm_traces", return_value={"score": 1.0, "score_version": "v6", "issues": []}), \
         patch.object(core, "_audit_performance", return_value={"total_ms": 100}), \
         patch.object(core, "_audit_recovery", return_value={}), \
         patch.object(core, "_audit_execution_errors", return_value=[]), \
         patch.object(core, "_audit_triage", return_value={}), \
         patch.object(core, "_audit_schedule_health", return_value={}):

        asyncio.run(eval_worker.run_autoresearch(job_id, {"cycle_id": cycle_id, "cycle_summary": summary}))

    # 1. autoresearch_reports contains job_id
    assert len(reports_inserted) == 1
    report_doc = reports_inserted[0]
    assert report_doc["job_id"] == job_id
    assert report_doc["cycle_id"] == cycle_id
    report_id = report_doc["id"]

    # 2. system_commands is updated with report_id
    cmd_done = [u for u in commands_updated if u[1].get("$set", {}).get("status") == "completed"]
    assert len(cmd_done) == 1
    assert cmd_done[0][1]["$set"]["report_id"] == report_id
    result_data = json.loads(cmd_done[0][1]["$set"]["result"])
    assert result_data["cycle_id"] == cycle_id
    assert result_data["job_id"] == job_id
    assert result_data["report_id"] == report_id


from contextlib import ExitStack


@pytest.mark.asyncio
async def test_trace_lifecycle_across_six_cycle_profiles():
    """Verify that run_autoresearch handles all 6 cycle profiles:

    1. Normal cycle
    2. Stopped cycle (partial=True)
    3. Aborted/error cycle
    4. Empty/no-trade cycle
    5. Missing-data cycle (0 data score handled without crash)
    6. Model-failure cycle (LLM raises exception; rule-based reflection fallback succeeds,
       deterministic tool grading preserved)
    """
    profiles = [
        ("normal", {"status": "done", "tickers_final": ["NVDA", "AAPL"], "partial": False}),
        ("stopped", {"status": "stopped", "tickers_final": ["NVDA"], "partial": True}),
        ("aborted", {"status": "error", "error": "Execution timeout", "partial": True}),
        ("empty", {"status": "done", "tickers_final": [], "partial": False}),
        ("missing_data", {"status": "done", "tickers_final": ["XYZ"], "data_gaps": True}),
        ("model_failure", {"status": "done", "tickers_final": ["AAPL"], "model_failure": True}),
    ]

    for p_name, summary in profiles:
        cycle_id = f"cycle-prof-{p_name}-{secrets.token_hex(3)}"
        job_id = f"job-{secrets.token_hex(3)}"

        # Deterministic grading check: simulate a trace
        trace = TraceRecord(
            id=f"tr-{secrets.token_hex(3)}",
            run_id=f"run-{secrets.token_hex(3)}",
            cycle_id=cycle_id,
            agent_name="quant_analyst",
            stop_reason="completed",
            tool_name="get_financial_metrics",
            tool_result_summary="Success",
            loop_step=2,
        )
        score = eval_engine.evaluate_trace(trace)
        assert score["final_score"] >= 70.0
        assert eval_engine.classify_failure(trace, score) is None

        with ExitStack() as stack:
            stack.enter_context(patch.object(core.mongo_store, "insert_docs", return_value=1))
            stack.enter_context(patch.object(core.mongo_store, "update_docs", return_value=1))
            stack.enter_context(patch.object(core, "refresh_pending_outcome_prices", AsyncMock(return_value={"refreshed": 0})))
            stack.enter_context(patch.object(core, "resolve_pending_outcomes", return_value={"resolved": 0}))
            stack.enter_context(patch.object(core, "_audit_performance", return_value={"total_ms": 100}))
            stack.enter_context(patch.object(core, "_audit_recovery", return_value={}))
            stack.enter_context(patch.object(core, "_audit_execution_errors", return_value=[]))
            stack.enter_context(patch.object(core, "_audit_triage", return_value={}))
            stack.enter_context(patch.object(core, "_audit_schedule_health", return_value={}))
            stack.enter_context(patch.object(core, "_store_lessons", return_value={"failed": False}))
            stack.enter_context(patch("app.autoresearch.skill_optimizer.propose_and_validate_skill_edits", AsyncMock(return_value={"skipped": True})))
            stack.enter_context(patch.object(core, "_resolve_data_gaps", AsyncMock(return_value={"resolved": 0})))
            stack.enter_context(patch.object(core, "_generate_directives", return_value=None))
            stack.enter_context(patch.object(core, "_expire_old_directives", return_value=None))
            stack.enter_context(patch.object(core, "record_cycle_decisions", return_value=0))
            stack.enter_context(patch.object(core, "run_janitor", return_value={}))
            stack.enter_context(patch.object(core, "_collect_learning_signals", return_value={}))
            stack.enter_context(patch.object(core, "_collect_context_delivery", return_value={"availability": "unverified"}))

            if summary.get("data_gaps"):
                stack.enter_context(patch.object(core, "_audit_data_quality", return_value={"avg_score": 0.0, "gaps": ["PRICE_MISSING"]}))
                stack.enter_context(patch.object(core, "_audit_decisions", return_value={"score": 0.5, "score_version": "v6", "issues": []}))
                stack.enter_context(patch.object(core, "_audit_llm_traces", return_value={"score": 0.5, "score_version": "v6", "issues": []}))
            else:
                stack.enter_context(patch.object(core, "_audit_data_quality", return_value={"avg_score": 1.0, "gaps": []}))
                stack.enter_context(patch.object(core, "_audit_decisions", return_value={"score": 1.0, "score_version": "v6", "issues": []}))
                stack.enter_context(patch.object(core, "_audit_llm_traces", return_value={"score": 1.0, "score_version": "v6", "issues": []}))

            if summary.get("model_failure"):
                from app.services import prism_agent_caller as pac
                stack.enter_context(patch.object(pac.llm, "chat", AsyncMock(side_effect=RuntimeError("GLM model offline during model migration"))))
            else:
                stack.enter_context(patch.object(core, "_reflect", AsyncMock(return_value={"summary": "Healthy run", "recommendations": []})))

            res = await core.run_autoresearch(cycle_id, summary, job_id=job_id)

            assert res["status"] == "done", f"Profile {p_name} must complete successfully"
            assert "id" in res
            assert res["job_id"] == job_id
            assert res["cycle_id"] == cycle_id
            assert isinstance(res["overall_score"], (int, float))
