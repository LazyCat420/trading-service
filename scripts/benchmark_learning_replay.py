"""Frozen task replay of baseline vs reviewed skills; no production writes/tools."""
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import argparse, asyncio, hashlib, importlib, json, time, subprocess, os
from copy import deepcopy
import httpx
from app.services.learning.policy import BASELINES
from app.v3.artifacts import validate_artifact
from app.utils.text_utils import parse_json_response

ROLE_CASES = [('junior_analyst','LULU'),('junior_analyst','STX'),('fundamental_analyst','LULU'),
              ('quant_analyst','STX'),('bull_agent','LULU'),('bear_agent','LULU'),
              ('regime_engine','LULU'),('board_of_directors','LULU')]
TOOLS = [{'type':'function','function':{'name':name,'description':'Read frozen evidence for this replay task. No newer information is available.',
        'parameters':{'type':'object','properties':{'ticker':{'type':'string'},'query':{'type':'string'}},'required':['ticker']}}}
        for name in ('get_market_data','get_sec_filings','lazy_web_search','get_portfolio_state')]


def freeze(source, baseline, output):
    saved=json.loads(source.read_text())
    desks={}
    for row in saved['shared_desk']:
        desk=json.loads(row['desk_data']) if isinstance(row['desk_data'],str) else row['desk_data']
        desks[desk['ticker']]=desk
    skills={r['agent']:r['text'] for r in json.loads(baseline.read_text())['skills']}
    corpus=[]
    for n,(role,ticker) in enumerate(ROLE_CASES):
        module=importlib.import_module('app.v3.agents.'+role)
        desk=desks[ticker]; metadata=desk['cycle_metadata']
        sources={key:str(metadata.get(key) or '') for key in ('data_report','technical_baseline_context','fundamental_context','macro_briefing','quant_math_context','portfolio_context','research_answers_context')}
        if role in {'bull_agent','bear_agent','board_of_directors'}:
            for key in ['desk_note','fundamental_report','quant_report']:
                if desk.get(key):sources[key]=json.dumps(desk[key],ensure_ascii=False)
        if role in {'bear_agent','board_of_directors'} and desk.get('bull_argument'):
            sources['bull_argument']=json.dumps(desk['bull_argument'],ensure_ascii=False)
        if role=='board_of_directors':
            for key in ['bear_rebuttal','bull_defense','debate_judge','regime_classification']:
                if desk.get(key):sources[key]=json.dumps(desk[key],ensure_ascii=False)
        sources={k:v for k,v in sources.items() if v}
        system=getattr(module,'SYSTEM_PROMPT',None)
        if not system:
            system=module.get_persona_prompt('CONTRADICTORY')
        task={'id':f'{n+1:02}-{role}-{ticker}', 'role':module.AGENT_NAME,'ticker':ticker,
              'artifact_type':module.ARTIFACT_TYPE,'system':system,'sources':sources,
              'baseline_skill':skills[module.AGENT_NAME],'reviewed_skill':BASELINES[module.AGENT_NAME],
              'held':metadata.get('held',False),'position':metadata.get('position'), 'as_of':metadata.get('timestamp')}
        corpus.append(task)
    output.write_text(json.dumps(corpus,indent=2,ensure_ascii=False))
    return corpus


def assess(text, task):
    artifact=parse_json_response(text)
    if not isinstance(artifact,dict):return {'valid':False,'errors':['not a JSON object'],'citations_valid':False,'policy_violations':[]}
    if task['artifact_type'] in artifact and isinstance(artifact[task['artifact_type']],dict):artifact=artifact[task['artifact_type']]
    errors=validate_artifact(task['artifact_type'], deepcopy(artifact))
    citations=artifact.get('benchmark_evidence') or []
    norm=lambda s:' '.join(str(s).split()).casefold()
    valid_citations=isinstance(citations,list) and len(citations)>=2 and all(isinstance(c,dict) and c.get('source_id') in task['sources'] and len(c.get('quote',''))>=16 and norm(c['quote']) in norm(task['sources'][c['source_id']]) for c in citations)
    violations=[]
    if task['role']=='v3_board_of_directors':
        if artifact.get('action')=='SELL' and not task['held']:violations.append('sell_without_position')
        if not 0<=float(artifact.get('position_size_pct') or 0)<=5:violations.append('position_size_exceeds_fixture_limit')
    return {'valid':not errors,'errors':errors,'citations_valid':bool(valid_citations),'citation_count':len(citations),'policy_violations':violations}

async def run_one(client, endpoint, model, task, variant):
    prefix=task['baseline_skill' if variant=='before' else 'reviewed_skill']
    system='## Agent Skill Guidance (SkillOpt)\n'+prefix+'\n\n'+task['system']
    packet=json.dumps(task['sources'],ensure_ascii=False)
    user=f"## Ticker: {task['ticker']}\n## Replay as-of: {task['as_of']}\nHeld: {task['held']}. Position: {json.dumps(task['position'])}. Maximum position size for this fixture: 5%.\n\nThe following is the complete frozen evidence available at the original task time. It includes prior analyst observations; their claims remain attributed to those sources. No later information is available. Tools return these same frozen records. Produce the required {task['artifact_type']} artifact with explicit unknowns. Add benchmark_evidence: at least two objects containing source_id and a verbatim supporting quote of 16 or more characters from a record below.\n\n## Sources\n{packet}"
    messages=[{'role':'system','content':system},{'role':'user','content':user}]
    if variant == 'after':
        request=dict(project='vllm-trading-bot',provider='vllm-2',agent='CUSTOM_'+task['role'].upper(),
                     conversationId='replay:'+task['id'], systemPrompt=system, messages=[{'role':'user','content':user}])
        result=subprocess.run([os.environ['REPLAY_NODE'],'--import','tsx','scripts/learning-boundary-replay.ts'],
          cwd=Path(__file__).resolve().parents[2]/'lazy-agent-service',input=json.dumps(request),text=True,capture_output=True,check=True)
        messages=json.loads(result.stdout)

    row={'task':task['id'],'variant':variant,'model':model,'role':task['role'],'started_at':time.time(),'turns':[],
         'system_hash':hashlib.sha256(system.encode()).hexdigest(),'user_hash':hashlib.sha256(user.encode()).hexdigest(),
         'prompt_tokens':0,'completion_tokens':0,'cached_tokens':None,'tool_calls':0,'redundant_tool_calls':0}
    start=time.monotonic();text=''
    try:
        for turn in range(4):
            req={'model':model,'messages':messages,'temperature':0,'min_p':0,'max_tokens':4096,'stream':False,'chat_template_kwargs':{'enable_thinking':False},'tools':TOOLS,'tool_choice':'auto'}
            t=time.monotonic();response=await client.post(endpoint+'/v1/chat/completions',json=req);response.raise_for_status();data=response.json()
            choice=data['choices'][0];msg=choice['message'];usage=data.get('usage') or {}
            row['turns'].append({'elapsed_s':time.monotonic()-t,'usage':usage,'model':data.get('model'),'finish_reason':choice.get('finish_reason'),'message':msg})
            row['prompt_tokens']+=usage.get('prompt_tokens',0);row['completion_tokens']+=usage.get('completion_tokens',0)
            cache=(usage.get('prompt_tokens_details') or {}).get('cached_tokens')
            if cache is not None:row['cached_tokens']=(row['cached_tokens'] or 0)+cache
            text=msg.get('content') or '';calls=msg.get('tool_calls') or []
            if not calls:break
            messages.append({k:v for k,v in msg.items() if k in {'role','content','tool_calls'}})
            for call in calls:
                row['tool_calls']+=1;row['redundant_tool_calls']+=1
                messages.append({'role':'tool','tool_call_id':call['id'],'content':packet})
        row['assessment']=assess(text,task)
    except Exception as exc:
        row['error']=f'{type(exc).__name__}: {exc}'[:500]
        row['assessment']={'valid':False,'errors':[row['error']],'citations_valid':False,'policy_violations':[]}
    row['elapsed_s']=time.monotonic()-start;row['response']=text
    return row

async def main(args):
    args.output.mkdir(parents=True,exist_ok=True)
    corpus_file=args.output/'corpus.json'
    corpus=json.loads(corpus_file.read_text()) if corpus_file.exists() else freeze(args.source,args.baseline,corpus_file)
    async with httpx.AsyncClient(timeout=240) as client:
        data=(await client.get(args.endpoint+'/v1/models')).json();model=data['data'][0]['id']
        (args.output/'model.json').write_text(json.dumps(data,indent=2))
        semaphore=asyncio.Semaphore(2)
        async def pair(n, task):
            async with semaphore:
                for variant in (['before','after'] if n%2==0 else ['after','before']):
                    target=args.output/f"{task['id']}-{variant}.json"
                    if target.exists():continue
                    row=await run_one(client,args.endpoint,model,task,variant)
                    target.write_text(json.dumps(row,indent=2,ensure_ascii=False))
                    print(json.dumps({k:row[k] for k in ['task','variant','elapsed_s','prompt_tokens','completion_tokens','tool_calls','assessment']}),flush=True)
        await asyncio.gather(*(pair(n, task) for n,task in enumerate(corpus)))

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--source',type=Path,required=True);p.add_argument('--baseline',type=Path,required=True);p.add_argument('--output',type=Path,required=True);p.add_argument('--endpoint',required=True)
    asyncio.run(main(p.parse_args()))
