from copy import deepcopy
import json
from pathlib import Path
import pytest
from app.v3.financial_reasoning import reasoning_catalog, render_reasoning_artifact
from app.v3.financial_claims import audit_decision

FIXTURES=Path(__file__).resolve().parents[1]/'benchmarks/fixtures'
CASES=[]
for name in ('financial_reasoning_v1.json','financial_reasoning_holdout_v1.json'):
    CASES.extend(json.loads((FIXTURES/name).read_text())['cases'])


def selection(case):
    base=case.get('base_case',case['id'])
    groups={
        'headroom':[['headroom','proposal_fit'],['forward_peg'],['current_reward_risk','range_position']],
        'missing_history':[['volume_five_session_trend'],['forward_peg'],['sma_200']],
        'held_deterioration':[['free_cash_flow','operating_margin_pct'],['holding_return'],['source_count']],
        'conditional_entry':[['current_reward_risk'],['conditional_reward_risk'],['price_and_oscillator']],
    }[base]
    return {'financial_reasoning_version':2,'action':'SELL' if case['held'] and base=='held_deterioration' else 'HOLD',
            'confidence':72,'position_size_pct':0,'entry_mode':'enter_now' if case['held'] and base=='held_deterioration' else 'watch_only',
            'trigger_purpose':'none','dynamic_trigger':None,'resolution_condition':None,
            'reasoning_steps':groups[0],
            'research_answers':[{'item_id':q['id'],'step_ids':ids} for q,ids in zip(case['questions'],groups)]}


@pytest.mark.parametrize('case',CASES,ids=lambda c:c['id'])
def test_selected_relationships_render_complete_dated_answers_without_authored_numbers(case):
    raw=selection(case);before=deepcopy(raw)
    rendered,errors=render_reasoning_artifact(raw,case)
    assert not errors
    assert raw==before
    assert rendered['action']==raw['action'] and rendered['confidence']==raw['confidence']
    assert [a['question'] for a in rendered['research_answers']]==[q['question'] for q in case['questions']]
    assert audit_decision(raw,case)['status']=='consistent'
    assert audit_decision(rendered,case)['status']=='consistent'
    assert all(a['status']=='unresolved' for a in rendered['research_answers']) if case.get('base_case',case['id'])=='missing_history' else all(a['status']=='answered' for a in rendered['research_answers'])


@pytest.mark.parametrize('mutation',['reasoning','answer','question','claim','step','missing_question','unrelated_step'])
def test_rendering_rejects_conflicting_authored_content_instead_of_laundering_it(mutation):
    case=CASES[0];rendered,_=render_reasoning_artifact(selection(case),case)
    if mutation=='reasoning':rendered['reasoning']='The proposal fits with abundant capacity.'
    elif mutation=='answer':rendered['research_answers'][0]['answer']='The proposed purchase fits.'
    elif mutation=='question':rendered['research_answers'][0]['question']='A different question'
    elif mutation=='claim':rendered['financial_claims'][0]['value']=999
    elif mutation=='step':rendered['reasoning_steps']=['invented_relationship']
    elif mutation=='missing_question':rendered['research_answers'].pop()
    else:rendered['research_answers'][0]={'item_id':case['questions'][0]['id'],'step_ids':['observation:close']}
    before=deepcopy(rendered)
    assert audit_decision(rendered,case)['status']=='unresolved'
    assert rendered==before


def test_range_interpretation_tracks_changed_source_values():
    case=deepcopy(CASES[0]);assert 'nearer support' in reasoning_catalog(case)['range_position']['statement']
    next(f for f in case['facts'] if f['id']=='close')['value']=112
    assert 'nearer resistance' in reasoning_catalog(case)['range_position']['statement']



def test_equivalent_question_map_and_explicit_source_ids_preserve_model_selections():
    case=CASES[0];raw=selection(case)
    raw['research_answers']={
        'q-headroom':['calc_headroom_pct','calc_headroom_usd','calc_proposal_fits'],
        'q-peg':{'step_ids':['calc_forward_peg']},
        'q-range':['calc_range_position_pct','calc_proposed_current_reward_risk'],
    }
    rendered,errors=render_reasoning_artifact(raw,case)
    assert not errors
    assert rendered['action']==raw['action'] and rendered['confidence']==raw['confidence']
    assert audit_decision(rendered,case)['status']=='consistent'
    raw['research_answers']['q-peg']={'item_id':'q-range','step_ids':['calc_forward_peg']}
    assert audit_decision(raw,case)['status']=='unresolved'



def test_source_id_selection_does_not_add_unselected_operands():
    case=deepcopy(CASES[0]);case['questions']=[];raw=selection(CASES[0])
    raw.update(reasoning_steps=['support'],research_answers=[])
    rendered,errors=render_reasoning_artifact(raw,case)
    assert not errors
    assert [c['fact_id'] for c in rendered['financial_claims']]==['support']



def test_selected_price_and_oscillator_operands_answer_their_unit_relationship():
    case=deepcopy(next(c for c in CASES if c['id']=='conditional_entry'))
    case['questions']=case['questions'][-1:]
    raw=selection(next(c for c in CASES if c['id']=='conditional_entry'))
    raw.update(reasoning_steps=['support'],research_answers=[{'item_id':'q-units','step_ids':['support','rsi_14']}])
    rendered,errors=render_reasoning_artifact(raw,case)
    assert not errors
    assert 'measure different quantities' in rendered['research_answers'][0]['answer']
    assert set(rendered['research_answers'][0]['fact_ids'])=={'support','rsi_14'}
    assert audit_decision(rendered,case)['status']=='consistent'



def test_comparison_step_and_single_source_id_have_distinct_meanings():
    case=deepcopy(CASES[0]);case['questions']=[]
    catalog=reasoning_catalog(case)
    assert catalog['compare_roic_pct']['fact_ids']==['roic_pct','sector_roic_pct']
    assert 'exceeds' in catalog['compare_roic_pct']['statement']
    raw=selection(CASES[0]);raw.update(reasoning_steps=['sector_roic_pct'],research_answers=[])
    rendered,errors=render_reasoning_artifact(raw,case)
    assert not errors
    assert [c['fact_id'] for c in rendered['financial_claims']]==['sector_roic_pct']


@pytest.mark.parametrize('case',[c for c in CASES if 'base_case' not in c],ids=lambda c:c['id'])
def test_repeated_known_references_are_idempotent_without_changing_authored_selections(case):
    raw=selection(case)
    raw['reasoning_steps']*=2
    for answer in raw['research_answers']:
        answer['step_ids']*=2
    before=deepcopy(raw)
    rendered,errors=render_reasoning_artifact(raw,case)
    assert not errors
    assert raw==before and rendered['reasoning_steps']==before['reasoning_steps']
    assert rendered['action']==raw['action'] and rendered['confidence']==raw['confidence']
    claims=rendered['financial_claims']
    assert len({c['fact_id'] for c in claims})==len(claims)
    if case['id']=='held_deterioration':
        answer=rendered['research_answers'][-1]
        assert answer['answer'].count('do not provide independent corroboration')==1
        assert answer['fact_ids']==['underlying_filing_count']
        assert next(c['value'] for c in claims if c['fact_id']=='underlying_filing_count')==1
    assert audit_decision(rendered,case)['status']=='consistent'
    rendered['financial_claims'][0]['value']=999
    assert audit_decision(rendered,case)['status']=='unresolved'


def test_repeated_references_do_not_hide_an_unknown_selection():
    case=CASES[0];raw=selection(case)
    raw['reasoning_steps']=['headroom','headroom','invented_relationship']
    _,errors=render_reasoning_artifact(raw,case)
    assert any('invented_relationship' in error for error in errors)
    assert audit_decision(raw,case)['status']=='unresolved'
