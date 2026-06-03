import asyncio
import logging
import time
import psutil
import os
import gc

from app.cognition.orchestration.runner import execute_v2_pipeline
from app.db.connection import get_db

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

async def memory_soak_test(iterations: int = 100):
    """
    Run N consecutive full cycles and measure memory usage over time to detect leaks.
    """
    logger.info(f"Starting memory soak test for {iterations} iterations...")
    process = psutil.Process(os.getpid())
    baseline_mem = process.memory_info().rss / 1024 / 1024

    logger.info(f"Baseline memory: {baseline_mem:.2f} MB")

    for i in range(iterations):
        ticker = "AAPL"
        cycle_id = f"soak-test-{i}"
        
        start_t = time.monotonic()
        try:
            # We mock DB and network where possible, or just let it run if against a test DB
            # For a pure memory soak of the python orchestration layer, we run the pipeline
            result = await execute_v2_pipeline(
                ticker=ticker,
                cycle_id=cycle_id,
                bot_id="audit_bot"
            )
            elapsed = time.monotonic() - start_t
            
            # Force garbage collection to measure true leak, not just lazy GC
            gc.collect()
            
            current_mem = process.memory_info().rss / 1024 / 1024
            growth = current_mem - baseline_mem
            logger.info(f"Iteration {i+1}/{iterations} completed in {elapsed:.1f}s. Memory: {current_mem:.2f} MB (Growth: {growth:+.2f} MB)")
            
            if growth > 500: # 500MB leak is severe
                logger.error(f"SEVERE MEMORY LEAK DETECTED: Growth reached {growth:.2f} MB after {i+1} iterations.")
                return False
                
        except Exception as e:
            logger.error(f"Iteration {i+1} failed: {e}")
            
    logger.info("Memory soak test completed.")
    return True

if __name__ == "__main__":
    asyncio.run(memory_soak_test())
