"""A transport failure must back off durably without burning a six-hour window."""
import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch
import pytest
from app.db import mongo_store
from app.services.learning import consolidation as jobs
from app.services.memory import consolidator as c
pytestmark = pytest.mark.real_mongo

@pytest.mark.parametrize('failure', [RuntimeError('database blip'), ConnectionError('socket dropped')])
def test_failures_back_off_survive_reenqueue_and_retry(real_mongo, failure):
    now = datetime.now(timezone.utc)
    mongo_store.insert_docs('episodic_observations', [dict(id=str(i), ticker='NBIS', created_at=now, observation_text='recorded evidence', promoted_to_memory=False) for i in range(5)])
    jobs.enqueue('NBIS')
    call = AsyncMock(side_effect=failure)
    with patch.object(c, 'run_ticker_consolidation', call):
        first = asyncio.run(jobs.process_one())
        assert first['state'] == 'retry'
        row = mongo_store.find_docs('memory_consolidation_jobs', {})[0]
        assert now + timedelta(minutes=9) < row['retry_at'].replace(tzinfo=timezone.utc) < now + timedelta(minutes=11)
        jobs.enqueue('NBIS')  # a new cycle cannot erase backoff
        assert asyncio.run(jobs.process_one()) == {'state':'idle'}
        call.assert_awaited_once()
        mongo_store.update_docs('memory_consolidation_jobs', {'id':'NBIS'}, {'$set':{'retry_at':now-timedelta(seconds=1)}})
        assert asyncio.run(jobs.process_one())['state'] == 'retry'
        assert call.await_count == 2

def test_enqueue_has_no_untracked_llm_task(real_mongo):
    with patch.object(c, 'call_prism_agent', new_callable=AsyncMock) as call:
        asyncio.run(c.maybe_consolidate('NBIS'))
    call.assert_not_called()
    assert mongo_store.find_docs('memory_consolidation_jobs', {})[0]['state'] == 'pending'
