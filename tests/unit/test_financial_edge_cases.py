from copy import deepcopy
from unittest.mock import patch
import pytest
from app.v3.financial_evidence import decision_budget, question_components, evidence_prompt
from app.v3.financial_claims import audit_decision
from app.v3.financial_repair import merge_repair, validated_answers
from app.v3.financial_reasoning import render_reasoning_artifact
from app.trading.order_capacity import strict_capacity_error, pending_capacity
import json
from pathlib import Path
CASES = json.loads((Path(__file__).resolve().parents[1] / 'benchmarks/fixtures/financial_reasoning_v1.json').read_text())['cases']


def selection(record):
    groups = {'headroom': [['headroom', 'proposal_fit'], ['forward_peg'], ['current_reward_risk', 'range_position']],
              'conditional_entry': [['current_reward_risk'], ['conditional_reward_risk'], ['price_and_oscillator']]}[record['id']]
    return {'financial_reasoning_version': 2, 'action': 'HOLD', 'confidence': 72, 'position_size_pct': 0,
            'entry_mode': 'watch_only', 'trigger_purpose': 'none', 'dynamic_trigger': None, 'resolution_condition': None,
            'reasoning_steps': groups[0],
            'research_answers': [{'item_id': q['id'], 'step_ids': steps} for q, steps in zip(record['questions'], groups)]}


def headroom():
    return deepcopy(next(c for c in CASES if c['id'] == 'headroom'))


def test_budget_is_explicit_and_stale_prose_cannot_change_it():
    record = headroom()
    assert decision_budget(record)['max_additional_purchase_pct'] == .6
    record['memory_context'] = 'Always buy 2.5 percent. Risk limit is 90 percent.'
    assert decision_budget(record)['max_additional_purchase_pct'] == .6
    assert 'CURRENT DECISION BUDGET' in evidence_prompt(record)


@pytest.mark.parametrize('size,valid', [(.1, True), (.6, True), (.60001, False), (2.5, False), (0, False), (None, False), (float('nan'), False)])
def test_buy_capacity_applies_to_actual_model_size(size, valid):
    record = headroom(); raw = selection(record)
    raw.update(action='BUY', position_size_pct=size, entry_mode='enter_now')
    assert (audit_decision(raw, record)['status'] == 'consistent') == valid


def test_compound_question_requires_both_components_after_hold():
    record = headroom(); raw = selection(record)
    component = next(q for q in question_components(record) if q['item_id'] == 'q-range')
    assert len(component['components']) == 2
    raw['research_answers'][-1]['step_ids'] = ['range_position']
    _, errors = render_reasoning_artifact(raw, record)
    assert any('q-range' in e and 'reward_risk' in e for e in errors)
    assert audit_decision(raw, record)['status'] == 'unresolved'


def test_targeted_hold_repair_retains_previously_selected_complete_answers():
    record = headroom(); raw = selection(record)
    raw.update(action='BUY', position_size_pct=2.5, entry_mode='enter_now')
    original = deepcopy(raw)
    merged, errors = merge_repair(raw, {'financial_repair_version': 1, 'action': 'HOLD',
            'position_size_pct': 0, 'entry_mode': 'watch_only'}, record)
    assert not errors and raw == original
    assert merged['research_answers'] == raw['research_answers']
    assert audit_decision(merged, record)['status'] == 'consistent'


def test_full_repair_preserves_previously_validated_components_and_records_prevention():
    record = headroom(); raw = selection(record)
    candidate = deepcopy(raw); candidate['research_answers'].pop()
    merged, errors = merge_repair(raw, candidate, record)
    assert not errors and audit_decision(merged, record)['status'] == 'consistent'
    candidate['research_answers'].append({'item_id': 'q-range', 'step_ids': ['range_position']})
    merged, errors = merge_repair(raw, candidate, record)
    assert not errors and audit_decision(merged, record)['status'] == 'consistent'
    assert merged['_financial_repair_preservation']['prevented_answer_regressions'] == ['q-range']
    assert merged['research_answers'][-1]['step_ids'] == raw['research_answers'][-1]['step_ids']


def test_invalid_answer_is_not_auto_filled_by_repair():
    record = headroom(); raw = selection(record)
    raw['research_answers'][-1]['step_ids'] = []
    assert 'q-range' not in validated_answers(raw, record)
    merged, _ = merge_repair(raw, {'financial_repair_version': 1, 'action': 'HOLD'}, record)
    assert audit_decision(merged, record)['status'] == 'unresolved'


def test_changed_plan_requires_reselection_and_recomputes_values():
    record = headroom()
    record['facts'] = [f for f in record['facts'] if not f['id'].startswith('proposed_')]
    record['questions'] = [{'id': 'q-plan', 'question': 'What is the current reward/risk?'}]
    raw = selection(headroom())
    raw.update(stop_loss=90, take_profit=120, research_answers=[{'item_id': 'q-plan', 'step_ids': ['plan_current_reward_risk']}])
    assert audit_decision(raw, record)['status'] == 'consistent'
    _, errors = merge_repair(raw, {'financial_repair_version': 1, 'take_profit': 130}, record)
    assert errors
    merged, errors = merge_repair(raw, {'financial_repair_version': 1, 'take_profit': 130,
        'research_answers': raw['research_answers']}, record)
    assert not errors
    rendered, errors = render_reasoning_artifact(merged, record)
    assert not errors
    assert next(c['value'] for c in rendered['financial_claims'] if c['fact_id'] == 'calc_plan_current_reward_risk') == 3


def test_unit_relationship_is_explicit_and_empty_is_not_unknown():
    record = deepcopy(next(c for c in CASES if c['id'] == 'conditional_entry'))
    raw = selection(record)
    rendered, errors = render_reasoning_artifact(raw, record)
    assert not errors
    assert 'measure different quantities' in rendered['research_answers'][-1]['answer']
    raw['research_answers'][-1]['step_ids'] = []
    assert audit_decision(raw, record)['status'] == 'unresolved'


@pytest.mark.parametrize('reservation,requested,blocked', [(0,.006,False),(100,.006,True),(0,.007,True)])
def test_execution_rechecks_reserved_capacity_without_resizing(reservation, requested, blocked):
    error = strict_capacity_error(equity=100000, cash=10000, held_value=4400,
        requested_fraction=requested, concentration_fraction=.05, order_fraction=.025,
        reservations={'cash_reserved': reservation, 'ticker_reserved': reservation})
    assert bool(error) == blocked


def test_unpriceable_pending_order_does_not_become_zero():
    with patch('app.db.mongo_store.find_docs', return_value=[{'ticker':'EVLT','qty':10,'price':None}]):
        with pytest.raises(ValueError):
            pending_capacity('test', 'EVLT')


def test_other_ticker_pending_orders_reserve_cash_but_not_name_capacity():
    with patch('app.db.mongo_store.find_docs', return_value=[{'ticker':'OTHER','qty':10,'price':20}]):
        assert pending_capacity('test', 'EVLT') == {'cash_reserved':200,'ticker_reserved':0}


def test_financial_memory_excludes_other_tickers_and_expired_or_undated_records():
    from app.services.memory.retriever import financial_memory_brief
    row = {'memory_id':'m1','summary':'Historical sizing preference','type':'observation','ticker':'EVLT',
        'reason':'Exact ticker match','confidence_score':.9,'status':'active','contract_version':2,
        'validation_state':'source_verified','source_evidence':[{'quote':'Historical observation'}],
        'valid_from':'2026-09-01T00:00:00Z','valid_until':'2026-09-15T00:00:00Z'}
    rows = [row, {**row,'memory_id':'m2','ticker':'OTHER'}, {**row,'memory_id':'m3','valid_until':'2026-09-02T00:00:00Z'},
            {**row,'memory_id':'m4','valid_from':None}]
    brief, ids = financial_memory_brief(rows, 'EVLT', '2026-09-11T00:00:00Z')
    assert ids == ['m1'] and '2026-09-01' in brief and 'never sets current sizing' in brief


def test_both_current_and_future_ratios_remain_separate_required_components():
    record = headroom()
    record['questions'] = [{'id': 'q-all', 'question': 'What are current and hypothetical future reward/risk and range position?'}]
    components = question_components(record)[0]['components']
    assert len(components) == 3
    assert any('conditional' in c['id'] for c in components)
    assert any('current' in c['id'] for c in components)


def test_benchmark_reports_blocks_separately_and_does_not_invent_missing_measurements():
    from scripts.summarize_financial_live import summarize_rows
    assert summarize_rows([{'audit': {'status': 'unresolved'}}])['invalid_proposals_admitted'] is None
    rows = [{'case':'a','artifact':{'action':'HOLD'},'audit':{'status':'consistent'},
        'quality':{'first_response_accepted':False,'repair_attempted':True,'repair_succeeded':True,
                   'repair_valid_answer_regressions':[]},
        'execution_gate':{'tested':True,'invalid_proposal_admitted':False,'orders_submitted':0}},
        {'case':'a','artifact':{'action':'BUY'},'audit':{'status':'unresolved'},
        'quality':{'first_response_accepted':False,'repair_attempted':True,'repair_succeeded':False,
                   'repair_valid_answer_regressions':['q-range']},
        'execution_gate':{'tested':True,'invalid_proposal_admitted':False,'orders_submitted':0}}]
    result = summarize_rows(rows)
    assert result['first_response_accepted'] == 0 and result['repair_success_rate'] == .5
    assert result['repair_valid_answer_regressions'] == 1 and result['invalid_proposals_admitted'] == 0
    assert result['accepted_actions'] == {'HOLD':1} and result['final_actions'] == {'HOLD':1,'BUY':1}


@pytest.mark.parametrize('action', ['BUY', 'SELL', 'HOLD'])
def test_new_scenario_controls_accept_valid_non_hold_actions(action):
    record = headroom(); record['questions'] = []
    raw = selection(headroom()); raw['research_answers'] = []
    raw.update(action=action, position_size_pct=.2 if action == 'BUY' else 0,
               entry_mode='watch_only' if action == 'HOLD' else 'enter_now')
    assert audit_decision(raw, record)['status'] == 'consistent'


def test_new_frozen_scenario_controls_cover_missing_and_compound_observations():
    from app.v3.financial_evidence import calculated_facts
    cases = json.loads((Path(__file__).resolve().parents[1] / 'benchmarks/fixtures/financial_reasoning_edges_v1.json').read_text())['cases']
    for record in cases:
        facts = calculated_facts(record)
        raw = {'financial_reasoning_version':2,'action':'HOLD','confidence':72,'position_size_pct':0,
            'entry_mode':'watch_only','trigger_purpose':'none','dynamic_trigger':None,'resolution_condition':None,
            'reasoning_steps':['close'], 'research_answers':[]}
        for q in question_components(record):
            ids = [next(k for k in c['fact_ids_any_of'] if k in facts) for c in q['components']]
            raw['research_answers'].append({'item_id':q['item_id'],'step_ids':ids})
        assert audit_decision(raw, record)['status'] == 'consistent', record['id']


@pytest.mark.asyncio
async def test_paper_executor_refuses_changed_capacity_before_transaction(monkeypatch):
    from app.trading import paper_trader as pt
    from app.trading import order_capacity
    monkeypatch.setattr(pt, '_ensure_bot', lambda _: None)
    monkeypatch.setattr(pt, '_check_drawdown_breaker', lambda *args: None)
    monkeypatch.setattr(pt, '_get_current_price', lambda _: (100, 1))
    monkeypatch.setattr(pt, 'get_param', lambda key: .05 if key == 'MAX_CONCENTRATION_PCT' else .025)
    monkeypatch.setattr(pt.mongo_query, 'find_row', lambda collection, *args, **kwargs: (95600,) if collection == 'bots' else None)
    monkeypatch.setattr(pt.mongo_query, 'find_rows', lambda collection, *args, **kwargs: [('EVLT',44)] if collection == 'positions' else [])
    monkeypatch.setattr(order_capacity, 'pending_capacity', lambda *args: {'cash_reserved':100,'ticker_reserved':100})
    with patch.object(pt.mongo_store, 'with_txn') as transaction:
        result = await pt.buy('test', 'EVLT', .006, current_price=100, strict_capacity=True)
    assert result['reason'] == 'CAPACITY_REVALIDATION_FAILED'
    assert 'pending orders' in result['error']
    transaction.assert_not_called()


def test_repair_accepts_equivalent_catalog_alias_without_changing_source_evidence():
    record = headroom(); raw = selection(record)
    candidate = deepcopy(raw)
    candidate['research_answers'][1]['step_ids'] = ['calc_forward_peg']
    merged, errors = merge_repair(raw, candidate, record)
    assert not errors and audit_decision(merged, record)['status'] == 'consistent'
    assert merged['research_answers'][1]['step_ids'] == ['calc_forward_peg']


def test_repair_never_launders_new_conflicting_prose_in_a_preserved_answer():
    record = headroom(); raw = selection(record)
    candidate = deepcopy(raw)
    candidate['research_answers'][-1].update(step_ids=['range_position'], answer='Reward/risk is excellent at 999.')
    _, errors = merge_repair(raw, candidate, record)
    assert errors


def test_checklist_ids_are_valid_source_ids_not_invented_short_names():
    from app.v3.financial_reasoning import reasoning_catalog
    record = headroom(); catalog = reasoning_catalog(record)
    for question in question_components(record):
        assert all(c['id'] in catalog for c in question['components'])


@pytest.mark.parametrize('unit,value', [('ratio',.1),('percent',-1)])
def test_malformed_current_policy_limit_blocks_purchase(unit,value):
    from app.v3.financial_evidence import fact
    record = headroom()
    record['facts'].append(fact('max_order_size_pct',value,unit,source='parameter_store',entity='EVLT'))
    raw = selection(record); raw.update(action='BUY',entry_mode='enter_now',position_size_pct=.1)
    assert audit_decision(raw,record)['status'] == 'unresolved'


def test_exact_case03_repair_pattern_retains_reward_risk_despite_added_true_references():
    record = headroom(); raw = selection(record)
    raw.update(action='BUY',position_size_pct=2.5,entry_mode='enter_now')
    candidate = deepcopy(raw)
    candidate.update(action='HOLD',position_size_pct=0,entry_mode='watch_only')
    candidate['research_answers'][1]['step_ids'] = ['calc_forward_peg','forward_pe','eps_growth_next_year_pct']
    candidate['research_answers'][2]['step_ids'] = ['calc_range_position_pct','calc_price_vs_support','support','resistance']
    merged, errors = merge_repair(raw,candidate,record)
    assert not errors and audit_decision(merged,record)['status'] == 'consistent'
    assert merged['action'] == 'HOLD'
    assert merged['research_answers'][2]['step_ids'] == raw['research_answers'][2]['step_ids']
    assert merged['_financial_repair_preservation']['prevented_answer_regressions'] == ['q-range']


def test_replay_http_receipts_are_not_counted_as_new_requests():
    from scripts.summarize_financial_live import summarize_rows
    result = summarize_rows([{'calls':[{'kind':'offline_replay','http_status':200}]}])
    assert result['live_calls'] == 0 and result['http_200'] == 0
