import json
from copy import deepcopy
from unittest.mock import AsyncMock, patch
import pytest
from app.v3.board_evidence import regime_packet
from app.v3.shared_desk import SharedDesk


def regime():
    return {'regime':'CONTRADICTORY','confidence':64,'rationale':'Conflicting sources remain unresolved.',
            'board_directive':'Do not treat repeated vendor claims as independent evidence. Preserve risk DELTA-73.',
            'factors':{'volatility':0.6},'market_context_tags':['uncertain'],
            'additional_evidence':{'source':'source-42','unknown_value':None}}


def test_long_research_cannot_swallow_regime_and_source_is_lossless():
    source=regime();original=deepcopy(source)
    desk=SharedDesk(ticker='TEST',cycle_id='test',desk_note={'summary':'research '*3000},regime_classification=source)
    old=desk.get_compressed_context(True)
    assert source['board_directive'] not in old
    context=desk.get_compressed_context(True,include_regime=False)
    packet,receipt=regime_packet(source)
    assert json.loads(packet.split('\n')[-1])==original
    assert (packet+'\n'+context).count(source['board_directive'])==1
    assert receipt['complete'] and receipt['directive_present']
    assert source==original


def test_short_context_does_not_duplicate_regime_and_default_consumers_keep_it():
    desk=SharedDesk(ticker='TEST',cycle_id='test',regime_classification=regime())
    assert regime()['board_directive'] in desk.get_compressed_context()
    assert regime()['board_directive'] not in desk.get_compressed_context(include_regime=False)


@pytest.mark.parametrize('source',[None,{},'missing'])
def test_unavailable_regime_remains_unknown(source):
    text,receipt=regime_packet(source)
    assert 'unknown' in text and not receipt['available']


def test_delivery_receipt_changes_with_evidence():
    source=regime();_,before=regime_packet(source)
    source['board_directive']='New evidence changes the directive.'
    _,after=regime_packet(source)
    assert before['sha256']!=after['sha256']


@pytest.mark.asyncio
async def test_actual_board_runner_delivers_directive_once_before_model_call():
    from types import SimpleNamespace
    from app.v3.agent_runner import run_v3_agent
    from app.v3.agents import board_of_directors as board
    source=regime()
    desk=SharedDesk(ticker='TEST',cycle_id='test-board-delivery',desk_note={'summary':'research '*3000},
                    regime_classification=source)
    module=SimpleNamespace(AGENT_NAME=board.AGENT_NAME,ARTIFACT_TYPE=board.ARTIFACT_TYPE,
                           TOOL_WHITELIST=board.TOOL_WHITELIST,SYSTEM_PROMPT=board.get_persona_prompt('CONTRADICTORY'))
    response={'response':json.dumps({'action':'HOLD','confidence':62,'reasoning':'Material source conflict remains unresolved.'}),
              'tokens_used':80,'loops_used':1,'stop_reason':'completed'}
    with patch('app.agents.base_agent.run_agent',new_callable=AsyncMock,return_value=response) as model, \
         patch('app.v3.data_trace.record',return_value='test-trace') as trace:
        await run_v3_agent(desk,module,cycle_id=desk.cycle_id,bot_id='test',include_debate_context=True)
    first=model.call_args_list[0].kwargs
    assert first['user_prompt'].count(source['board_directive'])==1
    assert source['board_directive'] not in first['system_prompt']
    assert any(len(c.args)>3 and c.args[3]=='board.regime_delivery' for c in trace.call_args_list)
