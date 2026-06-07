import asyncio
import pytest
from unittest.mock import MagicMock, patch, AsyncMock
from app.cycle.phases.phase4_analysis import run_phase4_analysis
from app.cycle.context import CycleContext
from app.config import settings

@pytest.mark.asyncio
async def test_worker_pool_spawns_correct_number_of_workers():
    """Verify that run_phase4_analysis creates worker tasks and processes batch queue."""
    ctx = CycleContext(
        cycle_id="test-cycle",
        tickers=["AAPL", "MSFT", "GOOG"],
        collect=True,
        analyze=True,
        trade=True,
    )
    
    mock_results = [
        {"ticker": "AAPL", "action": "BUY", "confidence": 80},
        {"ticker": "MSFT", "action": "HOLD", "confidence": 50},
        {"ticker": "GOOG", "action": "SELL", "confidence": 90},
    ]
    
    # We mock execute_v2_pipeline to return immediately with our mock results
    async def mock_execute(ticker, *args, **kwargs):
        for res in mock_results:
            if res["ticker"] == ticker:
                return res
        return {"ticker": ticker, "action": "HOLD", "confidence": 0}
        
    with patch("app.cognition.orchestration.runner.execute_v2_pipeline", side_effect=mock_execute), \
         patch("app.cycle.phases.phase4_analysis.settings") as mock_settings, \
         patch("app.services.ticker_report_generator.report_generator.save_ticker_report") as mock_save_report:
         
        mock_settings.V2_TICKER_CONCURRENCY = 2
        mock_settings.ANALYSIS_WORKER_TIMEOUT_SECONDS = 5.0
        
        # Run in batch mode (no analysis_queue passed)
        results = await run_phase4_analysis(
            ctx=ctx,
            bot_id="test-bot",
            macro_memo="mock macro",
            emit=lambda *args, **kwargs: None,
            cycle_summary={},
            state={"triage": {}},
            analysis_queue=None,
        )
        
        assert len(results) == 3
        # Ensure AAPL, MSFT, GOOG results are present
        tickers = {r["ticker"] for r in results}
        assert tickers == {"AAPL", "MSFT", "GOOG"}

@pytest.mark.asyncio
async def test_worker_pool_sentinel_shutdown():
    """Verify that a None sentinel in the queue shuts down a worker cleanly."""
    ctx = CycleContext(
        cycle_id="test-cycle-sentinel",
        tickers=["AAPL", "MSFT"],
        collect=True,
        analyze=True,
        trade=True,
    )
    
    # Set up queue mode
    queue = asyncio.Queue()
    await queue.put("AAPL")
    await queue.put(None)  # Sentinel puts immediately
    
    mock_result = {"ticker": "AAPL", "action": "BUY", "confidence": 90}
    
    with patch("app.cognition.orchestration.runner.execute_v2_pipeline", return_value=mock_result), \
         patch("app.cycle.phases.phase4_analysis.settings") as mock_settings, \
         patch("app.services.ticker_report_generator.report_generator.save_ticker_report") as mock_save_report:
         
        mock_settings.V2_TICKER_CONCURRENCY = 1
        mock_settings.ANALYSIS_WORKER_TIMEOUT_SECONDS = 5.0
        
        results = await run_phase4_analysis(
            ctx=ctx,
            bot_id="test-bot",
            macro_memo="mock macro",
            emit=lambda *args, **kwargs: None,
            cycle_summary={},
            state={"triage": {}},
            analysis_queue=queue,
        )
        
        # Worker shuts down after AAPL because next is sentinel, MSFT is not in queue
        assert len(results) == 1
        assert results[0]["ticker"] == "AAPL"
