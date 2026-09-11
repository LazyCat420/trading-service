from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace
import pytest
from app.v3.financial_evidence import calculated_facts, build_record, evidence_prompt

FIXTURE = json.loads((Path(__file__).resolve().parents[1] / 'benchmarks/fixtures/financial_reasoning_v1.json').read_text())
CASES = {c['id']: c for c in FIXTURE['cases']}


def test_arithmetic_uses_financial_denominators_and_distinct_scenarios():
    head = calculated_facts(CASES['headroom'])
    assert head['calc_headroom_pct']['value'] == .6
    assert head['calc_headroom_usd']['value'] == 600
    assert head['calc_proposal_fits']['value'] is False
    assert head['calc_forward_peg']['value'] == 1.25
    assert head['calc_range_position_pct']['value'] == 25
    assert head['calc_proposed_current_reward_risk']['value'] == 3
    held = calculated_facts(CASES['held_deterioration'])
    assert held['calc_holding_return_pct']['value'] == pytest.approx(-16.6666667)
    assert held['calc_price_vs_support']['value'] == 'above'
    conditional = calculated_facts(CASES['conditional_entry'])
    assert conditional['calc_proposed_current_reward_risk']['value'] == .75
    assert conditional['calc_proposed_conditional_reward_risk']['value'] == 6
    assert conditional['calc_proposed_conditional_reward_risk']['period'] == 'hypothetical'


def test_missing_history_does_not_use_revenue_growth_or_current_dates():
    facts = calculated_facts(CASES['missing_history'])
    assert facts['calc_forward_peg']['value'] is None
    assert facts['volume_five_session_trend']['as_of'] == '2026-08-17'
    assert facts['sma_200']['status'] == 'unknown'


@pytest.mark.parametrize('metric,value,unit', [('proposed_stop',115,'USD'), ('proposed_stop',95,'RSI_points'), ('proposed_target',100,'USD'), ('proposed_entry',None,'USD')])
def test_undefined_or_mixed_unit_calculations_abstain(metric,value,unit):
    case = deepcopy(CASES['conditional_entry'])
    f = next(f for f in case['facts'] if f['id']==metric)
    f.update(value=value,unit=unit,status='unknown' if value is None else 'known')
    assert calculated_facts(case)['calc_proposed_conditional_reward_risk']['status']=='unknown'


def test_model_proposal_math_does_not_treat_rsi_as_an_entry_price():
    decision={'stop_loss':95,'take_profit':130,'dynamic_trigger':{'type':'rsi_14_oversold','value':95}}
    result=calculated_facts(CASES['conditional_entry'],decision)
    assert result['calc_plan_current_reward_risk']['value']==.75
    assert result['calc_plan_conditional_reward_risk']['value'] is None
    decision['dynamic_trigger']={'type':'price_below','value':100}
    assert calculated_facts(CASES['conditional_entry'],decision)['calc_plan_conditional_reward_risk']['value']==6


def test_source_record_uses_captured_snapshots_and_field_dates_only():
    metadata={'timestamp':'2026-09-03','financial_technical_snapshot':{'as_of':'2026-09-03','rsi':46,'close':100},
              'financial_fundamental_snapshot':{'as_of':'2026-09-03','source':'vendor','oper_margin':-.02,'roic':.14,
                'field_as_of':{'roic':{'as_of':'2026-08-20','source':'older_vendor'}}},
              'fundamental_report':{'oper_margin':28},'memory_context':'operating margin 28',
              'financial_book_snapshot':{'valuation_complete':False,'equity':100000}}
    before=deepcopy(metadata)
    record=build_record(SimpleNamespace(ticker='EVLT',cycle_metadata=metadata))
    facts={f['id']:f for f in record['facts']}
    assert facts['operating_margin_pct']['value']==-2
    assert facts['roic_pct']['as_of']=='2026-08-20'
    assert 'older_vendor' in facts['roic_pct']['source']
    assert facts['eps_growth_next_year_pct']['value'] is None
    assert facts['equity']['value'] is None
    assert metadata==before
    assert '28' not in evidence_prompt(record)


def test_baseline_capture_matches_the_exact_rendered_observation(monkeypatch):
    from app.quant import fundamental_block, technical_baseline
    fundamental={'as_of':'2026-09-03','source':'vendor','oper_margin':-.02,'gross_margin':.30}
    technical={'as_of':'2026-09-03','rsi':46,'close':100}
    for module,compute,build,snapshot in ((fundamental_block,'compute_fundamental_baseline','build_fundamental_block',fundamental),
                                         (technical_baseline,'compute_technical_baseline','build_technical_baseline_block',technical)):
        monkeypatch.setattr(module,compute,lambda ticker,s=snapshot:s)
        sink={}
        builder=getattr(module,build)
        text=builder('EVLT',snapshot_sink=sink)
        assert text==builder('EVLT')
        assert sink==snapshot
        sink['source']='changed copy'
        assert snapshot.get('source')!='changed copy'


def test_incomplete_portfolio_marks_do_not_become_verified_exposure(monkeypatch):
    from app.v3.book_brief import build_book_brief
    from app.trading import paper_trader
    from app.tools import portfolio_tools
    monkeypatch.setattr(paper_trader,'get_portfolio',lambda bot:{'cash':25000,'positions':[{'ticker':'EVLT','qty':100,'avg_entry_price':120}]})
    monkeypatch.setattr(portfolio_tools,'_get_current_price',lambda ticker:(None,None))
    monkeypatch.setattr(portfolio_tools,'resolve_bot_id',lambda bot:bot)
    sink={}
    build_book_brief('EVLT','test',snapshot_sink=sink)
    assert sink['valuation_complete'] is False
    record=build_record(SimpleNamespace(ticker='EVLT',cycle_metadata={'financial_book_snapshot':sink}))
    assert next(f for f in record['facts'] if f['id']=='exposure_pct')['value'] is None
