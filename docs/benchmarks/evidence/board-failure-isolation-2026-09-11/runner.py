"""Paired exploratory replay of five failures. Expected answers never sent."""
import asyncio, json, hashlib, time, uuid
from pathlib import Path
from copy import deepcopy
import httpx
from app.utils.text_utils import parse_json_response
from app.v3.decision_contract import entry_errors
from app.v3.artifacts import validate_artifact
OUT=Path(__file__).resolve().parent
SOURCE=OUT.parent/'board-memory-proxy-2026-09-11'
CLARIFICATION='''
## Final output consistency check
Return one JSON object containing action, confidence, reasoning, entry_mode, trigger_purpose and dynamic_trigger. Include any requested research_answers inside that same object; answers or a rationale alone do not replace the decision.
Choose the action from the evidence, then express its timing consistently: HOLD always uses watch_only, including when waiting for a price condition. Only BUY can use enter_on_condition, and then trigger_purpose must be entry. A HOLD with a supported numeric wake uses monitor or research, never entry.
An unresolved research question is independent of an executable numeric trigger. When missing historical data or another unresolved fact has no supported numeric trigger, retain the question in resolution_condition and use trigger_purpose=none and dynamic_trigger=null. Do not invent a numeric threshold to fill the schema. Do not change the financial decision to make its timing fields valid.
'''
async def main():
 original=json.loads((SOURCE/'manifest.json').read_text())
 plan=[]
 for i,n in enumerate((4,5,6,9,11)):
  for arm in (('baseline','clarified') if i%2==0 else ('clarified','baseline')):
   row=deepcopy(original['plan'][n-1]); row.update(source_index=n,arm=arm)
   row['body']['conversationId']=str(uuid.uuid4())
   if arm=='clarified':row['body']['messages'][0]['content']+=CLARIFICATION
   plan.append(row)
 manifest={'endpoint':original['endpoint'],'source_manifest_sha256':hashlib.sha256((SOURCE/'manifest.json').read_bytes()).hexdigest(),'plan':plan,'limits':'10 serial attempts, no retries, original model/settings/evidence; selected failure cohort, exploratory paired comparison only. Candidate is an added output-contract clarification. Schema does not grade factual correctness.'}
 assert not (OUT/'manifest.json').exists()
 (OUT/'manifest.json').write_text(json.dumps(manifest,indent=2))
 async with httpx.AsyncClient(timeout=180,trust_env=False) as client:
  for i,p in enumerate(plan):
   row={k:v for k,v in p.items() if k!='body'};start=time.monotonic()
   try:
    r=await client.post(original['endpoint'],json=p['body'],headers={'x-project':'vllm-trading-bot','x-username':'lazycat'})
    row['http_status']=r.status_code;r.raise_for_status();d=r.json()
    row.update(response=d.get('finalText') or d.get('text'),usage=d.get('usage'),model=d.get('model'),conversation_id=d.get('conversationId'))
    a=parse_json_response(row['response'])
    if isinstance(a,dict) and isinstance(a.get('final_decision'),dict):a=a['final_decision']
    row['artifact']=a
    row['schema_errors']=validate_artifact('final_decision',deepcopy(a)) if isinstance(a,dict) else ['not object']
    row['entry_errors']=entry_errors(a) if isinstance(a,dict) else ['not object']
   except Exception as e:row['error']=str(e)
   row['elapsed_s']=time.monotonic()-start
   (OUT/f'{i+1:02}-{p["source_index"]}-{p["arm"]}.json').write_text(json.dumps(row,indent=2))
   print(json.dumps({k:row.get(k) for k in ('source_index','arm','schema_errors','entry_errors','error')}),flush=True)
asyncio.run(main())
