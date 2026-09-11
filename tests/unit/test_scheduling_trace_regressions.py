from copy import deepcopy
from datetime import datetime, timezone, timedelta
import json
from unittest.mock import patch, AsyncMock
import pytest
from app.services import research_admission as admission
from app.v3 import data_trace
from app.v3.decision_contract import correction_errors, contract_errors

NOW = datetime(2026,9,10,15,tzinfo=timezone.utc)

@pytest.fixture
def admission_state():
    params = {'MAX_WATCH_WAKES_PER_DAY':6,'WATCH_MIN_REANALYSIS_H':12,'WATCH_MAX_ANALYSES_PER_WEEK':3}
    with patch.object(admission,'get_param',side_effect=params.__getitem__), \
         patch('app.services.watch_desk._wakes_today',return_value=2), \
         patch('app.services.watch_desk._human_stop_cooldown_active',return_value=False), \
         patch.object(admission,'nonwatch_starts_today',return_value=4), \
         patch.object(admission.mongo_store,'find_docs',return_value=[]) as find:
        yield find

def test_orders_share_watch_budget_and_risk_reviews_stay_available(admission_state):
    denied = admission.admit(['TEST'], {'trigger_type':'edge_case_dynamic'},NOW)
    assert not denied['allowed'] and denied['reason']=='global_daily_budget_exhausted'
    assert denied['used_today']==6
    assert admission.admit(['TEST'],{'trigger_type':'edge_case_stop_loss'},NOW)['budget_exempt']
    assert admission.admit(['TEST'],{},NOW)['reason']=='explicit_request'

def test_watch_reservation_is_not_charged_twice(admission_state):
    assert admission.admit(['TEST'],{'watch_wake':True},NOW)['allowed']

def test_order_reanalysis_obeys_the_same_ticker_cadence(admission_state):
    admission_state.return_value=[{'cycle_id':'earlier','created_at':NOW-timedelta(hours=1)}]
    with patch.object(admission,'nonwatch_starts_today',return_value=0):
        verdict=admission.admit(['TEST'],{'trigger_type':'edge_case_dynamic'},NOW)
    assert verdict['reason']=='min_reanalysis_interval' and not verdict['allowed']

@pytest.mark.asyncio
@pytest.mark.parametrize('market,age', [('closed',0.01),('open',2),('open',None),('open',-1)])
async def test_stale_or_closed_price_cannot_wake_discretionary_research(market,age):
    from app.trading import order_triggers as ot
    row=('id','TEST','dynamic',100,'BUY',1,None,None,'enter below 100','price_below',100)
    with patch.object(ot,'_expire_stale_dynamic_triggers'),patch.object(ot,'retire_inert_dynamic_triggers'), \
         patch.object(ot.mongo_query,'find_rows',return_value=[row]), \
         patch.object(ot,'_get_current_price',return_value=(90,age)),          patch.object(ot.mongo_store,'update_docs'), \
         patch('app.services.market_calendar.MarketCalendar.get_market_state',return_value=market), \
         patch('app.services.pipeline_service.PipelineService.start_cycle',new_callable=AsyncMock) as start:
        assert await ot.check_triggers('paper')==[]
        start.assert_not_called()

def test_board_persona_examples_are_valid_contracts():
    from app.v3.agents.board_of_directors import PERSONA_MAP
    for persona in PERSONA_MAP.values():
        example=json.loads(persona[persona.index('\n{',persona.index('## OUTPUT')):])
        assert contract_errors(example)==[]
        assert example['resolution_condition'] is None

def test_contract_correction_preserves_the_decision():
    original={'action':'HOLD','confidence':65,'reasoning':'Margin evidence remains insufficient to commit capital.',
              'position_size_pct':0,'dynamic_trigger':None}
    fixed={**original,'entry_mode':'watch_only','trigger_purpose':'none','resolution_condition':None}
    assert correction_errors(original,fixed)==[]
    assert 'correction changed action' in correction_errors(original,{**fixed,'action':'BUY'})
    assert 'correction changed confidence' in correction_errors(original,{**fixed,'confidence':80})

def test_snapshots_preserve_public_data_omit_private_content_and_do_not_mutate():
    value={'analysis':{'revenue':12},'reasoning':'Public decision rationale',
           'reasoning_content':'private deliberation','api_key':'secret',
           'output':'<think>private</think>{"action":"HOLD"}'}
    before=deepcopy(value)
    blob=data_trace.snapshot(value)
    content=json.loads(blob['content'])
    assert content['analysis']=={'revenue':12}
    assert content['reasoning']=='Public decision rationale'
    assert 'reasoning_content' not in content and content['api_key']=='[redacted]'
    assert '<think>' not in content['output'] and 'HOLD' in content['output']
    assert value==before
    assert blob['hash']==data_trace.snapshot(value)['hash']
    assert data_trace.snapshot('x'*(data_trace.MAX_BYTES+100))['truncated']

def test_trace_spans_and_immutable_snapshots_join_by_cycle():
    writes=[]
    with patch.object(data_trace.mongo_store,'insert_docs',side_effect=lambda c,docs:writes.extend(docs)), \
         patch.object(data_trace.mongo_store,'update_docs') as blob:
        data_trace.record('cycle-test','','scheduler','cycle.admission',data={'reason':'verified event'})
        with data_trace.scope('cycle-test','TEST','board',attempt=1):
            data_trace.record('cycle-test','TEST','board','artifact.parsed',data={'action':'HOLD'})
        assert data_trace.current_span() is None
    assert writes[1]['parent_span_id']==writes[0]['span_id']
    assert writes[2]['parent_span_id']==writes[1]['span_id']
    assert len({r['trace_id'] for r in writes})==1
    assert all(len(r['span_id'])==16 for r in writes)
    assert blob.call_args.kwargs['upsert']
    assert '$setOnInsert' in blob.call_args.args[2]
    assert 'last_referenced_at' in blob.call_args.args[2]['$max']

@pytest.mark.asyncio
async def test_board_normalizes_unique_hold_labels_without_regenerating_decision():
    from app.v3.shared_desk import SharedDesk, PhaseOutcome
    from app.v3.agent_runner import run_v3_agent
    from app.v3.agents import board_of_directors as board
    from types import SimpleNamespace
    desk=SharedDesk(ticker='TEST',cycle_id='board-contract-correction')
    desk.cycle_metadata={'decision_contract_version':1,'held':False,
                         'data_report':'Verified operating margin is twelve percent.'}
    original={'action':'HOLD','confidence':63,'reasoning':'The operating margin does not resolve the valuation uncertainty.',
              'position_size_pct':0,'dynamic_trigger':None}
    fixed={**original,'entry_mode':'watch_only','trigger_purpose':'none','resolution_condition':None}
    module=SimpleNamespace(AGENT_NAME=board.AGENT_NAME,ARTIFACT_TYPE=board.ARTIFACT_TYPE,
                           TOOL_WHITELIST=board.TOOL_WHITELIST,SYSTEM_PROMPT=board.PERSONA_JANE_STREET)
    responses=[{'response':json.dumps(value),'tokens_used':80,'loops_used':1,'stop_reason':'completed'}
               for value in (original,fixed)]
    with patch('app.agents.base_agent.run_agent',new_callable=AsyncMock,side_effect=responses) as model, \
         patch.object(data_trace.mongo_store,'update_docs'),patch.object(data_trace.mongo_store,'insert_docs'), \
         patch('app.v3.agent_runner.trace_data') as trace:
        outcome=await run_v3_agent(desk,module,cycle_id=desk.cycle_id,bot_id='test')
    assert outcome in (PhaseOutcome.SUCCESS,PhaseOutcome.DATA_GAP)
    assert model.await_count==1
    assert desk.final_decision['action']=='HOLD'
    assert desk.final_decision['confidence']==63
    assert desk.final_decision['entry_mode']=='watch_only'

    event=next(call for call in trace.call_args_list if call.args[3]=='artifact.timing_normalized')
    payload=event.kwargs['data']
    assert payload['rule']=='unique_minimal_nonentry_labels'
    assert payload['original']['reasoning']==original['reasoning']
    assert payload['normalized']['reasoning']==original['reasoning']
    assert payload['patch']=={'entry_mode':'watch_only','trigger_purpose':'none'}


def test_earnings_estimates_and_wrong_event_do_not_prove_release():
    from app.services.event_readiness import reported_actual
    assert reported_actual([{'symbol':'TEST','date':'2026-09-10','epsEstimate':2}], 'TEST','2026-09-10') is None
    assert reported_actual([{'symbol':'TEST','date':'2026-06-10','epsActual':2}], 'TEST','2026-09-10') is None
    assert reported_actual([{'symbol':'TEST','date':'2026-09-10','epsActual':0}], 'TEST','2026-09-10')['epsActual']==0
    assert reported_actual([{'symbol':'TEST','date':'2026-09-10','epsActual':float('nan')}], 'TEST','2026-09-10') is None


def test_otlp_export_uses_measured_durations_and_standard_wire_types():
    events=[{'id':'one','cycle_id':'cycle','trace_id':'a'*32,'span_id':'b'*16,
             'stage':'agent.start','created_at':NOW,'agent':'board','ticker':'TEST'},
            {'id':'two','cycle_id':'cycle','trace_id':'a'*32,'span_id':'c'*16,
             'parent_span_id':'b'*16,'stage':'agent.end','created_at':NOW+timedelta(seconds=3)}]
    spans=data_trace.otlp_export(events)['resourceSpans'][0]['scopeSpans'][0]['spans']
    assert int(spans[0]['endTimeUnixNano'])-int(spans[0]['startTimeUnixNano'])==3_000_000_000
    assert spans[1]['parentSpanId']==spans[0]['spanId']
    assert spans[1]['startTimeUnixNano']==spans[1]['endTimeUnixNano']
    assert spans[0]['kind']==1


def test_deferred_one_shot_is_persisted_and_retry_is_bounded_by_expiry():
    from app.services.cycle_scheduler import SchedulerService, scheduler
    from datetime import datetime, timezone, timedelta
    now = datetime.now(timezone.utc)
    schedule = {'id':'retry-one-shot','schedule_type':'once','is_active':False,
                'expiry_at':now+timedelta(minutes=5)}
    with patch.object(data_trace.mongo_store,'find_docs',return_value=[schedule]), \
         patch.object(data_trace.mongo_store,'update_docs') as update, \
         patch.object(scheduler,'add_job') as add:
        SchedulerService.rearm_deferred_command({'schedule_id':schedule['id']}, {'status':'deferred'})
    saved = update.call_args.args[2]['$set']
    assert saved['is_active'] is True
    assert saved['run_at'] == saved['next_run_at'] == schedule['expiry_at']
    assert add.call_args.kwargs['trigger'].run_date == schedule['expiry_at']
    assert update.call_args_list[0].args[2]['$set']['last_status'] == 'deferred'
