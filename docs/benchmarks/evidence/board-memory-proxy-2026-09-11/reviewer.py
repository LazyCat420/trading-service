import logging,json,hashlib
logging.disable(logging.CRITICAL)
from pathlib import Path
from app.db import mongo_store as m
ROOT=Path('docs/benchmarks/evidence/board-memory-proxy-2026-09-11')
manifest=json.loads((ROOT/'manifest.json').read_text())
reviews={
1:('partial',['Claims close is one-third through 95–115; correct location is 25%.'],['Correct 0.6 percentage-point headroom, PEG 1.25 and reward/risk 3.'],['No research_answers; omits explicit response to each supplied question.']),
2:('partial',[],['Correct headroom, PEG and reward/risk; lower third is compatible with 25%.'],['Exact 25% range location and research_answers missing.']),
3:('partial',[],['Correct headroom, PEG 1.25 and reward/risk 3; rejects oversized proposed purchase.'],['Exact 25% range location and research_answers missing.']),
4:('partial',['Changes the requested August 17 volume lookback to September 3.'],['Does not invent numeric PEG or SMA-200; volume is unknown.'],['Entry contract invalid; no research_answers.']),
5:('failed',['Reports RSI as 14 percent rather than supplied 46.','Compares operating margin 14% against gross margin 38% and says it exceeds it.','Misstates company/sector ROIC in the reasoning.'],['Acknowledges missing SMA, EPS growth and volume.'],['Entry contract invalid; research_answers missing.']),
6:('partial',[],['Correctly preserves August-volume, EPS-growth and SMA-200 unknowns.'],['Research trigger purpose lacks dynamic_trigger; research_answers missing.']),
7:('failed',['Calls gross margin 30% operating margin; current operating margin is -2%.','Says price 100 is below support 95.'],['Correct negative FCF, 10% exposure and -16.67% holding loss.'],['Source independence question not answered; research_answers missing.']),
8:('failed',['Loses negative sign on operating margin (-2% becomes +2%).','Confuses gross margin history with operating margin.','Says stock is below support despite close 100 above support 95.'],['Correct -16.67% holding return and current negative FCF.'],['Source independence question not answered; research_answers missing.']),
9:('failed',['Answers current operating margin as 28% instead of -2%.','Invents older debt/equity 2.0 and confuses gross/operating margins.'],['Correct holding return and one-source independence answer.'],['No final_decision action/confidence/reasoning or entry fields; cannot use artifact.']),
10:('partial',['Claims three guidance beats; evidence only says guidance ranges were met.','Says cash 25000 is within a 5000 single-name cap; cash is not exposure.'],['Correct present reward/risk 0.75 and hypothetical reward/risk 6, no current fill at 100.'],['Does not explicitly answer price-versus-RSI units question; research_answers missing.']),
11:('failed',['Hypothetical reward/risk is 3 instead of 6.','Sentence conflates the current and proposed entry reward/risk thresholds.'],['Current 7.5:10 equals 0.75; no fill at hypothetical price 100.'],['HOLD with enter_on_condition and monitor violates entry contract; research_answers missing.']),
12:('failed',['Present reward/risk 1.5 should be 0.75; hypothetical 3.5 should be 6.','Claims guidance beats instead of met ranges.','Treats cash as within a single-name exposure cap.'],['Correctly distinguishes price and RSI units and hypothetical entry.'],['research_answers missing.'])}
rows=[]
for n,p in enumerate(manifest['plan'],1):
 f=next(ROOT.glob(f'{n:02}-*.json'));r=json.loads(f.read_text());quality,errors,correct,gaps=reviews[n]
 events=m.find_docs('pipeline_trace_events',{'cycle_id':p['cycle_id'],'stage':'provider.payload'},projection={'_id':0})
 delivery=[]
 for e in events:
  h=(e.get('snapshot') or {}).get('hash');bs=m.find_docs('pipeline_trace_blobs',{'_id':h},limit=1)
  content=bs[0].get('content','') if bs else ''
  try:payload=json.loads(content);messages=payload.get('messages',[])
  except Exception:messages=[]
  systems='\n'.join(str(x.get('content','')) for x in messages if x.get('role')=='system')
  users=[x.get('content') for x in messages if x.get('role')=='user']
  delivery.append({'task_delivery':e.get('attributes',{}).get('task_delivery'),'model':e.get('attributes',{}).get('model'),'snapshot_hash':h,'snapshot_truncated':e.get('snapshot',{}).get('truncated'),'exact_user_found':p['body']['messages'][0]['content'] in users,'requested_system_found':p['body']['systemPrompt'] in systems})
 schema=not r.get('schema_errors') and not r.get('error');contract=schema and not r.get('entry_errors')
 rows.append({'case':p['case'],'arm':p['arm'],'file':f.name,'response_sha256':hashlib.sha256((r.get('response') or '').encode()).hexdigest(),'schema_valid':schema,'entry_valid':contract,'factual_errors':errors,'correct_observations':correct,'missing_or_incomplete':gaps,'review':quality,'evidence_consistent_usable':contract and not errors,'fully_complete':False,'provider_delivery':delivery,'usage':r.get('usage')})
summary={arm:{'attempts':4,'schema_valid':sum(x['schema_valid'] for x in rows if x['arm']==arm),'entry_valid':sum(x['entry_valid'] for x in rows if x['arm']==arm),'evidence_consistent_usable':sum(x['evidence_consistent_usable'] for x in rows if x['arm']==arm),'fully_complete':0} for arm in ('current','no_method','stale_memory')}
result={'summary':summary,'reviewer_limitation':'Single evaluator, not blinded. Material factual errors and incompleteness are listed separately. Four independent cases, one repeat per arm; exploratory diagnostics, not causal learning benefit or profitability. No artifacts rerun or repaired in this cohort.','rows':rows}
(ROOT/'review.json').write_text(json.dumps(result,indent=2))
print(json.dumps(summary,indent=2))
print('Provider receipts',sum(len(r['provider_delivery']) for r in rows),'exact tasks',sum(any(x['exact_user_found'] for x in r['provider_delivery']) for r in rows),'exact systems',sum(any(x['requested_system_found'] for x in r['provider_delivery']) for r in rows))
