"""Offline capture utility. Explicit opt-in; never calls a model or writes stores."""
import importlib,json,os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock,patch
import pytest
from app.v3.shared_desk import SharedDesk
from app.v3.agent_runner import run_v3_agent
from app.agents.tool_whitelists import get_agent_budget_turns
from app.services.learning.policy import BASELINES
BASE=Path(__file__).resolve().parents[3]/'.scratch/memory-isolation-20260907'
CORPUS=BASE.parent/'learning-revamp-20260907/replay-final/corpus.json'
@pytest.mark.skipif(os.environ.get('MEMORY_AUDIT_CAPTURE')!='1',reason='explicit offline capture utility')
@pytest.mark.asyncio
async def test_capture_only():
    captured=[]
    for case in json.loads(CORPUS.read_text()):
        role=case['role'].removeprefix('v3_')
        module=importlib.import_module('app.v3.agents.'+role)
        if role=='board_of_directors':
            module=SimpleNamespace(AGENT_NAME=module.AGENT_NAME,ARTIFACT_TYPE=module.ARTIFACT_TYPE,TOOL_WHITELIST=module.TOOL_WHITELIST,SYSTEM_PROMPT=module.get_persona_prompt('CONTRADICTORY'))
        desk=SharedDesk(ticker=case['ticker'],cycle_id='offline-memory-audit')
        desk.cycle_metadata={'decision_contract_version':1,'held':case['held'],'position':case['position'],'as_of':case['as_of']}
        for key,value in case['sources'].items():
            if hasattr(desk,key) and key!='data_report':
                setattr(desk,key,json.loads(value) if isinstance(value,str) and value.startswith('{') else value)
            else:desk.cycle_metadata[key]=value
        desk.cycle_metadata['book_brief_context']=case['sources'].get('portfolio_context','')
        async def capture(**kwargs):
            captured.append({'id':case['id'],'role':case['role'],'ticker':case['ticker'],'artifact_type':case['artifact_type'],
                'system':kwargs['system_prompt'],'user':kwargs['user_prompt'],'max_tokens':kwargs['max_tokens'],
                'max_turns':get_agent_budget_turns(case['role'],True),'tool_names':list(module.TOOL_WHITELIST),
                'sources':case['sources'],'context_delivery':desk.cycle_metadata.get('context_delivery')})
            raise RuntimeError('OFFLINE_CAPTURE_STOP_BEFORE_MODEL')
        prefix='## Agent Skill Guidance (SkillOpt)\n'+BASELINES.get(case['role'],'')+'\n\n'
        with patch('app.agents.base_agent.run_agent',new=AsyncMock(side_effect=capture)),patch('app.autoresearch.skill_loader.load_skill_prefix',return_value=prefix),patch('app.services.learning.receipts.record_delivery'),patch('app.v3.agent_chat.pending_directives',return_value=[]):
            await run_v3_agent(desk=desk,agent_module=module,cycle_id=desk.cycle_id,bot_id='offline',include_debate_context=bool(case['sources'].get('bull_argument') or case['sources'].get('desk_note')))
    assert len(captured)==8
    (BASE/'role-capture.json').write_text(json.dumps(captured,indent=2,default=str))
