"""Paired offline role replay. All tools use frozen evidence/local state only."""
import asyncio,copy,hashlib,json,os,subprocess,time
from pathlib import Path
import httpx
import sys
sys.path.append("/usr/lib/python3/dist-packages")
import jsonschema
from memory_stream import complete_stream
ROOT=Path(__file__).resolve().parents[3];BASE=Path(os.environ.get('MEMORY_BENCHMARK_DIR',str(ROOT/'.scratch/memory-isolation-20260907')))
ENDPOINT='http://10.0.0.16:5591/vllm-shim/gold-spark'
capture=json.loads((BASE/'role-capture.json').read_text());requests=[]
for c in capture:
 memory=json.loads((BASE/(c['role'][3:]+'-memory.json')).read_text())
 requests.append({'benchmark_id':c['id'],'project':'vllm-trading-bot','provider':'vllm-2','agent':memory['agent'],'conversationId':'memory-ab-role:'+c['id'],'systemPrompt':c['system']+'\n\n'+'\n\n'.join(memory['blocks']),'messages':[{'role':'user','content':c['user']}]})
p=subprocess.run([os.environ['REPLAY_NODE'],'--import','tsx','scripts/benchmark-memory-filter-cases.ts'],cwd=ROOT/'lazy-agent-service',input=json.dumps(requests),text=True,capture_output=True,check=True)
cases=json.loads(p.stdout)
raw=json.loads((ROOT/'lazy-agent-service/tool_schemas.json').read_text());schema={x.get('function',x)['name']:x.get('function',x) for x in raw}
required={'desk_note':['summary','key_findings','data_gaps','confidence','triage_recommendation'],'fundamental_report':['summary','data_gaps','confidence'],'quant_report':['summary','data_gaps','confidence'],'bull_argument':['summary','confidence'],'bear_rebuttal':['summary','confidence'],'regime_classification':['regime','confidence','factors','forward_call'],'final_decision':['action','confidence','reasoning','entry_mode','trigger_purpose']}
for case,c in zip(cases,capture):
 case.update({k:c[k] for k in ('role','ticker','artifact_type','sources','max_tokens','max_turns')})
 case['tools']=[{'type':'function','function':{k:schema[n][k] for k in ('name','description','parameters') if k in schema[n]}} for n in c['tool_names'] if n in schema]
 for tool in case['tools']:jsonschema.Draft7Validator.check_schema(tool['function'].get('parameters',{}))
 case['missing_schemas']=[n for n in c['tool_names'] if n not in schema]
 case['required_fields']=required[c['artifact_type']]
(BASE/'role-inputs.json').write_text(json.dumps(cases,indent=2))
def tool_result(case,call,whiteboard):
 name=call['function']['name']
 try:args=json.loads(call['function'].get('arguments') or '{}')
 except Exception:return {'error':'INVALID_JSON_ARGUMENTS'}
 if name not in [t['function']['name'] for t in case['tools']]:return {'error':'NOT_WHITELISTED'}
 import jsonschema
 try:jsonschema.validate(args,next(t['function'].get('parameters',{}) for t in case['tools'] if t['function']['name']==name))
 except jsonschema.ValidationError as e:return {'error':'INVALID_ARGUMENTS','detail':e.message}
 if name=='whiteboard_write':whiteboard[args.get('section','')]=args.get('content');return {'success':True,'offline':True}
 if name=='whiteboard_read':return {'offline':True,'sections':whiteboard}
 mapping={'get_market_data':'data_report','get_technical_indicators':'technical_baseline_context','get_fundamentals':'fundamental_context','get_portfolio_state':'portfolio_context','get_macro_indicators':'macro_briefing'}
 key=mapping.get(name)
 if key and key in case['sources']:return {'offline_frozen_evidence':True,'source':key,'data':case['sources'][key]}
 return {'error':'OFFLINE_EVIDENCE_UNAVAILABLE','message':'This frozen replay contains no recorded result for this tool/query. Treat this as unavailable evidence; no external action was performed.'}
def grade(row,case):
 try:obj=json.loads(row['response']);is_object=isinstance(obj,dict)
 except Exception:obj={};is_object=False
 if not is_object:obj={}
 row['valid_json']=is_object
 row['missing_fields']=[k for k in case['required_fields'] if k not in obj]
 row['required_fields_pass']=is_object and not row['missing_fields']
 row['whiteboard_written']=row['whiteboard_writes']>0
 row['artifact']=obj
 # Exact frozen fact checks are presence probes, not a blanket correctness score.
 checks={}
 if case['role']=='v3_regime_engine':checks['vix_matches_frozen']=obj.get('vix_level')==14.53
 if case['role']=='v3_board_of_directors':
  size=obj.get('position_size_pct');checks['size_within_frozen_ceiling']=isinstance(size,(int,float)) and 0<=size<=0.6
  checks['entry_mode_enum']=obj.get('entry_mode') in ['enter_now','enter_on_condition','watch_only']
 row['fact_checks']=checks
async def run(client,model,case,arm,repeat):
 messages=copy.deepcopy(case[arm]);whiteboard={};start=time.monotonic()
 row={'case':case['id'],'arm':arm,'repeat':repeat,'transport':'stream','model':model,'started_at':time.time(),'response':'','turns':[],'whiteboard_writes':0,'prompt_tokens':0,'completion_tokens':0,'tool_calls':0,'tool_errors':0,'usage_complete':True,'stop':'unknown'}
 try:
  for turn in range(case['max_turns']):
   load=None
   try:
    metrics=await client.get(ENDPOINT+'/metrics',timeout=8)
    load=[line for line in metrics.text.splitlines() if not line.startswith('#') and any(key in line for key in ('num_requests_running','num_requests_waiting','kv_cache_usage_perc'))][:12]
   except Exception:pass
   t=time.monotonic();payload={'model':model,'messages':messages,'tools':case['tools'],'temperature':0,'min_p':0,'max_tokens':case['max_tokens'],'chat_template_kwargs':{'enable_thinking':False}}
   def first_output(first,headers):
    print(json.dumps({'first_output':case['id'],'arm':arm,'repeat':repeat,'turn':turn+1,'first_delta_s':first,'headers_s':headers}),flush=True)
   remaining=900-(time.monotonic()-start)
   if remaining<=0:raise TimeoutError('900-second role deadline exceeded')
   choice=await asyncio.wait_for(complete_stream(client,ENDPOINT+'/v1/chat/completions',payload,first_output),timeout=remaining)
   message=choice['message'];usage=choice.get('usage') or {}
   if not usage:row['usage_complete']=False
   row['prompt_tokens']+=usage.get('prompt_tokens',0);row['completion_tokens']+=usage.get('completion_tokens',0)
   calls=message.get('tool_calls') or [];row['response']=message.get('content') or ''
   event={'load_before':load,'first_delta_s':choice['first_delta_s'],'headers_s':choice['headers_s'],'elapsed_s':time.monotonic()-t,'usage':usage,'finish_reason':choice.get('finish_reason'),'message':message,'tool_results':[]}
   row['turns'].append(event)
   print(json.dumps({'progress':case['id'],'arm':arm,'turn':turn+1,'elapsed_s':event['elapsed_s'],'usage':usage,'tool_names':[x['function']['name'] for x in calls]}),flush=True)
   if not calls:row['stop']=choice.get('finish_reason');break
   messages.append({k:message[k] for k in ['role','content','tool_calls'] if k in message})
   for call in calls:
    result=tool_result(case,call,whiteboard);row['tool_calls']+=1;row['tool_errors']+=int('error' in result)
    if call['function']['name']=='whiteboard_write' and result.get('success'):row['whiteboard_writes']+=1
    event['tool_results'].append(result);messages.append({'role':'tool','tool_call_id':call['id'],'content':json.dumps(result)})
   if turn==case['max_turns']-1:row['stop']='max_turns'
 except Exception as e:row.update(error=f'{type(e).__name__}: {e}',stop='error',usage_complete=False)
 row['elapsed_s']=time.monotonic()-start;grade(row,case)
 return row
async def main():
 rows=[]
 async with httpx.AsyncClient(timeout=900) as client:
  model=(await client.get(ENDPOINT+'/v1/models')).json()['data'][0]['id']
  for repeat in range(2):
   for i,case in enumerate(cases):
    for arm in (('before','after') if (i+repeat)%2==0 else ('after','before')):
     row=await run(client,model,case,arm,repeat);rows.append(row);(BASE/'role-results.json').write_text(json.dumps(rows,indent=2))
     print(json.dumps({k:v for k,v in row.items() if k not in ['turns','response','artifact']}),flush=True)
asyncio.run(main())
