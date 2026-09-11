"""Real Mongo verification of snapshot immutability, retention and scoped replay reads."""
import json
from datetime import datetime, timezone, timedelta
from unittest.mock import patch
import pytest
from fastapi import HTTPException
from app.db import mongo_store
from app.v3 import data_trace
from app.routers import cycle_replay_router as replay

pytestmark = pytest.mark.real_mongo

@pytest.mark.asyncio
async def test_content_dedup_retention_and_cycle_scoped_replay(real_mongo):
    old = datetime.now(timezone.utc)-timedelta(days=29)
    payload = {'action':'HOLD','evidence':'verified fixture'}
    blob = data_trace.snapshot(payload)
    real_mongo.pipeline_trace_blobs.insert_one({'_id':blob['hash'], **blob,
        'created_at':old,'last_referenced_at':old})
    data_trace.record('trace-a','TEST','board','artifact.accepted',data=payload)
    data_trace.record('trace-b','TEST','board','artifact.accepted',data=payload)
    stored = real_mongo.pipeline_trace_blobs.find_one({'_id':blob['hash']})
    assert real_mongo.pipeline_trace_blobs.count_documents({}) == 1
    assert stored['created_at'].replace(tzinfo=timezone.utc) < old+timedelta(seconds=1)
    assert stored['last_referenced_at'].replace(tzinfo=timezone.utc) > old+timedelta(days=28)
    assert stored['content'] == blob['content']
    assert (await replay.trace_blob('trace-a',blob['hash']))['content'] == blob['content']
    with pytest.raises(HTTPException) as error:
        await replay.trace_blob('unrelated-cycle',blob['hash'])
    assert error.value.status_code == 404
    page = await replay.data_trace('trace-a',ticker='',limit=1,offset=0)
    assert len(page['events']) == 1 and not page['has_more']
    exported = json.loads((await replay.trace_export('trace-a')).body)
    assert len(exported['resourceSpans'][0]['scopeSpans'][0]['spans']) == 1


def test_legacy_snapshot_index_migrates_to_reference_retention(real_mongo):
    old = datetime.now(timezone.utc)-timedelta(days=2)
    real_mongo.pipeline_trace_blobs.insert_one({'_id':'legacy','created_at':old})
    real_mongo.pipeline_trace_blobs.create_index('created_at',expireAfterSeconds=31*86400)
    with patch.object(mongo_store,'_indexes_ready',False):
        mongo_store.ensure_indexes()
    indexes = real_mongo.pipeline_trace_blobs.index_information()
    assert not any(i.get('key') == [('created_at',1)] and 'expireAfterSeconds' in i for i in indexes.values())
    assert indexes['last_referenced_at_1']['expireAfterSeconds'] == 31*86400
    row = real_mongo.pipeline_trace_blobs.find_one({'_id':'legacy'})
    assert row['last_referenced_at'] == row['created_at']
