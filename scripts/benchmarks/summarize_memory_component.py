"""Summarize completed fixed-output component probes without exposing prompts."""
import json,os,statistics
from pathlib import Path
ROOT=Path(__file__).resolve().parents[3]
BASE=Path(os.environ.get('MEMORY_BENCHMARK_DIR',str(ROOT/'.scratch/memory-isolation-20260907')))
rows=json.loads((BASE/'component-results.json').read_text())
scored=[r for r in rows if not r['warmup']]
roles=list(dict.fromkeys(r['case'] for r in scored))
def aggregate(rs):
    usage=[r for r in rs if r.get('usage')]
    first=[r['first_content_s'] for r in rs if r.get('first_content_s') is not None]
    return {'calls':len(rs),'valid':sum(r['valid'] for r in rs),
        'prompt_tokens_observed':sum(r['usage'].get('prompt_tokens',0) for r in usage),
        'completion_tokens_observed':sum(r['usage'].get('completion_tokens',0) for r in usage),
        'usage_missing':len(rs)-len(usage),
        'median_elapsed_s_all_attempts':statistics.median(r['elapsed_s'] for r in rs) if rs else None,
        'mean_elapsed_s_all_attempts':statistics.mean(r['elapsed_s'] for r in rs) if rs else None,
        'median_first_content_s_observed':statistics.median(first) if first else None}
pairs=[]
for role in roles:
    for repeat in sorted(set(r['repeat'] for r in scored if r['case']==role)):
        arms={a:next((r for r in scored if r['case']==role and r['repeat']==repeat and r['arm']==a),None) for a in ('before','after')}
        if all(arms.values()):pairs.append({'case':role,'repeat':repeat,**arms})
summary={'experiment':'memory-only-component','model':rows[0]['model'] if rows else None,
    'scored_calls':len(scored),'warmup_calls':len(rows)-len(scored),'completed_pairs':len(pairs),
    'limitations':['Fixed diagnostic output measures memory input overhead, not role quality or production cycle speed.',
        'Shared backend load/cache is uncontrolled; failures remain in denominators.',
        'This summary does not infer whether another test overlapped; consult the run protocol.'],
    'arms':{a:aggregate([r for r in scored if r['arm']==a]) for a in ('before','after')},'roles':[]}
for role in roles:
    rs=[r for r in scored if r['case']==role]
    summary['roles'].append({'role':role,'removed_chars':rs[0]['removed_chars'],**{
        a:{**aggregate([r for r in rs if r['arm']==a]),
           'prompt_tokens_per_call':sorted(set(r['usage']['prompt_tokens'] for r in rs if r['arm']==a and r.get('usage')))} for a in ('before','after')}})
matched=[p for p in pairs if p['before'].get('usage') and p['after'].get('usage')]
summary['matched_complete_usage']={'pairs':len(matched),**{a:aggregate([p[a] for p in matched]) for a in ('before','after')}}
summary['measurements']=rows
(BASE/'component-summary.json').write_text(json.dumps(summary,indent=2))
print(json.dumps({k:v for k,v in summary.items() if k!='measurements'},indent=2))
