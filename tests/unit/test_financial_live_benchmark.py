"""Opt-in financial benchmark: production runner, real NAS proxy, no tools/orders."""
from copy import deepcopy
import hashlib,json,os,time,uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import pytest

pytestmark=pytest.mark.skipif(os.getenv('RUN_FINANCIAL_BENCHMARK')!='1',reason='Explicit live financial benchmark only')
ROOT=Path(__file__).resolve().parents[2]
SOURCE=ROOT/'docs/benchmarks/evidence/board-memory-proxy-2026-09-11'
ENDPOINT='http://10.0.0.16:5591/prism-proxy/agent?stream=false'
FAMILY=os.getenv('FINANCIAL_BENCH_FAMILY','original')
DEFAULT_INDICES='1,2,3,4,5,6,7,8' if FAMILY=='holdout' else '1,2,3,4,5,6,7,8,9,10,11,12'
INDICES=[int(n) for n in os.getenv('FINANCIAL_BENCH_INDICES',DEFAULT_INDICES).split(',')]


@pytest.mark.asyncio
@pytest.mark.parametrize('index',INDICES)
async def test_financial_live_benchmark(index,live_http):
    import httpx
    from app.v3 import data_trace
    from app.v3.agent_runner import run_v3_agent
    from app.v3.shared_desk import SharedDesk
    from app.v3.financial_claims import desk_status
    manifest=json.loads((SOURCE/'manifest.json').read_text())
    assert manifest['endpoint']==ENDPOINT
    plan=manifest['plan'][index-1]
    corpus=json.loads((ROOT/'tests/benchmarks/fixtures/decision_quality_v1.json').read_text())
    assert corpus['ticker']=='EVLT' and corpus['shared'].startswith('Synthetic company EVLT;')
    original=next(c for c in corpus['cases'] if c['id']==plan['case'])
    fixture=json.loads((ROOT/'tests/benchmarks/fixtures/financial_reasoning_v1.json').read_text())
    evidence=deepcopy(next(c for c in fixture['cases'] if c['id']==plan['case']))
    evidence['version']=1
    variant=os.getenv('FINANCIAL_BENCH_VARIANT','candidate')
    assert variant in ('baseline','candidate')
    source_brief=corpus['shared']+'\n'+original['facts']
    if FAMILY=='holdout':
        holdouts=json.loads((ROOT/'tests/benchmarks/fixtures/financial_reasoning_holdout_v1.json').read_text())['cases']
        evidence=deepcopy(holdouts[index-1])
        plan=next(p for p in manifest['plan'] if p['case']==evidence['base_case'] and p['arm']=='current')
        source_brief=('Synthetic EVLT source observations. Rows are metric, value, unit, as_of, source, period. '
                      'Null means unavailable, not zero. Hypothetical entries are not executable quotes.\n'+
                      json.dumps([[f[k] for k in ('metric','value','unit','as_of','source','period')] for f in evidence['facts']],separators=(',',':')))
    assert len(source_brief)<=4900, 'Never benchmark against a silently truncated source brief'
    cohort=os.environ['FINANCIAL_BENCH_COHORT']
    assert '/' not in cohort and '..' not in cohort
    out=ROOT/'docs/benchmarks/evidence/financial-reasoning-2026-09-11'/cohort
    out.mkdir(parents=True,exist_ok=True)
    target=out/f'{index:02}.json'
    assert not target.exists(),'Preserve every attempt; choose a fresh cohort name'
    mode=os.getenv('FINANCIAL_BENCH_MODE','fresh')
    assert not (FAMILY=='holdout' and mode=='frozen')
    cid='bench-financial-'+uuid.uuid4().hex[:12]
    desk=SharedDesk(ticker='EVLT',cycle_id=cid)
    desk.cycle_metadata={'decision_contract_version':1,'financial_evidence_version':1 if variant=='candidate' else 0,'held':evidence['held'],
                         'financial_evidence_record':evidence,
                         'research_questions':[{'id':q['id'],'payload':{'question':q['question']}} for q in evidence['questions']],
                         'data_report':source_brief,'timestamp':evidence['as_of']}
    if plan['arm']=='stale_memory':
        task=plan['body']['messages'][0]['content']
        desk.cycle_metadata['memory_context']=task.split('## Past Cycle Memory\n',1)[1].split('## DECISION CONTRACT',1)[0]
    module=SimpleNamespace(AGENT_NAME='v3_board_of_directors',ARTIFACT_TYPE='final_decision',
                           TOOL_WHITELIST=[],SYSTEM_PROMPT=plan['body']['systemPrompt'])
    record={'index':index,'case':evidence['id'],'arm':plan['arm'],'mode':'offline_replay' if os.getenv('FINANCIAL_BENCH_REPLAY_COHORT') else mode,'family':FAMILY,'variant':variant,'cycle_id':cid,
            'started_at':time.time(),'endpoint':ENDPOINT,'calls':[],'evidence':evidence,
            'replay_cohort':os.getenv('FINANCIAL_BENCH_REPLAY_COHORT'),
            'source_hashes':{p:hashlib.sha256((ROOT/p).read_bytes()).hexdigest() for p in
                ('app/v3/agent_runner.py','app/v3/financial_evidence.py','app/v3/financial_claims.py','app/v3/financial_reasoning.py','tests/unit/test_financial_live_benchmark.py')},
            'limits':'At most two endpoint calls (initial plus shared repair); upstream may make additional recovery attempts, recorded separately. Tools disabled, no orders; persistence mocked. Proxy retains request/session logs.',
            'ablation':{k:os.getenv(k) for k in ('FINANCIAL_BENCH_THINKING','FINANCIAL_BENCH_OUTPUT_TOKENS','FINANCIAL_BENCH_HTTP_TIMEOUT','FINANCIAL_BENCH_PHASE_TIMEOUT')},
            'comparison':'Synthetic frozen cases and original persona/method arms. Typed source evidence and financial contract are added. Expected answers/checks excluded. Acceptance alone is not reasoning quality.'}
    async def model(**kwargs):
        assert kwargs['enable_tools'] is False
        if mode=='frozen' and not record['calls']:
            frozen=json.loads(next(SOURCE.glob(f'{index:02}-*.json')).read_text())
            record['calls'].append({'kind':'frozen_initial','response':frozen['response']})
            return {'response':frozen['response'],'tokens_used':0,'loops_used':1,'stop_reason':'completed'}
        replay=os.getenv('FINANCIAL_BENCH_REPLAY_COHORT')
        if replay:
            assert '/' not in replay and '..' not in replay
            saved=json.loads((out.parent/replay/f'{index:02}.json').read_text())
            n=len(record['calls'])
            assert n<len(saved['calls']), 'Offline replay has no recorded correction'
            call=deepcopy(saved['calls'][n]);call['kind']='offline_replay'
            record['calls'].append(call)
            return {'response':call['response'],'tokens_used':0,'loops_used':1,'stop_reason':'completed'}
        assert len(record['calls'])<2,'No extra model retry'
        body={'project':'vllm-trading-bot','username':'lazycat','provider':'vllm','model':'nemotron35',
              'agent':'CUSTOM_V3_BOARD_OF_DIRECTORS','conversationId':str(uuid.uuid4()),'createSession':True,
              'systemPrompt':kwargs['system_prompt'],'messages':[{'role':'user','content':kwargs['user_prompt']}],
              'maxTokens':int(os.getenv('FINANCIAL_BENCH_OUTPUT_TOKENS',str(kwargs['max_tokens']))),'enabledTools':[],'maxIterations':1,'temperature':0,
              'functionCallingEnabled':False,'agenticLoopEnabled':False,'thinkingEnabled':os.getenv('FINANCIAL_BENCH_THINKING')=='1','workspaceEnabled':False}
        call={'kind':'live_initial' if not record['calls'] else 'live_repair','body':body,'started_at':time.time()}
        record['calls'].append(call);target.write_text(json.dumps(record,indent=2,default=str))
        try:
            async with httpx.AsyncClient(timeout=float(os.getenv('FINANCIAL_BENCH_HTTP_TIMEOUT','180')),trust_env=False) as client:
                response=await client.post(ENDPOINT,json=body,headers={'x-project':'vllm-trading-bot','x-username':'lazycat'})
            call['http_status']=response.status_code;response.raise_for_status();data=response.json()
            call.update(response=data.get('finalText') or data.get('text'),usage=data.get('usage'),model=data.get('model'))
            return {'response':call['response'],'tokens_used':data.get('usage',{}).get('outputTokens',0),'loops_used':1,'stop_reason':'completed'}
        except Exception as exc:
            call['error']=type(exc).__name__
            raise
        finally:
            call['elapsed_s']=time.time()-call['started_at'];target.write_text(json.dumps(record,indent=2,default=str))
    with patch('app.agents.base_agent.run_agent',side_effect=model), \
         patch('app.autoresearch.skill_loader.load_skill_prefix',return_value=''), \
         patch.object(data_trace.mongo_store,'insert_docs'),patch.object(data_trace.mongo_store,'update_docs'), \
         patch.object(data_trace.mongo_store,'find_docs',return_value=[]):
        outcome=await run_v3_agent(desk,module,cycle_id=cid,bot_id='test',timeout_seconds=float(os.getenv('FINANCIAL_BENCH_PHASE_TIMEOUT','240')),
            custom_instructions='Synthetic frozen evidence assessment. No tools or orders. Return the complete final_decision JSON and research_answers for every supplied question. Do not aim for a specified action.'+(' Follow the financial evidence contract.' if variant=='candidate' else ''))
    record.update(telemetry=desk.agent_telemetry,outcome=outcome.value,artifact=desk.final_decision,audit=desk_status(desk),elapsed_s=time.time()-record['started_at'])
    target.write_text(json.dumps(record,indent=2,default=str))
    print(json.dumps({'index':index,'outcome':record['outcome'],'audit':record['audit']['status'],'calls':len(record['calls'])}),flush=True)
    assert record['calls']
    assert all(c.get('http_status')==200 for c in record['calls'] if c['kind'].startswith('live_'))
