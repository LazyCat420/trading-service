"""Opt-in live repair experiment; ordinary test runs never contact a model."""
import json, os, uuid, hashlib
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
import pytest

pytestmark = pytest.mark.skipif(os.getenv('RUN_FROZEN_BOARD_REPAIR') != '1', reason='explicit live experiment only')

@pytest.mark.asyncio
@pytest.mark.parametrize('index', [4,5,6,9,11])
async def test_live_repair(index, live_http):
    import httpx
    from app.v3 import data_trace
    from app.v3.shared_desk import SharedDesk
    from app.v3.agent_runner import run_v3_agent
    from app.v3.agents import board_of_directors as board
    from app.v3.decision_contract import entry_errors
    source = Path(__file__).resolve().parents[2]/'docs/benchmarks/evidence/board-memory-proxy-2026-09-11'
    out = source.parent/'board-failure-isolation-2026-09-11'/os.getenv('FROZEN_REPAIR_COHORT','live-repairs')
    out.mkdir(exist_ok=True)
    target=out/f'{index:02}.json'
    assert not target.exists(), 'Preserve every live attempt'
    manifest=json.loads((source/'manifest.json').read_text())
    plan=manifest['plan'][index-1]
    frozen=json.loads(next(source.glob(f'{index:02}-*.json')).read_text())
    desk=SharedDesk(ticker='EVLT',cycle_id='frozen-live-repair-'+uuid.uuid4().hex[:12])
    desk.cycle_metadata={'decision_contract_version':1,'held':index==9,'data_report':plan['body']['messages'][0]['content']}
    module=SimpleNamespace(AGENT_NAME=board.AGENT_NAME,ARTIFACT_TYPE=board.ARTIFACT_TYPE,
                           TOOL_WHITELIST=board.TOOL_WHITELIST,SYSTEM_PROMPT=plan['body']['systemPrompt'])
    record={'runner_sha256':hashlib.sha256(Path('app/v3/agent_runner.py').read_bytes()).hexdigest(),'source_index':index,'calls':[], 'interpretation':'Frozen first output replayed; subsequent repair uses actual production runner prompt and live model. Frozen evidence delivered as desk data_report. Persistence blocked by test fixtures and mocks. No tools or orders.'}
    async def model(**kwargs):
        if not record['calls']:
            record['calls'].append({'kind':'frozen_first_response'})
            return {'response':frozen['response'],'tokens_used':80,'loops_used':1,'stop_reason':'completed'}
        assert len(record['calls'])==1 and kwargs['enable_tools'] is False
        body={**plan['body'],'conversationId':str(uuid.uuid4()),'systemPrompt':kwargs['system_prompt'],
              'messages':[{'role':'user','content':kwargs['user_prompt']}], 'maxTokens':kwargs['max_tokens']}
        call={'kind':'live_toolless_repair','body':body}
        record['calls'].append(call)
        target.write_text(json.dumps(record,indent=2))
        async with httpx.AsyncClient(timeout=180,trust_env=False) as client:
            response=await client.post(manifest['endpoint'],json=body,headers={'x-project':'vllm-trading-bot','x-username':'lazycat'})
        call['http_status']=response.status_code
        response.raise_for_status();data=response.json()
        call.update(response=data.get('finalText') or data.get('text'),usage=data.get('usage'),model=data.get('model'))
        target.write_text(json.dumps(record,indent=2))
        return {'response':call['response'],'tokens_used':data.get('usage',{}).get('outputTokens',0),'loops_used':1,'stop_reason':'completed'}
    with patch('app.agents.base_agent.run_agent',side_effect=model), \
         patch.object(data_trace.mongo_store,'insert_docs'),patch.object(data_trace.mongo_store,'update_docs'):
        outcome=await run_v3_agent(desk,module,cycle_id=desk.cycle_id,bot_id='test')
    record.update(outcome=outcome.value,artifact=desk.final_decision,repair_errors=desk.cycle_metadata.get('decision_contract_repair_errors'))
    target.write_text(json.dumps(record,indent=2,default=str))
    if len(record['calls'])==1:
        assert desk.final_decision and index!=9
        assert desk.final_decision['action']==frozen['artifact']['action']
        assert desk.final_decision['reasoning']==frozen['artifact']['reasoning']
    else:
        assert len(record['calls'])==2
        assert record['calls'][1].get('http_status') == 200, 'Repair did not reach provider'
    if desk.final_decision:assert entry_errors(desk.final_decision)==[]
