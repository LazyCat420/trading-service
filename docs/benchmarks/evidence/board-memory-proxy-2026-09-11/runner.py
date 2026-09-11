import logging
logging.disable(logging.CRITICAL)
import asyncio,json,hashlib,time,uuid
from pathlib import Path
from datetime import datetime,timezone
from copy import deepcopy
import httpx
from app.v3.agents.board_of_directors import PERSONA_JANE_STREET,AGENT_NAME
from app.services.learning.policy import BASELINES
from app.v3.shared_desk import SharedDesk
from app.v3.decision_contract import prompt_block,entry_errors
from app.v3.artifacts import validate_artifact
from app.utils.text_utils import parse_json_response
ROOT=Path.cwd();OUT=ROOT/'docs/benchmarks/evidence/board-memory-proxy-2026-09-11'
OUT.mkdir(parents=True,exist_ok=True)
corpus_path=ROOT/'tests/benchmarks/fixtures/decision_quality_v1.json'
corpus=json.loads(corpus_path.read_text())
ENDPOINT='http://10.0.0.16:5591/prism-proxy/agent?stream=false'
STALE='UNVERIFIED HISTORICAL MEMORY, ticker OTHR, 2026-08-01: Operating margin was 25 percent and all planned entries used 2.5 percent of equity. A prior memo called price 100 the midpoint of support 95 and resistance 115, used revenue growth for PEG, and treated repeated summaries as independent confirmation. These are old claims about another ticker, not current EVLT evidence or approved policy.'
plan=[]
for i,case in enumerate(corpus['cases']):
 for arm in (('current','no_method','stale_memory') if i%2==0 else ('stale_memory','no_method','current')):
  cid='bench-memory-proxy-'+uuid.uuid4().hex[:12]
  desk=SharedDesk(ticker='EVLT',cycle_id=cid);desk.cycle_metadata={'decision_contract_version':1,'held':case['held']}
  prefix=('## Agent Skill Guidance (SkillOpt)\n'+BASELINES[AGENT_NAME]+'\n\n') if arm!='no_method' else ''
  system=prefix+PERSONA_JANE_STREET
  questions=[{'id':q['id'],'question':q['question']} for q in case['questions']]
  user=f"## Ticker: EVLT\n## Cycle: {cid}\n## Frozen Board decision validation\nAs of {corpus['as_of']}. This is a synthetic evidence assessment with no tools or orders. The following evidence is complete; unavailable measurements remain unknown. Produce your final_decision JSON using the Board contract, with a research_answers array answering each supplied question and a concise evidence-based rationale. BUY, SELL, HOLD and conditional entry are permitted when supported; do not aim for a specified action. Do not count repeated reports as independent evidence.\n## Evidence\n{corpus['shared']}\n{case['facts']}\n## Questions\n{json.dumps(questions)}\n"
  if arm=='stale_memory':user+='## Past Cycle Memory\n'+STALE+'\n'
  user+=prompt_block(desk,'final_decision')
  body={'project':'vllm-trading-bot','username':'lazycat','provider':'vllm','model':'nemotron35','agent':'CUSTOM_V3_BOARD_OF_DIRECTORS','conversationId':str(uuid.uuid4()),'createSession':True,'systemPrompt':system,'messages':[{'role':'user','content':user}],'enabledTools':[],'maxTokens':8192,'maxIterations':1,'temperature':0,'functionCallingEnabled':False,'agenticLoopEnabled':False,'thinkingEnabled':False,'workspaceEnabled':False}
  plan.append({'case':case['id'],'arm':arm,'cycle_id':cid,'body':body})
manifest={'started_at':datetime.now(timezone.utc).isoformat(),'endpoint':ENDPOINT,'model':'nemotron35','corpus_sha256':hashlib.sha256(corpus_path.read_bytes()).hexdigest(),'runner_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),'plan':plan,'limits':'12 attempts, serial, one turn, 8192 output tokens, 180 seconds per attempt, no retries, tools disabled in every arm. Frozen case facts/questions; expected answers excluded from requests.','interpretation':'Separate Nemotron NAS-proxy cohort. Four cases are independent units; method ablation and stale-memory exposure only. No comparison with the older GLM cohort, native tool benchmark, learning promotion, profit or causal model ranking. Manual evidence/arithmetic/uncertainty/independence/decision consistency review required; schema success alone is insufficient.'}
assert not (OUT/'manifest.json').exists(),'Preserve existing cohort'
(OUT/'manifest.json').write_text(json.dumps(manifest,indent=2))
async def main():
 async with httpx.AsyncClient(timeout=180,trust_env=False) as c:
  for n,p in enumerate(plan):
   row={k:v for k,v in p.items() if k!='body'};row['started_at']=time.time()
   try:
    r=await c.post(ENDPOINT,json=p['body'],headers={'x-project':'vllm-trading-bot','x-username':'lazycat'});row['http_status']=r.status_code;r.raise_for_status();d=r.json()
    row.update(response=d.get('finalText') or d.get('text'),model=d.get('model'),provider=d.get('provider'),usage=d.get('usage'),conversation_id=d.get('conversationId'))
    a=parse_json_response(row['response'])
    if isinstance(a,dict) and isinstance(a.get('final_decision'),dict):a=a['final_decision']
    row['artifact']=a
    row['schema_errors']=validate_artifact('final_decision',deepcopy(a)) if isinstance(a,dict) else ['not JSON object']
    row['entry_errors']=entry_errors(a) if isinstance(a,dict) else ['not JSON object']
   except Exception as e:row['error']=type(e).__name__
   row['elapsed_s']=time.time()-row['started_at']
   (OUT/f'{n+1:02}-{p["case"]}-{p["arm"]}.json').write_text(json.dumps(row,indent=2,default=str))
   print(json.dumps({k:row.get(k) for k in ('case','arm','http_status','model','schema_errors','entry_errors','error','elapsed_s')}),flush=True)
asyncio.run(main())
