"""Unseen numerical variants, with independent expected results kept out of prompts."""
import json
from copy import deepcopy
from pathlib import Path
import pytest
from app.v3.financial_evidence import calculated_facts
from app.v3.financial_claims import audit_decision

CASES={c['id']:c for c in json.loads((Path(__file__).resolve().parents[1]/'benchmarks/fixtures/financial_reasoning_holdout_v1.json').read_text())['cases']}
EXPECTED={
 'headroom_a':{'calc_headroom_pct':.5,'calc_headroom_usd':1250,'calc_forward_peg':2.25,'calc_proposed_current_reward_risk':17/6,'calc_range_position_pct':600/23},
 'headroom_b':{'calc_headroom_pct':.45,'calc_headroom_usd':360,'calc_forward_peg':2,'calc_proposed_current_reward_risk':2,'calc_range_position_pct':100/3},
 'missing_a':{'calc_forward_peg':None,'sma_200':None,'volume_five_session_trend':None},
 'missing_b':{'calc_forward_peg':None,'sma_200':None,'volume_five_session_trend':None},
 'held_a':{'calc_holding_return_pct':-26.25,'operating_margin_pct':-4.5,'free_cash_flow':-65000000},
 'held_b':{'calc_holding_return_pct':900/92,'operating_margin_pct':-1.2,'free_cash_flow':-12000000},
 'conditional_a':{'calc_proposed_current_reward_risk':11/12,'calc_proposed_conditional_reward_risk':3.6},
 'conditional_b':{'calc_proposed_current_reward_risk':11/6,'calc_proposed_conditional_reward_risk':7.5},
}


@pytest.mark.parametrize('name',CASES)
def test_holdout_calculations_and_correct_claim_controls(name):
    case=deepcopy(CASES[name]);case['questions']=[]
    facts=calculated_facts(case)
    claims=[]
    for key,expected in EXPECTED[name].items():
        f=facts[key]
        assert f['value'] is None if expected is None else f['value']==pytest.approx(expected)
        claims.append({'fact_id':key,**{k:f[k] for k in ('metric','value','unit','as_of','source')}})
    artifact={'action':'HOLD','reasoning':'These supplied facts determine the current assessment '+''.join('['+c['fact_id']+']' for c in claims)+'.',
              'financial_claims':claims,'research_answers':[]}
    assert audit_decision(artifact,case)['status']=='consistent'
    artifact['financial_claims'][0]['value']=999
    assert any(e['kind']=='fact_value' for e in audit_decision(artifact,case)['errors'])


def test_holdout_dates_are_new_and_answers_are_not_embedded():
    assert all(c['as_of'].startswith('2026-10-03') for c in CASES.values())
    for name in ('missing_a','missing_b'):
        assert 'September 17, 2026' in CASES[name]['questions'][0]['question']
        assert calculated_facts(CASES[name])['volume_five_session_trend']['as_of']=='2026-09-17'
    assert all('expected' not in q for c in CASES.values() for q in c['questions'])
