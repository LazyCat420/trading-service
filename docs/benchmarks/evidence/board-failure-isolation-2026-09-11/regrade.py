"""Offline corrected review; original requests, responses and reviews stay intact."""
import hashlib, json, logging
from pathlib import Path
logging.disable(logging.CRITICAL)
from app.utils.text_utils import parse_json_response
from app.v3.agent_runner import _parse_artifact
from app.v3.decision_contract import entry_errors
OUT=Path(__file__).resolve().parent
SOURCE=OUT.parent/'board-memory-proxy-2026-09-11'
old=json.loads((SOURCE/'review.json').read_text())
rows=[]
extras={
 2:['Answer says price 100 is 2% above support 95 and 18% below resistance 115; both supplied percentages are wrong.'],
 3:['Answer equates 6.9% of 100000 with 7500; correct amount is 6900.','Range answer says 20% from support and contradicts itself about resistance; correct location is 25% from support.','PEG answer initially says unavailable despite supplied EPS growth and its own correct calculation.'],
}
for n,r in enumerate(old['rows'],1):
 d=json.loads((SOURCE/r['file']).read_text());raw=parse_json_response(d['response'])
 answers=raw.get('research_answers',[])
 a=_parse_artifact(d['response'],'final_decision','board')
 rows.append({'file':r['file'],'case':r['case'],'arm':r['arm'],
              'response_sha256':hashlib.sha256(d['response'].encode()).hexdigest(),
              'research_answer_count':len(answers),'research_answers':answers,
              'schema_valid':r['schema_valid'],'entry_valid':r['entry_valid'],
              'factual_errors':r['factual_errors']+extras.get(n,[]),
              'correct_observations':r['correct_observations'],
              'structural_errors':d['schema_errors']+d['entry_errors'],
              'notes':'All requested answers are present. Prior claims of missing arrays or omitted independence/units answers are withdrawn. Presence does not imply correctness.'})
summary={arm:{'attempts':4,'schema_valid':sum(r['schema_valid'] for r in rows if r['arm']==arm),
 'entry_valid':sum(r['entry_valid'] for r in rows if r['arm']==arm),
 'all_three_answers_present':sum(r['research_answer_count']==3 for r in rows if r['arm']==arm),
 'structurally_usable_without_identified_factual_error':sum(r['schema_valid'] and r['entry_valid'] and not r['factual_errors'] for r in rows if r['arm']==arm)} for arm in ('current','no_method','stale_memory')}
(OUT/'original-review-corrected.json').write_text(json.dumps({'supersedes':str(SOURCE.name)+'/review.json','reason':'The original reader discarded sibling research_answers when extracting final_decision. All 12 raw responses contain three answers. Review now considers both the answers and the decision rationale. Original evidence unchanged.','summary':summary,'rows':rows},indent=2))
paired=json.loads((OUT/'review.json').read_text())
for r in paired['rows']:
 d=json.loads((OUT/r['file']).read_text());raw=parse_json_response(d['response'])
 r['research_answers_present']=isinstance(raw.get('research_answers'),list)
 r['manual_findings']=[x.replace('Missing requested research_answers; ','').replace('Missing requested research_answers;','').replace('but missing research_answers.','and all research answers are present.').replace('but missing research_answers','and all research answers are present') for x in r['manual_findings']]
 if r['file'].startswith('09-'):
  r['manual_findings'][0]='Current and conditional reward/risk are correct in both prose and the three raw research_answers (0.75 and 6); HOLD/entry-purpose mismatch remains.'
 r['raw_research_answers']=raw.get('research_answers')
paired['summary']={arm:{'attempts':5,'schema_and_entry_pass':sum(r['schema_and_entry_pass'] for r in paired['rows'] if r['arm']==arm),'research_answers_present':sum(r['research_answers_present'] for r in paired['rows'] if r['arm']==arm)} for arm in ('baseline','clarified')}
paired['correction']='Answers evaluated from full raw response, including siblings of final_decision. Original review.json retained for audit.'
(OUT/'paired-review-corrected.json').write_text(json.dumps(paired,indent=2))
cohorts={}
for name in ('live-repairs','live-repairs-after-parser','live-repairs-explicit-correction','live-repairs-final'):
 records=[json.loads(f.read_text()) for f in sorted((OUT/name).glob('*.json'))]
 cohorts[name]={'attempts':len(records),'http_200':sum(r['calls'][1].get('http_status')==200 for r in records),'accepted':sum(bool(r.get('artifact')) for r in records),'outcomes':[{k:r.get(k) for k in ('source_index','outcome','repair_errors')} for r in records]}
(OUT/'repair-summary.json').write_text(json.dumps({'cohorts':cohorts,'limitations':'Each cohort is one attempt per selected original failure, same provider/model and frozen first response, with successive local harness versions. No general success-rate claim. Schema success can retain wrong factual claims. Blocked-network-preflight contains five zero-provider-call fixture failures, excluded from model counts.','models':sorted({json.loads(f.read_text())['calls'][1].get('model') for name in cohorts for f in (OUT/name).glob('*.json')})},indent=2))
files=[*SOURCE.glob('*.json'),*SOURCE.glob('*.py')]
(OUT/'original-evidence-hashes.json').write_text(json.dumps({f.name:hashlib.sha256(f.read_bytes()).hexdigest() for f in sorted(files)},indent=2))
print(json.dumps({'original_corrected':summary,'paired_corrected':paired['summary'],'repairs':cohorts},indent=2))
