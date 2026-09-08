"""Explicitly invoked model benchmark; excluded from normal pytest discovery.

All persistence is in real_mongo's disposable database. Model/tool transport is
replaced; the V3 prompt assembler, parser, whiteboard and research receipts run.
"""
import asyncio
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sys
import time
from unittest.mock import AsyncMock
from types import SimpleNamespace

import pytest

pytestmark = [pytest.mark.real_mongo, pytest.mark.asyncio]
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'scripts/benchmarks'))
sys.path.append('/usr/lib/python3/dist-packages')
from memory_stream import complete_stream

PILOT = os.environ.get('TICKET_BENCH_PILOT') == '1'
ENABLED = os.environ.get('TICKET_BENCH_RUN') == '1' or PILOT
OUT = Path(os.environ.get('TICKET_BENCH_OUTPUT', '/tmp/ticket-harness-replay'))
ENDPOINT = 'http://10.0.0.16:5591/vllm-shim/gold-spark'
CASES = ('complete', 'historical_missing')
ORDER = [(case, repeat, arm) for repeat in range(2) for i, case in enumerate(CASES)
         for arm in (('no_tickets', 'tickets') if (i + repeat) % 2 == 0 else ('tickets', 'no_tickets'))]
if PILOT:
    ORDER = [('complete', 0, 'tickets'), ('historical_missing', 0, 'no_tickets')]


def fixture(case):
    facts = ('Synthetic evaluation company FIXT. Observation date 2026-09-03. '
             'Closing price is 100.00. Operating margin is 12.4 percent. '
             'Debt to equity is 0.4. Revenue growth is 12 percent. '
             'Forward P/E is 15. Earnings date is unavailable. '
             'Verified support is 95.00 and resistance is 115.00. '
             'No position is held. No live order may be placed.')
    history = (' Historical share volumes: 2026-08-11=1500000, 2026-08-12=1400000, '
               '2026-08-13=1300000, 2026-08-14=1200000, 2026-08-17=1100000.')
    facts += history if case == 'complete' else ' Only the 2026-09-03 volume of 1100000 shares is available. No August volume history is supplied.'
    questions = [
        ('q-margin', 'Is operating margin above ten percent in the supplied report?'),
        ('q-debt', 'Is debt to equity below one in the supplied report?'),
        ('q-history', 'As of August 17, 2026, was share volume decreasing over the preceding five supplied trading sessions?'),
    ]
    items = [{'id': qid, 'ticker': 'FIXT', 'created_at': datetime(2026, 8, 17, 20, tzinfo=timezone.utc),
              'source_agent': 'fixture_reviewer', 'payload': {'question': question},
              'status': 'processing', 'owner_cycle_id': 'fixture-owner', 'lease_token': qid,
              'attempts': 1} for qid, question in questions]
    return facts, items


@pytest.mark.skipif(not ENABLED, reason='Explicit TICKET_BENCH_RUN=1 required for model benchmark')
@pytest.mark.timeout(1900)  # Three 600-second role deadlines plus fixture/receipt overhead.
@pytest.mark.parametrize('case,repeat,arm', ORDER)
async def test_replay(real_mongo, live_http, monkeypatch, case, repeat, arm):
    assert real_mongo.name.startswith('trading_bot_pytest_harness_')
    from app.db import mongo_store, mongo
    monkeypatch.setattr(mongo_store, '_BACKENDS', {'*': 'mongo'})
    monkeypatch.setattr(mongo, 'get_mongo_db', lambda: real_mongo)
    from app.autoresearch import skill_loader
    monkeypatch.setattr(skill_loader, 'load_skill_prefix', lambda _: '')
    from app.v3 import agent_runner
    monkeypatch.setattr(agent_runner, '_get_tool_playbook_tips', lambda _: '')
    # Chart persistence can fetch Yahoo data outside the model tool loop.
    # It is not part of this frozen research contract comparison.
    monkeypatch.setattr(agent_runner, '_persist_quant_chart', AsyncMock())
    from app.v3.shared_desk import SharedDesk
    from app.v3.agents import fundamental_analyst, quant_analyst, board_of_directors
    from app.agents.whiteboard import whiteboard
    from app.tools import whiteboard_tools
    from app.services.research_work import _verified_answers, finish_questions
    import httpx
    import jsonschema

    board_module = SimpleNamespace(AGENT_NAME=board_of_directors.AGENT_NAME,
        ARTIFACT_TYPE=board_of_directors.ARTIFACT_TYPE, TOOL_WHITELIST=board_of_directors.TOOL_WHITELIST,
        SYSTEM_PROMPT=board_of_directors.get_persona_prompt('CONTRADICTORY'))
    facts, items = fixture(case)
    cycle = f'bench-ticket-{case}-{repeat}'
    desk = SharedDesk(ticker='FIXT', cycle_id=cycle)
    desk.cycle_metadata = {'data_report': facts, 'fundamental_context': facts,
        'technical_baseline_context': facts, 'portfolio_context': 'No position held; observation only.',
        'held': False, 'decision_contract_version': 1, 'agent_locale': 'default'}
    desk.desk_note = {'summary': facts, 'key_findings': [facts], 'data_gaps': [], 'confidence': 65,
                      'triage_recommendation': 'FULL_ANALYSIS'}
    desk.regime_classification = {'regime': 'CONTRADICTORY', 'confidence': 65, 'factors': ['Synthetic stable market']}
    desk.bull_argument = {'summary': 'Margin and manageable leverage may support a thesis; historical volume needs checking.', 'confidence': 65}
    desk.bear_rebuttal = {'summary': 'Sparse evidence and unverified historical claims remain risks.', 'confidence': 65}
    desk.bull_defense = {'summary': 'Use verified supplied facts; concede missing history.', 'thesis_survives': True,
                         'final_confidence': 60, 'independent_risks_answered': []}
    desk.debate_judge = {'summary': 'Synthetic fixed review: no recommendation established.', 'winner': 'tie', 'confidence': 60}
    await whiteboard.write_section('FIXT', cycle, 'desk_note', desk.desk_note, 'v3_junior_analyst')
    for item in items:
        item['owner_cycle_id'] = cycle
    if arm == 'tickets':
        desk.cycle_metadata['research_questions'] = deepcopy(items)
        mongo_store.insert_docs('v3_research_queues', deepcopy(items))
    else:
        await whiteboard.write_section('FIXT', cycle, 'research_notes',
            {'questions': [{'item_id': item['id'], 'asked_at': item['created_at'].isoformat(),
                            'question': item['payload']['question']} for item in items]}, 'fixture_reviewer')

    schema_rows = json.loads((ROOT.parent / 'lazy-agent-service/tool_schemas.json').read_text())
    catalog = {t['name']: t for t in schema_rows}
    row = {'case': case, 'repeat': repeat, 'arm': arm, 'cycle_id': cycle, 'pilot': PILOT,
           'fixture_sha256': hashlib.sha256(facts.encode()).hexdigest(), 'roles': [], 'started_at': time.time()}
    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / f'{case}-{repeat}-{arm}.json'
    save = lambda: path.write_text(json.dumps(row, default=str, indent=2))
    common = ('Evaluate the supplied questions, whether carried as unassigned whiteboard notes or claimed research. '
              'For each question you address return research_answers=[{"item_id":"exact supplied id", '
              '"status":"answered|unresolved","answer":"specific supported answer or why unresolved", '
              '"evidence":[{"source":"data_report or tool:<exact name>","quote":"verbatim supporting excerpt"}]}]. '
              'Do not infer historical facts from a later snapshot. Do not repeat a teammate\'s completed evidenced answer.')
    async with httpx.AsyncClient(timeout=590) as client:
        model = 'offline-pilot' if PILOT else (await client.get(ENDPOINT + '/v1/models')).json()['data'][0]['id']
        row['model'] = model
        for module in (fundamental_analyst, quant_analyst, board_module):
            role = {'agent': module.AGENT_NAME, 'calls': [], 'started_at': time.time()}
            row['roles'].append(role)
            tools = [{'type': 'function', 'function': {k: catalog[n][k] for k in ('name','description','parameters')}}
                     for n in module.TOOL_WHITELIST if n in catalog]
            allowed = {t['function']['name']: t['function'] for t in tools}
            async def execute_tool(call):
                name = call['function']['name'].split('__')[-1]
                try:
                    args = json.loads(call['function'].get('arguments') or '{}')
                    if name not in allowed:
                        return {'error': 'NOT_WHITELISTED'}
                    jsonschema.validate(args, allowed[name]['parameters'])
                    if any(str(args[k]).upper() != 'FIXT' for k in ('ticker','symbol') if args.get(k)):
                        return {'error': 'FIXTURE_TICKER_MISMATCH'}
                    if name in ('whiteboard_read','whiteboard_write','whiteboard_annotate','whiteboard_summarize'):
                        return json.loads(await getattr(whiteboard_tools, name)(**args))
                    if name in ('get_market_data','get_technical_indicators','get_sec_filings','get_portfolio_state'):
                        return {'ticker': 'FIXT', 'fixture_evidence': facts}
                    return {'error': 'OFFLINE_EVIDENCE_UNAVAILABLE', 'message': 'No recorded result for this tool. Treat missing evidence as unknown.'}
                except Exception as exc:
                    return {'error': type(exc).__name__, 'message': str(exc)[:250]}
            async def model_call(**kwargs):
                invocation = {'system': kwargs['system_prompt'], 'user': kwargs['user_prompt'], 'turns': [],
                              'usage_complete': True, 'prompt_tokens': 0, 'completion_tokens': 0}
                role['calls'].append(invocation)
                if PILOT:
                    answers = [{'item_id': 'q-margin', 'status': 'answered', 'answer': 'Yes, operating margin is 12.4 percent, above ten percent.',
                        'evidence': [{'source':'data_report','quote':'Operating margin is 12.4 percent.'}]}]
                    obj = {'summary': facts, 'key_findings': [facts], 'data_gaps': [], 'confidence': 65,
                           'research_answers': answers, 'action':'HOLD', 'reasoning':'Observe only; the evidence does not establish an investment thesis.',
                           'entry_mode':'watch_only','trigger_purpose':'none','position_size_pct':0,
                           'risk_metrics':{'annualized_volatility':0.2}, 'stop_loss_suggestion':95,
                           'hrp_weight_suggestion':0, 'position_sizing_note':'No position.'}
                    return {'response':json.dumps(obj),'tokens_used':0,'loops_used':1,'stop_reason':'completed'}
                messages = [{'role':'system','content':kwargs['system_prompt']},{'role':'user','content':kwargs['user_prompt']}]
                transcript=[]; final=''; stop='max_iterations'
                for turn in range(6 if kwargs.get('enable_tools') else 1):
                    payload={'model':model,'messages':messages,'temperature':0,'min_p':0,
                             'max_tokens':min(4096,kwargs.get('max_tokens',4096)),
                             'chat_template_kwargs':{'enable_thinking':False}}
                    if kwargs.get('enable_tools'): payload['tools']=tools
                    started=time.monotonic()
                    result=await complete_stream(client,ENDPOINT+'/v1/chat/completions',payload)
                    usage=result.get('usage') or {}; message=result['message']; calls=message.get('tool_calls') or []
                    invocation['usage_complete'] &= 'prompt_tokens' in usage and 'completion_tokens' in usage
                    invocation['prompt_tokens'] += usage.get('prompt_tokens',0)
                    invocation['completion_tokens'] += usage.get('completion_tokens',0)
                    entry={'elapsed_s':time.monotonic()-started,'usage':usage,'message':message,'tools':[],
                           'finish_reason':result.get('finish_reason')}
                    invocation['turns'].append(entry)
                    final=message.get('content') or ''
                    if not calls:
                        stop='completed'; save(); break
                    messages.append({k:message[k] for k in ('role','content','tool_calls') if k in message})
                    for call in calls:
                        output=await execute_tool(call)
                        entry['tools'].append({'call':call,'result':output})
                        transcript.append({'tool':call['function']['name'].split('__')[-1],'result':json.dumps(output)})
                        messages.append({'role':'tool','tool_call_id':call['id'],'content':json.dumps(output)})
                    save()
                kwargs['cost_sink'].update(tokens=invocation['prompt_tokens']+invocation['completion_tokens'],loops=len(invocation['turns']),tool_calls=len(transcript))
                return {'response':final,'tokens_used':invocation['prompt_tokens']+invocation['completion_tokens'],
                        'prompt_tokens':invocation['prompt_tokens'],'loops_used':len(invocation['turns']),
                        'tool_transcript':transcript,'stop_reason':stop,'model_used':model,'provider':'frozen-replay'}
            monkeypatch.setattr('app.agents.base_agent.run_agent', AsyncMock(side_effect=model_call))
            instructions = common if module != board_module else 'Weigh teammates\' evidenced answers and explicitly retain material unresolved questions. Observation only; do not place orders.'
            outcome = await agent_runner.run_v3_agent(desk=desk,agent_module=module,cycle_id=cycle,bot_id='fixture',
                timeout_seconds=600,include_debate_context=True,custom_instructions=instructions)
            role['outcome']=outcome.value; role['elapsed_s']=time.time()-role['started_at']
            role['artifact']=deepcopy(getattr(desk,module.ARTIFACT_TYPE))
            role['delivery']=deepcopy(desk.cycle_metadata.get('context_delivery',[])[-1:])
            if role['artifact']:
                await whiteboard.write_section('FIXT',cycle,module.ARTIFACT_TYPE,role['artifact'],module.AGENT_NAME)
            save()
            print(json.dumps({'case':case,'repeat':repeat,'arm':arm,'agent':module.AGENT_NAME,'outcome':role['outcome'],'elapsed_s':role['elapsed_s']}),flush=True)
    grading_desk=deepcopy(desk)
    grading_desk.cycle_metadata['research_questions']=deepcopy(items)
    row['verified_answers']=_verified_answers(grading_desk)
    row['ticket_delivery']=finish_questions(desk) if arm=='tickets' else None
    row['queue_states']=list(real_mongo.v3_research_queues.find({}, {'_id':0}))
    if PILOT:
        assert all(role['outcome'] == 'SUCCESS' for role in row['roles'])
        assert 'q-margin' in row['verified_answers']
        assert all(all(item['id'] in role['calls'][0]['user'] for item in items) for role in row['roles'])
        if arm == 'tickets':
            assert row['ticket_delivery'] == {'answered': 1, 'deferred': 2, 'delivery_pending': 0}
    row['elapsed_s']=time.time()-row['started_at']; row['complete']=True
    save()
