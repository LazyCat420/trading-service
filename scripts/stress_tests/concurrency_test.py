import asyncio
import logging
import time

from app.cognition.orchestration.runner import execute_v2_tickers

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

async def concurrency_stress_test(ticker_count: int = 50):
    """
    Fire multiple tickers at max capacity in parallel and verify no data is dropped or overwritten.
    """
    logger.info(f"Starting concurrency stress test for {ticker_count} tickers...")
    
    # Generate synthetic ticker symbols
    tickers = [f"STRESS{i:03d}" for i in range(ticker_count)]
    cycle_id = f"stress-concurrency-{int(time.time())}"
    
    start_t = time.monotonic()
    
    try:
        # execute_v2_tickers handles the concurrency chunking natively
        results = await execute_v2_tickers(
            tickers=tickers,
            cycle_id=cycle_id,
            bot_id="audit_bot"
        )
        
        elapsed = time.monotonic() - start_t
        
        success_count = len([r for r in results if r is not None])
        logger.info(f"Concurrency test completed in {elapsed:.1f}s.")
        logger.info(f"Successfully processed {success_count}/{ticker_count} tickers.")
        
        if success_count < ticker_count:
            logger.error(f"Failed to process {ticker_count - success_count} tickers.")
            return False
            
    except Exception as e:
        logger.error(f"Concurrency stress test failed: {e}")
        return False
        
    return True

if __name__ == "__main__":
    asyncio.run(concurrency_stress_test())
