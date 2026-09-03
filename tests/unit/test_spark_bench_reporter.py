import pytest
from unittest.mock import AsyncMock, patch, MagicMock
from datetime import datetime, timezone
import httpx

from app.services.bench_reporter import (
    emit_bench_run,
    build_run_id,
    serialize_decisions,
)

@pytest.mark.asyncio
async def test_emit_bench_run_success():
    run_id = "tc-2026-09-03T21:14:05Z"
    decisions = [
        {
            "ts": "2026-09-03T21:20:10Z",
            "symbol": "AAPL",
            "action": "buy",
            "confidence": 0.72,
            "rationale": "Strong breakout above 200 EMA",
        }
    ]

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {"ok": True, "run_id": run_id}

    with patch("httpx.AsyncClient.post", new_callable=AsyncMock, return_value=mock_resp) as mock_post:
        result = await emit_bench_run(
            run_id=run_id,
            harness="trading-cycle",
            task="daily-cycle",
            started_at="2026-09-03T21:14:05Z",
            ended_at="2026-09-03T21:22:41Z",
            status="ok",
            decisions=decisions,
            notes="daily test run",
        )

        assert result is not None
        assert result.get("ok") is True
        mock_post.assert_called_once()
        args, kwargs = mock_post.call_args

        assert "http://10.0.0.141:8800/api/bench/runs" in args[0]
        assert kwargs["headers"]["Content-Type"] == "application/json"

        body = kwargs["json"]
        assert body["run_id"] == run_id
        assert body["harness"] == "trading-cycle"
        assert body["task"] == "daily-cycle"
        assert body["started_at"] == "2026-09-03T21:14:05Z"
        assert body["ended_at"] == "2026-09-03T21:22:41Z"
        assert body["status"] == "ok"
        assert len(body["decisions"]) == 1
        assert body["decisions"][0]["symbol"] == "AAPL"
        assert body["decisions"][0]["action"] == "buy"
        assert body["decisions"][0]["confidence"] == 0.72
        assert body["notes"] == "daily test run"


@pytest.mark.asyncio
async def test_emit_bench_run_fail_safe_on_network_error():
    # If the console server is down or refused, it MUST NOT raise into pipeline
    with patch("httpx.AsyncClient.post", new_callable=AsyncMock, side_effect=httpx.ConnectError("Connection refused")):
        result = await emit_bench_run(
            run_id="tc-err-123",
            status="ok",
        )
        assert result is None  # Handled safely, returns None without raising


def test_build_run_id_format():
    dt = datetime(2026, 9, 3, 21, 14, 5, tzinfo=timezone.utc)
    run_id = build_run_id(timestamp=dt)
    assert run_id == "tc-2026-09-03T21:14:05Z"


def test_serialize_decisions_from_results():
    results = [
        {
            "ticker": "AAPL",
            "action": "BUY",
            "confidence": 0.85,
            "rationale": "High conviction breakout",
            "triage_tier": "tier_1",
        },
        {
            "ticker": "MSFT",
            "action": "HOLD",
            "confidence": 0.50,
            "rationale": "No setup",
        },
        None,  # skipped ticker
        Exception("Crashed"),  # failed ticker
    ]

    decisions = serialize_decisions(results)
    assert len(decisions) == 2
    assert decisions[0]["symbol"] == "AAPL"
    assert decisions[0]["action"] == "buy"
    assert decisions[0]["confidence"] == 0.85
    assert decisions[0]["rationale"] == "High conviction breakout"
    assert "ts" in decisions[0]

    assert decisions[1]["symbol"] == "MSFT"
    assert decisions[1]["action"] == "hold"
    assert decisions[1]["confidence"] == 0.50


@pytest.mark.asyncio
async def test_pipeline_service_bench_context():
    from app.services.pipeline_service import PipelineService
    from lazycat.llm import get_bench_context, clear_bench_context

    clear_bench_context()

    with patch("app.services.pipeline_service.PipelineStateDB.get_state", return_value={"status": "idle"}), \
         patch("app.services.pipeline_service.PipelineService.save_state"), \
         patch("app.services.pipeline_service.PipelineService.emit"), \
         patch("app.services.bench_reporter.emit_bench_run", new_callable=AsyncMock) as mock_bench, \
         patch("app.services.prism_agent_caller.prism_client.reset_kill_switch"), \
         patch("app.services.prism_agent_caller.llm.reset_kill_switch"), \
         patch.object(PipelineService, "_run_all_v3", new_callable=AsyncMock):

        await PipelineService.start_cycle(tickers=["AAPL"], cycle_id="cycle-test-1")

        bench_ctx = get_bench_context()
        assert bench_ctx["run_id"] is not None
        assert bench_ctx["run_id"].startswith("tc-")
        assert bench_ctx["harness"] == "trading-cycle"

        # Assert initial registration was fired
        mock_bench.assert_called()
        first_call_kwargs = mock_bench.call_args[1]
        assert first_call_kwargs["status"] == "running"
        assert first_call_kwargs["run_id"] == bench_ctx["run_id"]

    clear_bench_context()
