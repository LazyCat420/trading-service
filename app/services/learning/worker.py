"""Independent bounded receipt/index and consolidation workers.

Slow consolidation never holds final-artifact receipts behind an LLM call.
"""
import asyncio
import logging
import time
from app.services.learning import consolidation, health, records, receipts
from app.services.learning.policy import enabled
logger = logging.getLogger(__name__)

async def pause(shutdown, seconds=30):
    try:
        await asyncio.wait_for(shutdown.wait(), timeout=seconds)
    except asyncio.TimeoutError:
        pass

async def delivery_loop(shutdown):
    while not shutdown.is_set():
        try:
            delivery = await asyncio.to_thread(receipts.deliver_pending)
            health.record('artifact_receipts', 'failed' if delivery['failed'] else 'ready', **delivery)
            if enabled('indexing'):
                index = await asyncio.to_thread(records.index_pending, 3)
                health.record('lesson_index', 'failed' if index['failed'] else 'ready', **index)
        except Exception as exc:
            logger.exception('[LearningWorker] delivery failed')
            health.record('delivery_worker', 'failed', error=str(exc)[:500])
        await pause(shutdown)

async def consolidation_loop(shutdown):
    next_discovery = 0.0
    while not shutdown.is_set():
        try:
            if time.monotonic() >= next_discovery:
                backlog = await asyncio.to_thread(consolidation.discover)
                health.record('memory_backlog', 'pending' if backlog['eligible_tickers'] else 'ready', **backlog)
                next_discovery = time.monotonic() + 300
            if enabled('consolidation'):
                await consolidation.process_one()
        except Exception as exc:
            logger.exception('[LearningWorker] consolidation failed')
            health.record('consolidation_worker', 'failed', error=str(exc)[:500])
        await pause(shutdown)

async def run(shutdown: asyncio.Event):
    tasks = [asyncio.create_task(delivery_loop(shutdown)), asyncio.create_task(consolidation_loop(shutdown))]
    try:
        await asyncio.gather(*tasks)
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
