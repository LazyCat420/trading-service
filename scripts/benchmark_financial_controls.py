"""Offline before/after controls. Generated controls are NOT model successes."""
from copy import deepcopy
import argparse,hashlib,json
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from app.v3.financial_evidence import calculated_facts
from app.v3.financial_claims import audit_decision
from app.v3.financial_reasoning import render_reasoning_artifact
from app.v3.arithmetic_audit import audit_artifact


def control(case):
    facts=calculated_facts(case)
    base=case.get('base_case',case['id'])
    groups={
      'headroom':[['calc_headroom_pct','calc_headroom_usd','calc_proposal_fits'],['calc_forward_peg'],['calc_proposed_current_reward_risk','calc_range_position_pct']],
      'missing_history':[['volume_five_session_trend'],['calc_forward_peg'],['sma_200']],
      'held_deterioration':[['free_cash_flow','operating_margin_pct'],['calc_holding_return_pct'],['underlying_filing_count']],
      'conditional_entry':[['calc_proposed_current_reward_risk'],['calc_proposed_conditional_reward_risk'],['support','rsi_14']],
    }[base]
    descriptions={
      'headroom':['The proposal does not fit the remaining concentration capacity.','The growth-adjusted multiple uses the supplied earnings growth.','The ratio and range location follow the supplied price levels.'],
      'missing_history':['The requested historical observations are absent.','The required earnings-growth observation is unavailable.','The supplied observations do not establish the requested long moving average.'],
      'held_deterioration':['The current filing reports negative cash flow and operating margin.','The holding return uses purchase cost as its denominator.','These reports repeat the same underlying filing.'],
      'conditional_entry':['The current reference price determines the present ratio.','The alternative ratio is contingent on the proposed future entry.','Price and oscillator readings use different units and cannot be substituted.'],
    }[base]
    required=set(k for group in groups for k in group)
    required.update(k for k in ('rsi_14','operating_margin_pct','gross_margin_pct','debt_to_equity_prior','roic_pct','sector_roic_pct','guidance_record') if k in facts)
    claims=[{'fact_id':key,**{k:facts[key][k] for k in ('metric','value','unit','as_of','source')}} for key in sorted(required)]
    answers=[]
    for q,refs,description in zip(case['questions'],groups,descriptions):
        answers.append({'item_id':q['id'],'question':q['question'],
                        'status':'answered' if all(facts[k]['status']=='known' for k in refs) else 'unresolved',
                        'answer':description,'fact_ids':refs})
    action='SELL' if base=='held_deterioration' else 'HOLD'
    return {'action':action,'confidence':72,'position_size_pct':0,
            'entry_mode':'enter_now' if action=='SELL' else 'watch_only','trigger_purpose':'none','dynamic_trigger':None,
            'reasoning':'The assessment uses the supplied observations and their limitations '+''.join('['+k+']' for k in sorted(required))+'.',
            'financial_claims':claims,'research_answers':answers}


def benchmark():
    original=ROOT/'docs/benchmarks/evidence/board-memory-proxy-2026-09-11'
    review=json.loads((original/'review.json').read_text())
    baseline=[]
    for i in range(1,13):
        path=next(original.glob(f'{i:02}-*.json'));row=json.loads(path.read_text())
        baseline.append({'index':i,'case':row['case'],'old_arithmetic_checked':audit_artifact(deepcopy(row['artifact']))['checked'],
                         'factual_errors':review['rows'][i-1]['factual_errors'],
                         'fully_complete':review['rows'][i-1]['fully_complete'],'response_sha256':hashlib.sha256(row['response'].encode()).hexdigest()})
    cases=[]
    for name in ('financial_reasoning_v1.json','financial_reasoning_holdout_v1.json'):
        cases.extend(json.loads((ROOT/'tests/benchmarks/fixtures'/name).read_text())['cases'])
    controls=[];mutations=[];structured=[];structured_mutations=[]
    for case in cases:
        artifact=control(case);audit=audit_decision(artifact,case)
        controls.append({'case':case['id'],'artifact':artifact,'audit':audit})
        raw={k:deepcopy(artifact[k]) for k in ('action','confidence','position_size_pct','entry_mode','trigger_purpose','dynamic_trigger')}
        if case.get('base_case',case['id'])=='headroom':
            raw.update(action='BUY',position_size_pct=.2,entry_mode='enter_now')
        raw.update(financial_reasoning_version=2,resolution_condition=None,
                   reasoning_steps=[c['fact_id'] for c in artifact['financial_claims']],
                   research_answers=[{'item_id':a['item_id'],'step_ids':a['fact_ids']} for a in artifact['research_answers']])
        rendered,render_errors=render_reasoning_artifact(raw,case)
        structured.append({'case':case['id'],'authored_selection':raw,'rendered':rendered,
                           'render_errors':render_errors,'audit':audit_decision(rendered,case)})
        for kind in ('authored_prose','authored_value','changed_question','unknown_step'):
            bad=deepcopy(rendered)
            if kind=='authored_prose':bad['reasoning']='RSI is 999.'
            elif kind=='authored_value':bad['financial_claims'][0]['value']=999
            elif kind=='changed_question':bad['research_answers'][0]['question']='A different question'
            else:bad['reasoning_steps']=['invented_relationship']
            result=audit_decision(bad,case)
            structured_mutations.append({'case':case['id'],'kind':kind,'caught':result['status']=='unresolved','errors':result['errors']})

        for i,claim in enumerate(artifact['financial_claims']):
            bad=deepcopy(artifact);value=claim['value']
            wrong=not value if isinstance(value,bool) else 'invented' if isinstance(value,str) else 999 if value is None else value+1
            bad['financial_claims'][i]['value']=wrong
            result=audit_decision(bad,case)
            mutations.append({'case':case['id'],'fact_id':claim['fact_id'],'wrong_value':wrong,
                              'caught':any(e['kind']=='fact_value' for e in result['errors'])})
    return {'interpretation':'Offline deterministic controls, authored from supplied facts. These are not live model successes, not a causal reasoning-improvement result, and not investment-performance evidence.',
            'baseline':baseline,'controls':controls,'mutations':mutations,
            'structured_controls':structured,'structured_mutations':structured_mutations,
            'summary':{'original_responses':len(baseline),'original_expression_checks':sum(r['old_arithmetic_checked'] for r in baseline),
                       'original_responses_with_reviewed_factual_errors':sum(bool(r['factual_errors']) for r in baseline),
                       'original_fully_complete':sum(r['fully_complete'] for r in baseline),
                       'correct_controls':len(controls),'correct_controls_accepted':sum(r['audit']['status']=='consistent' for r in controls),
                       'corruptions':len(mutations),'corruptions_caught':sum(r['caught'] for r in mutations),
                       'structured_controls':len(structured),'structured_controls_accepted':sum(r['audit']['status']=='consistent' and not r['render_errors'] for r in structured),
                       'structured_tamper_cases':len(structured_mutations),'structured_tamper_cases_caught':sum(r['caught'] for r in structured_mutations)},
            'source_hashes':{p:hashlib.sha256((ROOT/p).read_bytes()).hexdigest() for p in
                ('app/v3/financial_evidence.py','app/v3/financial_claims.py','app/v3/financial_reasoning.py','scripts/benchmark_financial_controls.py')}}


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--out',required=True,type=Path);args=parser.parse_args()
    if args.out.exists():raise SystemExit('Refusing to replace a retained benchmark.')
    result=benchmark();args.out.parent.mkdir(parents=True,exist_ok=True)
    args.out.write_text(json.dumps(result,indent=2,default=str)+'\n')
    print(json.dumps(result['summary'],indent=2))
    assert result['summary']['correct_controls_accepted']==result['summary']['correct_controls']
    assert result['summary']['corruptions_caught']==result['summary']['corruptions']

    assert result['summary']['structured_controls_accepted']==result['summary']['structured_controls']
    assert result['summary']['structured_tamper_cases_caught']==result['summary']['structured_tamper_cases']
