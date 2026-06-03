import asyncio
import logging
import time
from unittest.mock import patch

from app.cognition.orchestration.runner import execute_v2_pipeline

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

async def simulate_slow_db_test():
    """
    Simulate PostgreSQL responding in 5-10 seconds and verify the cycle does not hang or crash.
    """
    logger.info("Starting slow DB simulation test...")
    
    # We will mock the core db connection fetch or the decision logging to sleep
    original_sleep = asyncio.sleep
    
    async def slow_db_execute(*args, **kwargs):
        logger.info("Simulating SLOW DB response (7 seconds)...")
        await asyncio.sleep(7)
        return {"status": "mock_success"}
        
    try:
        # Patching a common DB write function used in the pipeline
        with patch('app.pipeline.analysis.decision_engine._log_decision', new=slow_db_execute):
            start_t = time.monotonic()
            
            result = await execute_v2_pipeline(
                ticker="SLOW",
                cycle_id="stress-slow-db",
                bot_id="audit_bot"
            )
            
            elapsed = time.monotonic() - start_t
            logger.info(f"Slow DB test completed in {elapsed:.1f}s.")
            
            # The test passes if it didn't crash
            if result is not None:
                logger.info("Test PASSED: Pipeline recovered from slow DB response.")
                return True
            else:
                logger.error("Test FAILED: Pipeline returned None (crashed or aborted).")
                return False
                
    except Exception as e:
        logger.error(f"Slow DB test failed with exception: {e}")
        return False

if __name__ == "__main__":
    asyncio.run(simulate_slow_db_test())
