"""Summarize retained model runs; do not equate transport success with quality."""
import argparse,json
from collections import Counter
from pathlib import Path

parser=argparse.ArgumentParser();parser.add_argument('cohort',type=Path);args=parser.parse_args()
rows=[json.loads(p.read_text()) for p in sorted(args.cohort.glob('[0-9][0-9].json'))]
if not rows:raise SystemExit('No completed benchmark rows; no quality claim is possible.')
summary={'rows':len(rows),'completed':sum('outcome' in r for r in rows),'live_calls':sum(c['kind'].startswith('live_') for r in rows for c in r.get('calls',[])),
         'http_200':sum(c.get('http_status')==200 for r in rows for c in r.get('calls',[])),
         'first_pass_consistent':sum(len(r.get('calls',[]))==1 and r.get('audit',{}).get('status')=='consistent' for r in rows),
         'final_evidence_consistent':sum(r.get('audit',{}).get('status')=='consistent' for r in rows),
         'error_kinds':dict(Counter(e['kind'] for r in rows for e in r.get('audit',{}).get('errors',[]))),
         'limitation':'Counts evidence-contract consistency only. Independently review every rationale and research answer against source facts before claiming improved financial reasoning.'}
print(json.dumps(summary,indent=2))
