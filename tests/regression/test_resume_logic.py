import asyncio
import pytest
from unittest.mock import patch, AsyncMock, MagicMock
from app.pipeline.orchestration.lifecycle_controller import LifecycleControllerMixin
from app.pipeline.orchestration.state_manager import PipelineStateDB
from app.cycle.phases.phase4_analysis import run_phase4_analysis
from app.cycle.context import CycleContext


class DummyController(LifecycleControllerMixin):
    _state = PipelineStateDB.default_state()

    @classmethod
    def emit(cls, *args, **kwargs):
        pass

    @classmethod
    def load_state(cls, *args, **kwargs):
        pass

    @classmethod
    def save_state(cls, *args, **kwargs):
        pass

    @classmethod
    def force_save_checkpoint(cls, *args, **kwargs):
        pass

    @classmethod
    def _run_cycle(cls, *args, **kwargs):
        pass

    @classmethod
    def _checkpoint_heartbeat(cls, *args, **kwargs):
        pass


@pytest.fixture(autouse=True)
def reset_controller_state():
    """Reset the dummy controller state before each test."""
    DummyController._state = PipelineStateDB.default_state()
    DummyController._cycle_task = None
    DummyController._action_lock = None
    yield
    DummyController._state = PipelineStateDB.default_state()
    DummyController._cycle_task = None
    DummyController._action_lock = None


@pytest.mark.asyncio
async def test_start_fresh_bypasses_auto_resume():
    """Ensure that calling start_cycle with start_fresh=True bypasses any existing checkpoint."""
    DummyController._state["status"] = "idle"

    mock_checkpoint = {
        "cycle_id": "cycle-12345",
        "completed_phases": ["collecting"],
        "cycle_config": {"tickers": ["AAPL", "MSFT"]}
    }

    with patch.object(PipelineStateDB, "get_checkpoint", return_value=mock_checkpoint) as mock_get:
        with patch.object(PipelineStateDB, "expire_old_checkpoints") as mock_expire:
            with patch.object(DummyController, "resume_interrupted_cycle", new_callable=AsyncMock) as mock_resume:
                with patch.object(DummyController, "_background_start_cycle", new_callable=AsyncMock) as mock_bg_start:
                    
                    # 1. Call with start_fresh=True (should NOT resume)
                    await DummyController.start_cycle(tickers=["AAPL"], start_fresh=True)
                    
                    mock_resume.assert_not_called()
                    mock_bg_start.assert_called_once()
                    assert DummyController._state["status"] == "starting"


@pytest.mark.asyncio
async def test_start_cycle_auto_resumes_recent_checkpoint():
    """Ensure that start_cycle automatically resumes if an interrupted checkpoint exists and start_fresh=False."""
    DummyController._state["status"] = "idle"

    mock_checkpoint = {
        "cycle_id": "cycle-12345",
        "completed_phases": ["collecting"],
        "cycle_config": {"tickers": ["AAPL", "MSFT"]}
    }

    with patch.object(PipelineStateDB, "get_checkpoint", return_value=mock_checkpoint) as mock_get:
        with patch.object(PipelineStateDB, "expire_old_checkpoints") as mock_expire:
            with patch.object(DummyController, "resume_interrupted_cycle", new_callable=AsyncMock) as mock_resume:
                with patch.object(DummyController, "_background_start_cycle", new_callable=AsyncMock) as mock_bg_start:
                    
                    # 2. Call with start_fresh=False (should auto-resume)
                    await DummyController.start_cycle(tickers=["AAPL"], start_fresh=False)
                    
                    mock_expire.assert_called_once_with(max_age_hours=6)
                    mock_get.assert_called_once()
                    mock_resume.assert_called_once()
                    mock_bg_start.assert_not_called()
                    assert DummyController._state["status"] == "interrupted"
                    assert DummyController._state["cycle_id"] == "cycle-12345"


@pytest.mark.asyncio
async def test_run_phase4_analysis_skips_already_analyzed():
    """Verify that run_phase4_analysis skips tickers listed in already_analyzed and preserves existing_results."""
    ctx = CycleContext(
        tickers=["AAPL", "MSFT", "GOOGL"],
        collect=True,
        analyze=True,
        trade=True,
        cycle_id="cycle-123",
        already_analyzed=["AAPL", "MSFT"],
        existing_results=[
            {"ticker": "AAPL", "action": "BUY", "confidence": 80},
            {"ticker": "MSFT", "action": "HOLD", "confidence": 50}
        ]
    )

    # We mock execute_v2_pipeline to see if it is called only for the non-analyzed ticker (GOOGL)
    mock_pipeline_res = {"ticker": "GOOGL", "action": "SELL", "confidence": 90}
    
    with patch("app.cognition.orchestration.runner.execute_v2_pipeline", new_callable=AsyncMock, return_value=mock_pipeline_res) as mock_execute:
        with patch("app.services.ticker_report_generator.report_generator.save_ticker_report") as mock_report:
            
            results = await run_phase4_analysis(
                ctx=ctx,
                bot_id="test-bot",
                macro_memo="",
                emit=MagicMock(),
                cycle_summary={},
                state={}
            )
            
            # Should only call pipeline execution for GOOGL
            mock_execute.assert_called_once()
            assert mock_execute.call_args[0][0] == "GOOGL"
            
            # The output results must combine the pre-existing results and the new result
            assert len(results) == 3
            tickers_in_res = {r["ticker"]: r for r in results}
            assert "AAPL" in tickers_in_res
            assert "MSFT" in tickers_in_res
            assert "GOOGL" in tickers_in_res
            
            assert tickers_in_res["AAPL"]["action"] == "BUY"
            assert tickers_in_res["MSFT"]["action"] == "HOLD"
            assert tickers_in_res["GOOGL"]["action"] == "SELL"


@pytest.mark.asyncio
async def test_resume_restores_max_tickers_caps():
    """Verify that resuming a cycle successfully recovers max_tickers, discovered_tickers, and dynamic_selection_mode."""
    DummyController._state["status"] = "interrupted"
    DummyController._state["cycle_id"] = "cycle-12345"

    mock_checkpoint = {
        "cycle_id": "cycle-12345",
        "completed_phases": ["collecting"],
        "cycle_config": {
            "tickers": ["AAPL", "MSFT"],
            "max_tickers": 2,
            "discovered_tickers": 2,
            "dynamic_selection_mode": True,
        }
    }

    with patch.object(PipelineStateDB, "get_checkpoint", return_value=mock_checkpoint) as mock_get:
        with patch.object(DummyController, "_run_cycle", new_callable=MagicMock) as mock_run:
            with patch.object(DummyController, "_checkpoint_heartbeat", new_callable=MagicMock):
                await DummyController._background_resume_cycle("cycle-12345")
                
                # Check that DummyController._state was updated with checkpoint settings
                assert DummyController._state["max_tickers"] == 2
                assert DummyController._state["discovered_tickers"] == 2
                assert DummyController._state["dynamic_selection_mode"] is True

                # Check that _run_cycle was task-created with ctx containing correct settings
                mock_run.assert_called_once()
                ctx = mock_run.call_args[0][0]
                assert ctx.max_tickers == 2
                assert ctx.discovered_tickers == 2
                assert ctx.dynamic_selection_mode is True

