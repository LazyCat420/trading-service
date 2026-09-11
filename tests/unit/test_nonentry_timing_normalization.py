from copy import deepcopy
import pytest
from app.v3.decision_contract import unique_nonentry_timing_correction, correction_errors

ORIGINAL={'action':'HOLD','confidence':72,'reasoning':'Unverified historical volume remains unknown.',
          'entry_mode':'enter_now','trigger_purpose':'none','dynamic_trigger':None,
          'position_size_pct':1.5,'stop_loss':95,'take_profit':115,
          'resolution_condition':None,'research_answers':[{'id':'history','answer':'unknown'}],
          'extra_evidence':{'source':'retained'}}

def test_unique_label_correction_preserves_every_other_field_and_input():
    before=deepcopy(ORIGINAL)
    patch=unique_nonentry_timing_correction(ORIGINAL)
    assert patch=={'entry_mode':'watch_only'}
    assert correction_errors(ORIGINAL,{**ORIGINAL,**patch})==[]
    assert ORIGINAL==before

def test_research_question_does_not_require_a_numeric_trigger():
    original={**ORIGINAL,'entry_mode':'watch_only','trigger_purpose':'research',
              'resolution_condition':{'open_question':'Missing historical volume','resolving_fact':'Dated observations'}}
    assert unique_nonentry_timing_correction(original)=={'trigger_purpose':'none'}

def test_existing_monitor_and_threshold_are_preserved():
    original={**ORIGINAL,'entry_mode':'enter_on_condition','trigger_purpose':'monitor',
              'dynamic_trigger':{'type':'price_below','value':100}}
    assert unique_nonentry_timing_correction(original)=={'entry_mode':'watch_only'}

def test_sell_labels_can_be_recovered_but_not_the_sell_decision_itself():
    assert unique_nonentry_timing_correction({'action':'SELL'})=={'entry_mode':'enter_now','trigger_purpose':'none'}
    assert unique_nonentry_timing_correction({'reasoning':'Maybe sell'}) is None

@pytest.mark.parametrize('original', [
    {**ORIGINAL,'action':'BUY'},
    {**ORIGINAL,'entry_mode':'watch_only'},
    {**ORIGINAL,'dynamic_trigger':{'type':'price_below','value':100}}, # monitor/research tie
    {**ORIGINAL,'trigger_purpose':'monitor','dynamic_trigger':{'type':'price_below','value':-1}},
])
def test_ambiguous_valid_or_unrepairable_input_is_not_normalized(original):
    assert unique_nonentry_timing_correction(original) is None

@pytest.mark.asyncio
@pytest.mark.parametrize('change_question', [False,True])
async def test_ambiguous_case_keeps_one_model_repair_and_strict_preservation(change_question):
    import json
    from types import SimpleNamespace
    from unittest.mock import AsyncMock,patch
    from app.v3 import data_trace
    from app.v3.shared_desk import SharedDesk
    from app.v3.agent_runner import run_v3_agent
    from app.v3.agents import board_of_directors as board
    original={**ORIGINAL,'entry_mode':'watch_only','dynamic_trigger':{'type':'price_below','value':100}}
    fixed={**original,'trigger_purpose':'monitor'}
    if change_question:fixed['resolution_condition']={'open_question':'new','resolving_fact':'new'}
    desk=SharedDesk(ticker='EVLT',cycle_id='nonentry-label-regression')
    desk.cycle_metadata={'decision_contract_version':1,'held':False}
    module=SimpleNamespace(AGENT_NAME=board.AGENT_NAME,ARTIFACT_TYPE=board.ARTIFACT_TYPE,
                           TOOL_WHITELIST=board.TOOL_WHITELIST,SYSTEM_PROMPT=board.PERSONA_JANE_STREET)
    responses=[{'response':json.dumps(a),'tokens_used':80,'loops_used':1,'stop_reason':'completed'} for a in (original,fixed)]
    with patch('app.agents.base_agent.run_agent',new_callable=AsyncMock,side_effect=responses) as model, \
         patch.object(data_trace.mongo_store,'insert_docs'),patch.object(data_trace.mongo_store,'update_docs'):
        outcome=await run_v3_agent(desk,module,cycle_id=desk.cycle_id,bot_id='test')
    assert model.await_count==2 and model.call_args_list[1].kwargs['enable_tools'] is False
    if change_question:
        assert outcome.value=='AGENT_ERROR' and not desk.final_decision
    else:
        assert desk.final_decision['trigger_purpose']=='monitor'
        assert desk.final_decision['resolution_condition'] is None
