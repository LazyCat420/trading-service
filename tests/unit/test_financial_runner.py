"""Drive the real Board runner and final order guards with recorded-style calls."""
from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock,patch
import pytest
from app.v3 import data_trace
from app.v3.shared_desk import SharedDesk,PhaseOutcome
from app.v3.financial_claims import desk_status,execution_errors
from app.v3.financial_evidence import calculated_facts

CASES={c['id']:c for c in json.loads((Path(__file__).resolve().parents[1]/'benchmarks/fixtures/financial_reasoning_v1.json').read_text())['cases']}


def fixture():
    from app.v3.agents import board_of_directors as board
    record=deepcopy(CASES['headroom']);record['questions']=[]
    f=calculated_facts(record)['calc_headroom_pct']
    good={'action':'HOLD','confidence':72,'reasoning':'The proposal exceeds available headroom [calc_headroom_pct].',
          'position_size_pct':0,'entry_mode':'watch_only','trigger_purpose':'none','dynamic_trigger':None,
          'financial_claims':[{'fact_id':f['id'],**{k:f[k] for k in ('metric','value','unit','as_of','source')}}],
          'research_answers':[]}
    desk=SharedDesk(ticker='EVLT',cycle_id='bench-financial-runner')
    desk.cycle_metadata={'decision_contract_version':1,'financial_evidence_version':1,'held':True,
                         'financial_evidence_record':record}
    module=SimpleNamespace(AGENT_NAME=board.AGENT_NAME,ARTIFACT_TYPE=board.ARTIFACT_TYPE,
                           TOOL_WHITELIST=board.TOOL_WHITELIST,SYSTEM_PROMPT=board.PERSONA_JANE_STREET)
    return desk,module,good


async def run(desk,module,responses):
    from app.v3.agent_runner import run_v3_agent
    calls=[{'response':json.dumps(d),'tokens_used':80,'loops_used':1,'stop_reason':'completed'} for d in responses]
    with patch('app.agents.base_agent.run_agent',new_callable=AsyncMock,side_effect=calls) as model, \
         patch.object(data_trace.mongo_store,'insert_docs'),patch.object(data_trace.mongo_store,'update_docs'):
        outcome=await run_v3_agent(desk,module,cycle_id=desk.cycle_id,bot_id='test')
    return outcome,model


@pytest.mark.asyncio
async def test_real_runner_repairs_the_financial_conclusion_once_and_traces_evidence():
    desk,module,good=fixture()
    bad=deepcopy(good);bad.update(action='BUY',entry_mode='enter_now',position_size_pct=2.5)
    bad['financial_claims'][0]['value']=2.5
    with patch('app.v3.data_trace.record', wraps=data_trace.record) as trace:
        outcome,model=await run(desk,module,[bad,good])
    assert outcome in (PhaseOutcome.SUCCESS,PhaseOutcome.DATA_GAP)
    assert model.await_count==2
    assert model.call_args_list[1].kwargs['enable_tools'] is False
    assert 'FINANCIAL EVIDENCE RECONSIDERATION' in model.call_args_list[1].kwargs['user_prompt']
    assert 'reviewing an investment decision' in model.call_args_list[1].kwargs['system_prompt']
    assert 'FINANCIAL EVIDENCE CONTRACT v1' in model.call_args_list[0].kwargs['user_prompt']
    assert 'REQUIRED STRUCTURED FINANCIAL DECISION' in model.call_args_list[0].kwargs['system_prompt']
    assert 'Research agents: return research_answers=' not in model.call_args_list[0].kwargs['user_prompt']
    assert desk.final_decision['action']=='HOLD'
    assert desk_status(desk)['status']=='consistent'
    stages=[call.args[3] for call in trace.call_args_list if len(call.args)>3]
    assert {'financial.evidence','financial.correction','financial.validation'}<=set(stages)


@pytest.mark.asyncio
async def test_failed_repair_keeps_original_for_review_and_blocks_order_authority():
    from app.v3.orchestrator import _apply_policy_gates,_build_v1_compatible_result
    desk,module,good=fixture();bad=deepcopy(good)
    bad.update(action='BUY',entry_mode='enter_now',position_size_pct=2.5)
    bad['financial_claims'][0]['value']=2.5
    outcome,model=await run(desk,module,[bad,bad])
    assert model.await_count==2
    assert desk.final_decision['action']=='BUY'
    assert desk.final_decision['_financial_audit']['status']=='unresolved'
    assert _apply_policy_gates(desk)=='HOLD_POLICY_BLOCKED_FINANCIAL_EVIDENCE'
    result=_build_v1_compatible_result(desk)
    result['policy_action']='EXECUTE_BUY'  # bypassing a saved label cannot bypass the independent check
    assert execution_errors(result)


@pytest.mark.asyncio
async def test_financial_validation_does_not_add_a_retry_after_schema_repair():
    desk,module,good=fixture();bad=deepcopy(good);bad['financial_claims'][0]['value']=2.5
    outcome,model=await run(desk,module,[{'research_answers':[]},bad])
    assert model.await_count==2
    assert desk_status(desk)['status']=='unresolved'


@pytest.mark.asyncio
async def test_consistent_decision_needs_no_repair_and_rechecks_flattened_order_fields():
    from app.v3.orchestrator import _build_v1_compatible_result
    desk,module,good=fixture();outcome,model=await run(desk,module,[good])
    assert model.await_count==1
    result=_build_v1_compatible_result(desk)
    assert execution_errors(result)==[]
    result['estimate']['position_size_pct']=2.5
    assert execution_errors(result)
    result['financial_decision']['_financial_audit']={'status':'consistent'}
    result['financial_decision']['financial_claims'][0]['value']=2.5
    assert execution_errors(result)


def test_versioned_missing_record_is_not_a_legacy_decision():
    desk,_,good=fixture();desk.final_decision=good
    desk.cycle_metadata.pop('financial_evidence_record')
    assert desk_status(desk)['status']=='unresolved'
    assert execution_errors({'financial_evidence_version':1,'action':'BUY'})


@pytest.mark.asyncio
async def test_real_pipeline_dispatch_accepts_a_correct_control_and_blocks_a_forged_pass():
    from app.services.pipeline_service import PipelineService
    from app.v3.orchestrator import _build_v1_compatible_result
    desk,_,good=fixture()
    good.update(action='BUY',entry_mode='enter_now',position_size_pct=.5,confidence=90)
    desk.final_decision=good
    result=_build_v1_compatible_result(desk)
    result['policy_action']='EXECUTE_BUY'
    assert execution_errors(result)==[]
    with patch('app.services.pipeline_service.run_v3_pipeline',new_callable=AsyncMock) as pipeline, \
         patch('app.services.result_saver.save_analysis_result'), \
         patch('app.services.pipeline_state.PipelineStateDB.append_events'), \
         patch('app.services.pipeline_state.PipelineStateDB.save_state'), \
         patch('app.services.llm_preflight.llm_can_answer',new_callable=AsyncMock,return_value=(True,'mocked')), \
         patch('app.services.llm_preflight.tool_calls_are_parsed',new_callable=AsyncMock,return_value=(True,'mocked')), \
         patch('app.trading.paper_trader.buy',new_callable=AsyncMock) as buy, \
         patch('app.trading.paper_trader.sell',new_callable=AsyncMock) as sell:
        pipeline.return_value=deepcopy(result)
        await PipelineService._run_all_v3('test-financial-dispatch-good',['EVLT'])
        pipeline.assert_awaited_once()
        buy.assert_called_once()
        sell.assert_not_called()
        buy.reset_mock();pipeline.reset_mock()
        bad=deepcopy(result)
        bad['financial_decision']['financial_claims'][0]['value']=2.5
        bad['financial_decision']['_financial_audit']={'status':'consistent'}
        pipeline.return_value=bad
        await PipelineService._run_all_v3('test-financial-dispatch-bad',['EVLT'])
        pipeline.assert_awaited_once()
        buy.assert_not_called()
        sell.assert_not_called()
        assert bad['no_trade_reason']=='HOLD_POLICY_BLOCKED_FINANCIAL_EVIDENCE'


@pytest.mark.asyncio
@pytest.mark.parametrize('bad', ['{"financial_claims":[', json.dumps({'financial_claims':[], 'decision':{'action':'HOLD'}})])
async def test_toolless_evidence_board_repairs_truncation_and_nested_decision_once(bad):
    from app.v3.agent_runner import run_v3_agent
    desk,module,good=fixture();module.TOOL_WHITELIST=[]
    with patch('app.agents.base_agent.run_agent',new_callable=AsyncMock,side_effect=[
        {'response':bad,'tokens_used':10,'loops_used':1},
        {'response':json.dumps(good),'tokens_used':20,'loops_used':1},
    ]) as model, patch.object(data_trace.mongo_store,'insert_docs'), patch.object(data_trace.mongo_store,'update_docs'):
        outcome=await run_v3_agent(desk,module,cycle_id=desk.cycle_id,bot_id='test')
    assert model.await_count==2
    assert all(c.kwargs['enable_tools'] is False for c in model.call_args_list)
    assert outcome in (PhaseOutcome.SUCCESS,PhaseOutcome.DATA_GAP)
    assert desk_status(desk)['status']=='consistent'


@pytest.mark.asyncio
async def test_model_fact_references_survive_runner_and_revalidate_at_execution():
    from app.v3.orchestrator import _build_v1_compatible_result
    desk,module,good=fixture()
    good.update(action='BUY',entry_mode='enter_now',position_size_pct=.2,
                reasoning='The smaller addition respects the available capacity [calc_headroom_pct].')
    good['financial_claims']=['calc_headroom_pct']
    outcome,model=await run(desk,module,[good])
    assert model.await_count==1
    assert desk.final_decision['_financial_claim_reference_ids']==['calc_headroom_pct']
    assert desk.final_decision['financial_claims'][0]['value']==.6
    assert desk_status(desk)['status']=='consistent'
    result=_build_v1_compatible_result(desk)
    assert execution_errors(result)==[]
    result['financial_decision']['financial_claims'][0]['value']=2.5
    assert execution_errors(result)


@pytest.mark.asyncio
async def test_financial_toolless_repair_still_refuses_unexecuted_tool_calls():
    from app.v3.agent_runner import run_v3_agent
    from app.v3.output_rules import classify_output
    desk,module,_=fixture();module.TOOL_WHITELIST=[]
    raw='<tool_call>{"name":"get_sec_filings","arguments":{"ticker":"EVLT"}}</tool_call>'
    assert classify_output(raw).transport_failure
    with patch('app.agents.base_agent.run_agent',new_callable=AsyncMock,return_value={'response':raw,'tokens_used':10,'loops_used':1}) as model, \
         patch.object(data_trace.mongo_store,'insert_docs'),patch.object(data_trace.mongo_store,'update_docs'):
        outcome=await run_v3_agent(desk,module,cycle_id=desk.cycle_id,bot_id='test')
    assert model.await_count==1
    assert outcome==PhaseOutcome.AGENT_ERROR
    assert desk.final_decision is None


@pytest.mark.asyncio
async def test_structured_model_selection_renders_before_schema_and_execution_checks():
    from app.v3.orchestrator import _build_v1_compatible_result
    desk,module,_=fixture();module.TOOL_WHITELIST=[]
    raw={'financial_reasoning_version':2,'action':'BUY','confidence':72,'position_size_pct':.2,
         'entry_mode':'enter_now','trigger_purpose':'none','dynamic_trigger':None,'resolution_condition':None,
         'reasoning_steps':['headroom','proposal_fit'],'research_answers':[]}
    outcome,model=await run(desk,module,[raw])
    assert model.await_count==1
    assert desk.final_decision['action']=='BUY' and desk.final_decision['position_size_pct']==.2
    assert desk_status(desk)['status']=='consistent'
    assert desk.final_decision['_financial_explanation_provenance']=='model_selected_steps_code_rendered_statements'
    result=_build_v1_compatible_result(desk)
    assert execution_errors(result)==[]
    result['financial_decision']['reasoning']='The original supplied purchase fits.'
    assert execution_errors(result)


@pytest.mark.asyncio
@pytest.mark.parametrize("repair_valid", [True, False])
async def test_unknown_structured_selections_receive_precise_bounded_repair(repair_valid):
    desk,module,_=fixture();module.TOOL_WHITELIST=[]
    good={'financial_reasoning_version':2,'action':'HOLD','confidence':72,'position_size_pct':0,
          'entry_mode':'watch_only','trigger_purpose':'none','dynamic_trigger':None,
          'resolution_condition':None,'reasoning_steps':['headroom'],'research_answers':[]}
    bad=deepcopy(good);bad['reasoning_steps']=['invented_financial_step']
    outcome,model=await run(desk,module,[bad,good if repair_valid else bad])
    assert model.await_count==2
    prompt=model.call_args_list[1].kwargs['user_prompt']
    assert 'unknown step IDs' in prompt and 'invented_financial_step' in prompt
    assert 'Required top-level decision keys: action, confidence, reasoning.' not in prompt
    assert 'reviewing an investment decision' in model.call_args_list[1].kwargs['system_prompt']
    if repair_valid:
        assert desk_status(desk)['status']=='consistent'
        assert desk.final_decision['action']=='HOLD'
    else:
        assert outcome==PhaseOutcome.AGENT_ERROR
        assert desk.final_decision is None


@pytest.mark.asyncio
async def test_financial_schema_repair_does_not_request_conflicting_authored_prose():
    desk,module,_=fixture();module.TOOL_WHITELIST=[]
    good={'financial_reasoning_version':2,'action':'HOLD','confidence':72,'position_size_pct':0,
          'entry_mode':'watch_only','trigger_purpose':'none','dynamic_trigger':None,
          'resolution_condition':None,'reasoning_steps':['headroom'],'research_answers':[]}
    outcome,model=await run(desk,module,[{'research_answers':[]},good])
    assert model.await_count==2
    prompt=model.call_args_list[1].kwargs['user_prompt']
    assert 'FINANCIAL EVIDENCE RECONSIDERATION' in prompt
    assert 'Do not emit financial_claims or authored reasoning/answer prose.' in prompt
    assert 'Required top-level decision keys: action, confidence, reasoning.' not in prompt
    assert 'reviewing an investment decision' in model.call_args_list[1].kwargs['system_prompt']
    assert desk_status(desk)['status']=='consistent'
