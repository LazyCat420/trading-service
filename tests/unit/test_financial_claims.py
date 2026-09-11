from copy import deepcopy
import json
from pathlib import Path
import pytest
from app.v3.financial_evidence import calculated_facts
from app.v3.financial_claims import audit_decision

CASES={c['id']:c for c in json.loads((Path(__file__).resolve().parents[1]/'benchmarks/fixtures/financial_reasoning_v1.json').read_text())['cases']}


def claim(f):
    return {'fact_id':f['id'],**{k:f[k] for k in ('metric','value','unit','as_of','source')}}


def decision(case='conditional_entry'):
    record=deepcopy(CASES[case]);record['questions']=[]
    facts=calculated_facts(record)
    d={'action':'HOLD','confidence':65,'entry_mode':'watch_only','trigger_purpose':'none',
       'reasoning':'The current entry is extended [calc_proposed_current_reward_risk]. Waiting changes the scenario [calc_proposed_conditional_reward_risk].',
       'financial_claims':[claim(facts[k]) for k in ('calc_proposed_current_reward_risk','calc_proposed_conditional_reward_risk')],
       'research_answers':[]}
    return record,d


def test_correct_claims_accept_without_changing_model_decision():
    record,d=decision();before=deepcopy(d)
    report=audit_decision(d,record)
    assert report['status']=='consistent',report
    assert report['checked_claims']==2
    assert d==before


@pytest.mark.parametrize('key,value,kind', [('value',3,'fact_value'),('unit','RSI_points','fact_unit'),('as_of','2026-08-17','fact_as_of'),('metric','operating_margin_pct','fact_metric'),('source','invented','fact_source')])
def test_each_coordinate_of_a_claim_is_verified(key,value,kind):
    record,d=decision();d['financial_claims'][0][key]=value
    assert kind in {e['kind'] for e in audit_decision(d,record)['errors']}


@pytest.mark.parametrize('case,key,wrong', [('missing_history','rsi_14',14),('held_deterioration','operating_margin_pct',28),('held_deterioration','operating_margin_pct',2),('held_deterioration','debt_to_equity_prior',2),('conditional_entry','calc_proposed_conditional_reward_risk',3),('conditional_entry','calc_proposed_current_reward_risk',1.5),('headroom','calc_range_position_pct',33.3)])
def test_frozen_financial_error_classes_are_caught(case,key,wrong):
    record=deepcopy(CASES[case]);record['questions']=[];f=calculated_facts(record)[key]
    d={'action':'HOLD','reasoning':f'The decision depends on this observation [{key}].','financial_claims':[{**claim(f),'value':wrong}]}
    assert any(e['kind']=='fact_value' for e in audit_decision(d,record)['errors'])


def test_correct_fields_do_not_launder_wrong_numerical_prose():
    record,d=decision();d['reasoning']='Present reward/risk is 1.5 [calc_proposed_current_reward_risk].'
    assert any(e['kind']=='unstructured_number' for e in audit_decision(d,record)['errors'])


def test_question_date_and_unknown_status_are_enforced():
    record=deepcopy(CASES['missing_history']);record['questions']=record['questions'][:1]
    f=calculated_facts(record)['volume_five_session_trend']
    d={'action':'HOLD','reasoning':'Historical volume remains unknown [volume_five_session_trend].','financial_claims':[claim(f)],
       'research_answers':[{'item_id':'q-history','question':record['questions'][0]['question'],'status':'unresolved','answer':'The requested historical observations are absent.','fact_ids':[f['id']]}]}
    assert audit_decision(d,record)['status']=='consistent'
    d['research_answers'][0]['status']='answered'
    assert any(e['kind']=='answer_unknown' for e in audit_decision(d,record)['errors'])
    d['research_answers'][0]['question']='As of September third, was volume declining?'
    assert any(e['kind']=='question_changed' for e in audit_decision(d,record)['errors'])


def test_current_snapshot_cannot_resolve_a_historical_question():
    record=deepcopy(CASES['missing_history']);record['questions']=record['questions'][:1]
    f=calculated_facts(record)['close']
    d={'reasoning':'The current snapshot is available [close].','financial_claims':[claim(f)],
       'research_answers':[{'item_id':'q-history','question':record['questions'][0]['question'],'status':'answered','answer':'The historical trend is established.','fact_ids':['close']}]}
    assert any(e['kind']=='historical_evidence' for e in audit_decision(d,record)['errors'])


def test_operating_margin_is_not_comparable_to_sector_gross_margin():
    record=deepcopy(CASES['missing_history']);record['questions']=[]
    facts=calculated_facts(record)
    d={'reasoning':'The margin comparison informs the thesis [margin_comparison].',
       'financial_claims':[claim(facts[k]) for k in ('operating_margin_pct','sector_gross_margin_pct')],
       'financial_comparisons':[{'id':'margin_comparison','left_id':'operating_margin_pct','right_id':'sector_gross_margin_pct','relation':'above'}]}
    assert any(e['kind']=='comparison_metric' for e in audit_decision(d,record)['errors'])


@pytest.mark.parametrize('text,kind',[('The stock is below support.','price_support_relation'),('Management posted guidance beats.','guidance_overstatement')])
def test_non_numeric_contradictions_are_caught(text,kind):
    record,d=decision();d['reasoning']=text
    assert any(e['kind']==kind for e in audit_decision(d,record)['errors'])


def test_an_oversized_buy_does_not_pass_with_correct_arithmetic_claims():
    record=deepcopy(CASES['headroom']);record['questions']=[]
    d={'action':'BUY','position_size_pct':2.5,'reasoning':'The concentration budget is binding [calc_headroom_pct].',
       'financial_claims':[claim(calculated_facts(record)['calc_headroom_pct'])]}
    assert any(e['kind']=='size_exceeds_capacity' for e in audit_decision(d,record)['errors'])


def test_empty_or_forged_audit_is_never_a_pass():
    record,d=decision();d['financial_claims']=[];d['_financial_audit']={'status':'consistent'}
    assert audit_decision(d,record)['status']=='unresolved'


@pytest.mark.parametrize('bad', [True,{},'comparison'])
def test_malformed_comparison_container_fails_closed(bad):
    record,d=decision();d['financial_comparisons']=bad
    assert audit_decision(d,record)['status']=='unresolved'


def test_unrelated_true_number_cannot_answer_a_financial_question():
    record=deepcopy(CASES['held_deterioration']);record['questions']=record['questions'][:1]
    f=calculated_facts(record)['close']
    d={'reasoning':'The current observation is available [close].','financial_claims':[claim(f)],
       'research_answers':[{'item_id':'q-current','question':record['questions'][0]['question'],
                            'status':'answered','answer':'Current profitability is established.','fact_ids':['close']}]}
    assert any(e['kind']=='answer_relevance' for e in audit_decision(d,record)['errors'])


def test_abstention_cannot_hide_an_answerable_arithmetic_question():
    record=deepcopy(CASES['conditional_entry']);record['questions']=record['questions'][:1]
    d={'reasoning':'The current ratio is available [calc_proposed_current_reward_risk].',
       'financial_claims':[claim(calculated_facts(record)['calc_proposed_current_reward_risk'])],
       'research_answers':[{'item_id':'q-now','question':record['questions'][0]['question'],
                            'status':'unresolved','answer':'The arithmetic cannot be determined.','fact_ids':[]}]}
    assert any(e['kind']=='unnecessary_abstention' for e in audit_decision(d,record)['errors'])


def test_currency_units_alone_do_not_make_cash_an_exposure_measure():
    record,d=decision();facts=calculated_facts(record)
    d['financial_claims']=[claim(facts[k]) for k in ('cash','proposed_target')]
    d['financial_comparisons']=[{'id':'badcomparison','left_id':'cash','right_id':'proposed_target','relation':'above'}]
    d['reasoning']='This comparison is the stated justification [badcomparison].'
    assert any(e['kind']=='comparison_metric' for e in audit_decision(d,record)['errors'])


def test_repeated_source_does_not_become_independent_corroboration():
    record=deepcopy(CASES['held_deterioration']);record['questions']=[]
    c=claim(calculated_facts(record)['underlying_filing_count']);c['value']=2
    d={'reasoning':'The repeated reports corroborate each other [underlying_filing_count].','financial_claims':[c]}
    assert any(e['kind']=='fact_value' for e in audit_decision(d,record)['errors'])


@pytest.mark.parametrize('item', [None, True, [], {'item_id':[]}, {'item_id':{},'status':'answered'}])
def test_malformed_answers_fail_closed_without_crashing(item):
    record,d=decision();d['research_answers']=[item]
    assert audit_decision(d,record)['status']=='unresolved'


def test_correct_numeric_claim_cannot_mask_opposite_sign_in_prose():
    record=deepcopy(CASES['held_deterioration']);record['questions']=[]
    d={'reasoning':'Operating margin is positive [operating_margin_pct].',
       'financial_claims':[claim(calculated_facts(record)['operating_margin_pct'])]}
    assert any(e['kind']=='metric_sign' for e in audit_decision(d,record)['errors'])


def test_duplicate_source_ids_do_not_silently_overwrite_a_fact():
    record,d=decision();record['facts'].append(deepcopy(record['facts'][0]))
    assert audit_decision(d,record)['status']=='unresolved'


def test_resolution_question_cannot_move_the_original_historical_date():
    record=deepcopy(CASES['missing_history']);f=calculated_facts(record)['volume_five_session_trend']
    d={'reasoning':'The historical observations are unavailable [volume_five_session_trend].','financial_claims':[claim(f)],
       'resolution_condition':{'open_question':'Was volume declining as of September 3, 2026?'}}
    assert any(e['kind']=='resolution_date_changed' for e in audit_decision(d,record)['errors'])


def test_buy_above_total_cap_is_rejected_even_when_pending_orders_are_unknown():
    record=deepcopy(CASES['held_deterioration']);record['questions']=[]
    f=calculated_facts(record)['exposure_pct']
    d={'action':'BUY','position_size_pct':1,'reasoning':'The existing exposure is recorded [exposure_pct].','financial_claims':[claim(f)]}
    assert any(e['kind']=='size_exceeds_capacity' for e in audit_decision(d,record)['errors'])


@pytest.mark.parametrize('text', ['The close is one-third through the support-resistance range.', 'The close is at the midpoint of the range.'])
def test_range_words_cannot_mask_a_wrong_location(text):
    record=deepcopy(CASES['headroom']);record['questions']=[]
    d={'reasoning':text+' [calc_range_position_pct]',
       'financial_claims':[claim(calculated_facts(record)['calc_range_position_pct'])]}
    assert any(e['kind']=='range_position_relation' for e in audit_decision(d,record)['errors'])


def test_own_plan_cannot_replace_the_price_levels_in_a_question():
    record=deepcopy(CASES['conditional_entry']);record['questions']=record['questions'][:1]
    d={'action':'HOLD','stop_loss':100,'take_profit':145,'reasoning':'The alternative plan has a different ratio [calc_plan_current_reward_risk].'}
    d['financial_claims']=[claim(calculated_facts(record,d)['calc_plan_current_reward_risk'])]
    d['research_answers']=[{'item_id':'q-now','question':record['questions'][0]['question'],'status':'answered',
                            'answer':'This is the ratio for the alternate plan.','fact_ids':['calc_plan_current_reward_risk']}]
    assert any(e['kind']=='answer_relevance' for e in audit_decision(d,record)['errors'])


def test_filing_month_is_checked_even_without_an_explicit_day():
    record=deepcopy(CASES['held_deterioration']);record['questions']=record['questions'][2:]
    record['questions'][0]['question']='Do the reports repeating the same August filing provide independent sources?'
    f=calculated_facts(record)['underlying_filing_count']
    d={'reasoning':'The source identity is recorded [underlying_filing_count].','financial_claims':[claim(f)],
       'research_answers':[{'item_id':'q-independent','question':record['questions'][0]['question'],
                            'status':'answered','answer':'The reports repeat the same underlying filing.','fact_ids':[f['id']]}]}
    assert any(e['kind']=='historical_evidence' for e in audit_decision(d,record)['errors'])
    d['research_answers'][0].update(status='unresolved',answer='The requested earlier filing is not supplied.',fact_ids=[])
    assert audit_decision(d,record)['status']=='consistent'
