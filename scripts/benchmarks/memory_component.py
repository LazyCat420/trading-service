import asyncio,json,time,os,subprocess,hashlib
from pathlib import Path
import httpx
ROOT=Path(__file__).resolve().parents[3];BASE=Path(os.environ.get('MEMORY_BENCHMARK_DIR',str(ROOT/'.scratch/memory-isolation-20260907')))
roles=['junior_analyst','fundamental_analyst','quant_analyst','bull_agent','bear_agent','regime_engine','board_of_directors']
requests=[]
for role in roles:
 memory=json.loads((BASE/(role+'-memory.json')).read_text())
 requests.append({'benchmark_id':role,'project':'vllm-trading-bot','provider':'vllm-2','agent':memory['agent'],'conversationId':'memory-ab-component:'+role,'systemPrompt':'For this response-format diagnostic, reply only with {"result":"MEMORY_AB_OK"}.\n\n'+'\n\n'.join(memory['blocks']),'messages':[{'role':'user','content':'## Ticker: TEST\nReturn the required diagnostic JSON exactly.'}]})
p=subprocess.run([os.environ['REPLAY_NODE'],'--import','tsx','scripts/benchmark-memory-filter-cases.ts'],cwd=ROOT/'lazy-agent-service',input=json.dumps(requests),text=True,capture_output=True,check=True)
cases=json.loads(p.stdout);(BASE/'component-inputs.json').write_text(json.dumps(cases,indent=2))
ENDPOINT='http://10.0.0.16:5591/vllm-shim/gold-spark'
async def run(client,model,case,arm,repeat,warmup=False):
 row={'case':case['id'],'arm':arm,'repeat':repeat,'warmup':warmup,'model':model,'started_at':time.time(),'removed_chars':case['receipt']['excluded_chars'],'payload_hash':hashlib.sha256(json.dumps(case[arm],sort_keys=True).encode()).hexdigest(),'usage':None,'first_content_s':None,'response':'','finish_reason':None}
 try:
  metrics=await client.get(ENDPOINT+'/metrics');row['load_before']=[line for line in metrics.text.splitlines() if not line.startswith('#') and any(key in line for key in ('num_requests_running','num_requests_waiting','kv_cache_usage_perc'))][:12]
 except Exception:row['load_before']=None
 start=time.monotonic()
 try:
  async with client.stream('POST',ENDPOINT+'/v1/chat/completions',json={'model':model,'messages':case[arm],'temperature':0,'min_p':0,'max_tokens':64,'stream':True,'stream_options':{'include_usage':True},'chat_template_kwargs':{'enable_thinking':False}}) as response:
   response.raise_for_status()
   async for line in response.aiter_lines():
    if not line.startswith('data: ') or line[6:]=='[DONE]':continue
    event=json.loads(line[6:]);usage=event.get('usage')
    if usage:row['usage']=usage
    for choice in event.get('choices',[]):
     text=(choice.get('delta') or {}).get('content') or ''
     if text:
      if row['first_content_s'] is None:row['first_content_s']=time.monotonic()-start
      row['response']+=text
     if choice.get('finish_reason'):row['finish_reason']=choice['finish_reason']
  try:row['valid']=json.loads(row['response'])=={'result':'MEMORY_AB_OK'}
  except Exception:row['valid']=False
 except Exception as e:row.update(error=f'{type(e).__name__}: {e}',valid=False)
 row['elapsed_s']=time.monotonic()-start
 return row
async def main():
 rows=[]
 async with httpx.AsyncClient(timeout=180) as client:
  models=(await client.get(ENDPOINT+'/v1/models')).json();model=models['data'][0]['id'];(BASE/'component-model.json').write_text(json.dumps(models,indent=2))
  order=[(cases[0],arm,-1,True) for arm in ('before','after')]
  order += [(case,arm,rep,False) for case in cases for rep in (0,1) for arm in (('before','after') if rep==0 else ('after','before'))]
  for case,arm,rep,warmup in order:
   row=await run(client,model,case,arm,rep,warmup);rows.append(row);(BASE/'component-results.json').write_text(json.dumps(rows,indent=2))
   print(json.dumps({k:row[k] for k in ('case','arm','repeat','warmup','valid','first_content_s','elapsed_s','usage')}),flush=True)
asyncio.run(main())
