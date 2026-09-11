"""Offline review aid: independently recompute core arithmetic from raw fixture facts.

This is not the production validator and does not judge investment merit or
arbitrary prose. The compact output supports separate rationale review.
"""
import argparse
import json
from pathlib import Path


def review(record):
    source={f['id']:f for f in record['evidence']['facts']}
    def value(key):
        f=source.get(key,{})
        return f.get('value') if f.get('status')=='known' else None
    def ratio(price,stop,target):
        return round((target-price)/(price-stop),6) if all(x is not None for x in (price,stop,target)) and price>stop and target>price else None
    close,support,resistance=[value(k) for k in ('close','support','resistance')]
    pe,growth=value('forward_pe'),value('eps_growth_next_year_pct')
    cost=value('average_cost')
    limit,exposure,pending=[value(k) for k in ('single_name_limit_pct','exposure_pct','pending_exposure_pct')]
    headroom=max(0,limit-exposure-pending) if all(x is not None for x in (limit,exposure,pending)) else None
    expected={
        'calc_range_position_pct':100*(close-support)/(resistance-support) if all(x is not None for x in (close,support,resistance)) and resistance>support else None,
        'calc_forward_peg':pe/growth if pe is not None and growth is not None and growth>0 else None,
        'calc_holding_return_pct':100*(close-cost)/cost if close is not None and cost is not None and cost>0 else None,
        'calc_headroom_pct':headroom,
        'calc_proposed_current_reward_risk':ratio(close,value('proposed_stop'),value('proposed_target')),
        'calc_proposed_conditional_reward_risk':ratio(value('proposed_entry'),value('proposed_stop'),value('proposed_target')),
    }
    artifact=record.get('artifact') or {}
    discrepancies=[];checked=[]
    for claim in artifact.get('financial_claims',[]):
        if not isinstance(claim,dict):continue
        key=claim.get('fact_id');actual=claim.get('value')
        if key not in expected:continue
        wanted=expected[key];checked.append(key)
        matches=actual is None if wanted is None else isinstance(actual,(float,int)) and not isinstance(actual,bool) and abs(actual-wanted)<0.0001
        if not matches:discrepancies.append({'fact':key,'expected':wanted,'actual':actual})
    claim_values={c['fact_id']:c['value'] for c in artifact.get('financial_claims',[]) if isinstance(c,dict) and 'fact_id' in c}
    return {'index':record['index'],'case':record['case'],'arm':record['arm'],'outcome':record.get('outcome'),
            'financial_status':record.get('audit',{}).get('status'),'calls':len(record['calls']),
            'action':artifact.get('action'),'confidence':artifact.get('confidence'),'size':artifact.get('position_size_pct'),
            'entry_mode':artifact.get('entry_mode'),'arithmetic_checked':checked,'arithmetic_discrepancies':discrepancies,
            'reasoning':artifact.get('reasoning'),
            'answers':[{'id':a.get('item_id'),'question':a.get('question'),'status':a.get('status'),'answer':a.get('answer'),
                        'values':{k:claim_values.get(k) for k in a.get('fact_ids',[])}} for a in artifact.get('research_answers',[]) if isinstance(a,dict)]}


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('cohort',type=Path);parser.add_argument('--out',type=Path);args=parser.parse_args()
    rows=[review(json.loads(p.read_text())) for p in sorted(args.cohort.glob('[0-9][0-9].json')) if 'outcome' in json.loads(p.read_text())]
    result={'scope':'Independent arithmetic recomputation plus rationale review aid; no automatic investment-quality judgment.', 'rows':rows}
    if args.out:
        if args.out.exists():raise SystemExit('Preserve existing reviews; choose another output.')
        args.out.write_text(json.dumps(result,indent=2)+'\n')
    print(json.dumps(result,indent=2))
