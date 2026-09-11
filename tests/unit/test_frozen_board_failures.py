"""Replay actual saved model failures; never rewrite their financial claims."""
from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
import pytest
from app.v3 import data_trace
from app.v3.decision_contract import entry_errors, correction_errors

EVIDENCE = Path(__file__).resolve().parents[2] / 'docs/benchmarks/evidence/board-memory-proxy-2026-09-11'
ROWS = [json.loads(next(EVIDENCE.glob(f'{n:02}-*.json')).read_text()) for n in (4,5,6,9,11)]

@pytest.mark.parametrize('row', ROWS, ids=lambda row: row['case']+'-'+row['arm'])
def test_saved_entry_failures_are_reproducible(row):
    assert entry_errors(row['artifact']) == row['entry_errors']

@pytest.mark.asyncio
@pytest.mark.parametrize('row', [r for r in ROWS if r['case'] != 'held_deterioration'], ids=lambda row: row['case']+'-'+row['arm'])
async def test_production_repairs_only_contract_fields(row):
    from app.v3.shared_desk import SharedDesk, PhaseOutcome
    from app.v3.agent_runner import run_v3_agent
    from app.v3.agents import board_of_directors as board
    desk = SharedDesk(ticker='EVLT', cycle_id='frozen-replay-'+row['cycle_id'])
    desk.cycle_metadata = {'decision_contract_version':1, 'held':False}
    original = deepcopy(row['artifact'])
    original['research_answers'] = json.loads(row['response'])['research_answers']
    fixed = {**original, 'entry_mode':'watch_only'}
    if not fixed.get('dynamic_trigger'):
        fixed['trigger_purpose'] = 'none'
    assert correction_errors(original, fixed) == []
    module = SimpleNamespace(AGENT_NAME=board.AGENT_NAME, ARTIFACT_TYPE=board.ARTIFACT_TYPE,
                             TOOL_WHITELIST=board.TOOL_WHITELIST, SYSTEM_PROMPT=board.PERSONA_JANE_STREET)
    responses = [{'response':text,'tokens_used':80,'loops_used':1,'stop_reason':'completed'} for text in (row['response'],json.dumps(fixed))]
    with patch('app.agents.base_agent.run_agent',new_callable=AsyncMock,side_effect=responses) as model, \
         patch.object(data_trace.mongo_store,'update_docs'), patch.object(data_trace.mongo_store,'insert_docs'):
        outcome = await run_v3_agent(desk,module,cycle_id=desk.cycle_id,bot_id='test')
    assert outcome in (PhaseOutcome.SUCCESS, PhaseOutcome.DATA_GAP)
    assert model.await_count == 1
    assert desk.final_decision['action'] == original['action']
    assert desk.final_decision['reasoning'] == original['reasoning']
    assert entry_errors(desk.final_decision) == []

@pytest.mark.asyncio
async def test_answers_without_a_decision_cannot_be_published():
    from app.v3.shared_desk import SharedDesk, PhaseOutcome
    from app.v3.agent_runner import run_v3_agent
    from app.v3.agents import board_of_directors as board
    row = next(r for r in ROWS if r['case']=='held_deterioration')
    desk = SharedDesk(ticker='EVLT',cycle_id='frozen-missing-decision')
    desk.cycle_metadata = {'decision_contract_version':1,'held':True}
    module = SimpleNamespace(AGENT_NAME=board.AGENT_NAME,ARTIFACT_TYPE=board.ARTIFACT_TYPE,
                             TOOL_WHITELIST=board.TOOL_WHITELIST,SYSTEM_PROMPT=board.PERSONA_JANE_STREET)
    response = {'response':row['response'],'tokens_used':80,'loops_used':1,'stop_reason':'completed'}
    with patch('app.agents.base_agent.run_agent',new_callable=AsyncMock,return_value=response) as model, \
         patch.object(data_trace.mongo_store,'update_docs'), patch.object(data_trace.mongo_store,'insert_docs'):
        outcome = await run_v3_agent(desk,module,cycle_id=desk.cycle_id,bot_id='test')
    assert outcome == PhaseOutcome.AGENT_ERROR
    assert not desk.final_decision
    assert model.await_count == 2
    assert model.call_args_list[1].kwargs['enable_tools'] is False

@pytest.mark.parametrize('field,value', [('action','BUY'),('confidence',99),('reasoning','A fabricated replacement rationale'),('position_size_pct',4)])
def test_contract_repair_cannot_rewrite_financial_claims(field,value):
    original=ROWS[0]['artifact']
    fixed={**original,'entry_mode':'watch_only','trigger_purpose':'none',field:value}
    assert f'correction changed {field}' in correction_errors(original,fixed)


@pytest.mark.parametrize('row', [r for r in ROWS if r['case'] != 'held_deterioration'])
def test_raw_named_envelopes_retain_decision_and_answers(row):
    from app.v3.agent_runner import _parse_artifact
    raw=json.loads(row['response'])
    parsed=_parse_artifact(row['response'],'final_decision','board')
    assert all(parsed[key]==value for key,value in row['artifact'].items())
    assert parsed['research_answers']==raw['research_answers']
    assert entry_errors(parsed)==row['entry_errors']

@pytest.mark.parametrize('extra', [
    {'action':'SELL'}, {'unrecognized':1},
    {'research_answers':'not a list'},
])
def test_ambiguous_named_envelopes_are_not_unwrapped(extra):
    from app.v3.agent_runner import _parse_artifact
    raw={'final_decision':ROWS[0]['artifact'],**extra}
    assert _parse_artifact(json.dumps(raw),'final_decision','board')==raw


def test_named_envelope_never_salvages_partial_or_conflicting_payload():
    from app.v3.agent_runner import _parse_artifact
    for raw in ({'final_decision':{'reasoning':'fragment'}},
                {'final_decision':{**ROWS[0]['artifact'],'research_answers':[]},'research_answers':[{'id':'conflict'}]}):
        assert _parse_artifact(json.dumps(raw),'final_decision','board')==raw


@pytest.mark.parametrize('field', ['research_answers','resolution_condition'])
def test_correction_cannot_drop_existing_research(field):
    original={**ROWS[0]['artifact'],'research_answers':[{'id':'question','answer':'unknown'}],
              'resolution_condition':{'open_question':'Missing historical observations','resolving_fact':'Dated volume records'}}
    fixed={**original,'entry_mode':'watch_only'}
    assert correction_errors(original,fixed)==[]
    fixed.pop(field)
    assert f'correction changed {field}' in correction_errors(original,fixed)

@pytest.mark.asyncio
@pytest.mark.parametrize('index,expected', [(4,'SUCCESS'),(5,'SUCCESS'),(6,'SUCCESS'),(9,'SUCCESS'),(11,'SUCCESS')])
async def test_retained_live_repairs_through_production_runner(index,expected):
    from app.v3.shared_desk import SharedDesk
    from app.v3.agent_runner import run_v3_agent
    from app.v3.agents import board_of_directors as board
    row=json.loads(next(EVIDENCE.glob(f'{index:02}-*.json')).read_text())
    repair=json.loads((EVIDENCE.parent/'board-failure-isolation-2026-09-11/live-repairs-final'/f'{index:02}.json').read_text())
    desk=SharedDesk(ticker='EVLT',cycle_id='saved-live-repair-'+str(index))
    desk.cycle_metadata={'decision_contract_version':1,'held':index==9}
    module=SimpleNamespace(AGENT_NAME=board.AGENT_NAME,ARTIFACT_TYPE=board.ARTIFACT_TYPE,
                           TOOL_WHITELIST=board.TOOL_WHITELIST,SYSTEM_PROMPT=board.PERSONA_JANE_STREET)
    responses=[{'response':text,'tokens_used':80,'loops_used':1,'stop_reason':'completed'}
               for text in (row['response'],repair['calls'][1]['response'])]
    with patch('app.agents.base_agent.run_agent',new_callable=AsyncMock,side_effect=responses) as model, \
         patch.object(data_trace.mongo_store,'insert_docs'),patch.object(data_trace.mongo_store,'update_docs'):
        outcome=await run_v3_agent(desk,module,cycle_id=desk.cycle_id,bot_id='test')
    assert outcome.value==expected
    assert model.await_count==(2 if index==9 else 1)
    if index!=9:
        raw=json.loads(row['response'])
        assert desk.final_decision['research_answers']==raw['research_answers']
        assert desk.final_decision['reasoning']==row['artifact']['reasoning']
    else:
        # A successful schema repair is not verification of financial facts.
        assert '42% to 30%' in desk.final_decision['reasoning']
