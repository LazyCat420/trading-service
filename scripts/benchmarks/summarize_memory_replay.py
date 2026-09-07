"""Local paired analysis. No network or model calls; failures remain in denominator."""
import json,math,os,random,statistics
from pathlib import Path
ROOT=Path(__file__).resolve().parents[3]
BASE=Path(os.environ.get('MEMORY_BENCHMARK_DIR',str(ROOT/'.scratch/memory-isolation-20260907')))
rows=json.loads((BASE/'role-results.json').read_text())
quality=json.loads((BASE/'role-quality.json').read_text())
key=lambda r:(r['case'],r.get('repeat',0),r['arm'])
qmap={key(q):q for q in quality};case_ids=list(dict.fromkeys(r['case'] for r in rows))
arm_rows={a:[r for r in rows if r['arm']==a] for a in ['before','after']}
def pct(values,p):
 values=sorted(values)
 if not values:return None
 pos=(len(values)-1)*p;i=int(pos);return values[i]+(values[min(i+1,len(values)-1)]-values[i])*(pos-i)
def aggregate(rs):
 qs=[qmap[key(r)] for r in rs];facts=[f for q in qs for f in q['facts']]
 first_deltas=[t['first_delta_s'] for r in rs for t in r['turns'] if t.get('first_delta_s') is not None]
 error_causes={}
 for q in qs:
  for cause,count in q['tool_errors_by_cause'].items():error_causes[cause]=error_causes.get(cause,0)+count
 return {'runs':len(rs),'unique_cases':len(set(r['case'] for r in rs)),
  'elapsed_total_s':sum(r['elapsed_s'] for r in rs),'elapsed_mean_s':statistics.mean(r['elapsed_s'] for r in rs) if rs else None,
  'elapsed_median_s':statistics.median(r['elapsed_s'] for r in rs) if rs else None,'elapsed_p95_s':pct([r['elapsed_s'] for r in rs],.95),
  'prompt_tokens_observed':sum(r['prompt_tokens'] for r in rs),'completion_tokens_observed':sum(r['completion_tokens'] for r in rs),
  'usage_incomplete_runs':sum(not r['usage_complete'] for r in rs),'model_calls_observed':sum(len(r['turns']) for r in rs),
  'tool_calls':sum(r['tool_calls'] for r in rs),'tool_errors':sum(r['tool_errors'] for r in rs),'tool_errors_by_cause':error_causes,
  'first_output_delta_median_s_completed_turns':statistics.median(first_deltas) if first_deltas else None,
  'first_output_delta_p95_s_completed_turns':pct(first_deltas,.95),
  'raw_json_valid':sum(q['raw_json'] for q in qs),'parser_accepted':sum(q['parser_accepted'] for q in qs),
  'first_pass_usable':sum(q['first_pass_usable'] for q in qs),'complete_with_required_step':sum(q['complete_with_required_step'] for q in qs),
  'frozen_constraint_failures':sum(not ok for q in qs for field,ok in q.get('frozen_constraints',{}).items() if q.get('frozen_constraint_applicability',{}).get(field,True)),
  'frozen_constraint_evaluations':sum(1 for q in qs for field in q.get('frozen_constraints',{}) if q.get('frozen_constraint_applicability',{}).get(field,True)),
  'http_or_runtime_failures':sum(bool(r.get('error')) for r in rs),'output_cap_runs':sum(r['stop']=='length' for r in rs),
  'turn_cap_runs':sum(r['stop']=='max_turns' for r in rs),'facts':{s:sum(f['status']==s for f in facts) for s in ['correct','wrong','missing']}}
pairs=[]
for cid in case_ids:
 for rep in sorted(set(r.get('repeat',0) for r in rows if r['case']==cid)):
  matched={a:next((r for r in rows if key(r)==(cid,rep,a)),None) for a in ['before','after']}
  if not all(matched.values()):continue
  pairs.append({'case':cid,'repeat':rep,**matched})
def interval(metric):
 clusters=[]
 for cid in case_ids:
  ps=[p for p in pairs if p['case']==cid and (metric=='elapsed_s' or (p['before']['usage_complete'] and p['after']['usage_complete']))]
  if ps:clusters.append([sum(p[a][metric] for p in ps) for a in ['before','after']])
 if not clusters:return None
 rng=random.Random(20260907);boots=[]
 for _ in range(10000):
  pick=rng.choices(clusters,k=len(clusters));b=sum(x[0] for x in pick);a=sum(x[1] for x in pick)
  if b:boots.append(100*(a/b-1))
 before=sum(x[0] for x in clusters);after=sum(x[1] for x in clusters)
 return {'case_clusters':len(clusters),'matched_pairs':sum(1 for p in pairs if metric=='elapsed_s' or (p['before']['usage_complete'] and p['after']['usage_complete'])),'paired_change_percent':100*(after/before-1) if before else None,'exploratory_cluster_bootstrap_95_percent':[pct(boots,.025),pct(boots,.975)],'threshold_for_practical_reduction_percent':-10}
summary={'model':rows[0]['model'] if rows else None,'transports':sorted(set(r.get('transport','nonstream') for r in rows)),'completed_pairs':len(pairs),'unique_cases':len(case_ids),'arms':{a:aggregate(rs) for a,rs in arm_rows.items()},
 'paired_change':{m:interval(m) for m in ['elapsed_s','prompt_tokens','completion_tokens']},'per_case':[],
 'limitations':['Frozen tools are incomplete: unavailable-tool errors measure replay robustness, not live reliability.', 'Frozen get_market_data is case-scoped and ignores requested ticker; peer requests can receive a clearly labeled but mismatched case report. See manual fixture audit.','Two repetitions of a case are clustered, not independent held-out samples.','Shared model traffic/cache is uncontrolled; latency includes queueing.', 'Provider input/output usage is not a measurement of uncached GPU computation or monetary billing cost.','First-pass parser/contract checks do not simulate production repairs, reconciliation or order execution.', 'complete_with_required_step means usable artifact plus mandatory Junior note, not full all-role workflow compliance.','First-output timing includes content, reasoning or tool deltas and is observed only for completed turns; it is not server-only TTFT.']}
for cid in case_ids:
 summary['per_case'].append({'case':cid,**{a:aggregate([r for r in rows if r['case']==cid and r['arm']==a]) for a in ['before','after']}})
complete_usage_pairs=[p for p in pairs if p['before']['usage_complete'] and p['after']['usage_complete']]
summary['matched_complete_usage']={'pairs':len(complete_usage_pairs),**{a:aggregate([p[a] for p in complete_usage_pairs]) for a in ['before','after']}}
# Conditional diagnostic only: selecting completed outputs is not an unbiased
# estimate of the whole workload. Keep failure-inclusive results above.
completed_task_pairs=[p for p in complete_usage_pairs if all(qmap[key(p[a])]['complete_with_required_step'] for a in ['before','after'])]
summary['matched_both_usable_with_junior_note']={'pairs':len(completed_task_pairs),
 'interpretation':'Conditional diagnostic, excluding pairs with transport/output/Junior-note failures; not the whole-workload cost effect.',
 **{a:aggregate([p[a] for p in completed_task_pairs]) for a in ['before','after']}}
summary['memory_value_accounting']=[]
for p in pairs:
 b,a=p['before'],p['after']
 if not b['turns'] or not a['turns'] or not b['usage_complete'] or not a['usage_complete']:continue
 overhead=b['turns'][0]['usage']['prompt_tokens']-a['turns'][0]['usage']['prompt_tokens']
 tax=overhead*len(b['turns'])
 summary['memory_value_accounting'].append({'case':p['case'],'repeat':p['repeat'],
  'memory_tokens_per_first_request':overhead,'before_model_calls':len(b['turns']),'after_model_calls':len(a['turns']),
  'estimated_repeated_memory_input_tax':tax,
  'net_input_tokens_saved_by_keeping_bundle':a['prompt_tokens']-b['prompt_tokens'],
  'net_output_tokens_saved_by_keeping_bundle':a['completion_tokens']-b['completion_tokens'],
  'extra_trajectory_input_without_memory_accounting_estimate':a['prompt_tokens']-(b['prompt_tokens']-tax),
  'before_complete':qmap[key(b)]['complete_with_required_step'],'after_complete':qmap[key(a)]['complete_with_required_step']})
summary['completion_pairs']={}
for field in ['first_pass_usable','complete_with_required_step']:
 counts={'both_pass':0,'both_fail':0,'before_only_pass':0,'after_only_pass':0}
 for p in pairs:
  b=qmap[key(p['before'])][field];a=qmap[key(p['after'])][field]
  counts['both_pass' if b and a else 'before_only_pass' if b else 'after_only_pass' if a else 'both_fail']+=1
 summary['completion_pairs'][field]=counts
(BASE/'role-summary.json').write_text(json.dumps(summary,indent=2))
print(json.dumps({k:v for k,v in summary.items() if k!='per_case'},indent=2))
