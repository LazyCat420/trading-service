from datetime import datetime, timedelta, timezone
from unittest.mock import patch
import pytest
from app.autoresearch.trace_evidence import trace_quality_window
from app.autoresearch.trace_writer import write_agent_trace, confirm_agent_trace_model
from app.autoresearch.eval_engine import process_pending_traces
from app.db.collections import collection_for
from app.v3 import data_trace

pytestmark = pytest.mark.real_mongo


def test_grading_old_glm_today_does_not_make_it_recent(real_mongo):
    now = datetime.now(timezone.utc)
    traces = real_mongo[collection_for('agent_traces')]
    grades = real_mongo[collection_for('eval_scores')]
    traces.insert_many([
        {'id':'old','run_id':'old-cycle','created_at':now-timedelta(days=9),'model_name':'GLM'},
        {'id':'recent','run_id':'current','created_at':now-timedelta(hours=2),'model_name':'Nemotron'},
        {'id':'pending','run_id':'current','created_at':now-timedelta(hours=1),'model_name':'Nemotron'},
    ])
    grades.insert_many([
        {'id':'old-score','run_id':'old','created_at':now,'final_score':100},
        {'id':'recent-score','run_id':'recent','created_at':now,'final_score':60},
    ])
    evidence = trace_quality_window(now=now)
    assert evidence['trace_count'] == 2
    assert evidence['mean_score'] == 60
    assert evidence['pending_count'] == 1
    assert evidence['models'][0]['model'] == 'Nemotron'
    assert evidence['models'][0]['attribution'] == 'legacy_unverified'
    historical = trace_quality_window(now=now,cycle_id='old-cycle')
    assert historical['models'][0]['max_grading_delay_hours'] == 216


def test_only_response_identity_can_confirm_exact_attempt(real_mongo):
    with patch.object(data_trace,'record'):
        for cycle,attempt in [('current','attempt-a'),('current','attempt-b'),('other','attempt-a')]:
            write_agent_trace(cycle,'TEST','board','read',{},'ok',False,1,
                              model_name='requested-GLM',endpoint_name='vllm-2',agent_attempt_id=attempt)
        rows = list(real_mongo[collection_for('agent_traces')].find({}))
        assert all(r['model_name'] is None and r['requested_model']=='requested-GLM' for r in rows)
        confirm_agent_trace_model('current','attempt-a',None,None)
        assert real_mongo[collection_for('agent_traces')].count_documents({'model_attribution':'unconfirmed'}) == 3
        confirm_agent_trace_model('current','attempt-a','Nemotron','vllm')
    confirmed = list(real_mongo[collection_for('agent_traces')].find({'model_attribution':'response_metadata'}))
    assert len(confirmed) == 1
    assert confirmed[0]['model_name'] == 'Nemotron'
    assert confirmed[0]['endpoint_name'] == 'vllm'
    assert confirmed[0]['requested_model'] == 'requested-GLM'
    assert trace_quality_window(cycle_id='other')['models'][0]['max_grading_delay_hours'] is None


def test_grade_retains_execution_timestamp_and_prioritizes_current_cycle(real_mongo):
    old = datetime.now(timezone.utc)-timedelta(days=2)
    traces=real_mongo[collection_for('agent_traces')]
    traces.insert_many([
        {'id':'old','run_id':'old-cycle','created_at':old-timedelta(days=1),'stop_reason':'completed'},
        {'id':'current','run_id':'current-cycle','created_at':old,'stop_reason':'completed',
         'model_name':'Nemotron','endpoint_name':'vllm','model_attribution':'response_metadata'},
    ])
    assert process_pending_traces(limit=1,cycle_id='current-cycle') == 1
    row=real_mongo[collection_for('eval_scores')].find_one({'run_id':'current'})
    assert row['cycle_id'] == 'current-cycle'
    assert row['trace_created_at'].replace(tzinfo=timezone.utc) < old+timedelta(seconds=1)
    assert row['created_at'] > row['trace_created_at']
    assert row['model_name'] == 'Nemotron'
    assert real_mongo[collection_for('eval_scores')].count_documents({'run_id':'old'}) == 0
