"""Explicit, isolated current-role and paired decision replay; no live data tools."""
import asyncio
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import importlib
import json
import os
from pathlib import Path
import sys
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'scripts/benchmarks'))
sys.path.append('/usr/lib/python3/dist-packages')
from memory_stream import complete_stream

pytestmark = [pytest.mark.real_mongo, pytest.mark.asyncio]
CORPUS_PATH = Path(__file__).parent / 'fixtures/decision_quality_v1.json'
CORPUS = json.loads(CORPUS_PATH.read_text())
CASES = {case['id']: case for case in CORPUS['cases']}
DRY = os.environ.get('DECISION_AUDIT_DRY') == '1'
ENABLED = DRY or os.environ.get('DECISION_AUDIT_RUN') == '1'
OUT = Path(os.environ.get('DECISION_AUDIT_OUTPUT', '/tmp/decision-quality-replay'))
ENDPOINT = 'http://10.0.0.16:5591/vllm-shim/gold-spark'
OTHER_ROLES = ('junior_analyst', 'valuation_analyst', 'bull_agent', 'bear_agent',
               'bull_defense', 'debate_judge', 'regime_engine', 'decision_agent', 'delta_analyst')
PLAN = [('handoff', case, repeat, arm, 'workflow') for repeat in range(2)
        for i, case in enumerate(CASES)
        for arm in (('notes', 'tickets') if (i + repeat) % 2 == 0 else ('tickets', 'notes'))]
PLAN += [('roles', case, 0, 'current', role) for case in ('headroom', 'missing_history') for role in OTHER_ROLES]
PLAN += [('ablation', case, 0, arm, 'board_of_directors') for i, case in enumerate(CASES)
         for arm in (('current', 'no_method', 'stale_memory') if i % 2 == 0 else ('stale_memory', 'no_method', 'current'))]


def make_fixture(case_id):
    case = deepcopy(CASES[case_id])
    facts = f"{CORPUS['shared']}\n\nCASE EVIDENCE:\n{case['facts']}"
    assert len(facts) <= 5000, 'Do not silently truncate frozen evidence in the runner'
    items = [{'id': q['id'], 'ticker': 'EVLT', 'created_at': datetime(2026, 9, 3, 20, tzinfo=timezone.utc),
              'source_agent': 'fixture_reviewer', 'payload': {'question': q['question']},
              'status': 'processing', 'owner_cycle_id': '', 'lease_token': q['id'], 'attempts': 1}
             for q in case['questions']]
    return case, facts, items


def fixed_desk(SharedDesk, cycle, case, facts):
    desk = SharedDesk(ticker='EVLT', cycle_id=cycle)
    desk.cycle_metadata = {'data_report': facts, 'fundamental_context': facts,
        'technical_baseline_context': facts, 'quant_math_context': 'Use the dated risk inputs in CASE EVIDENCE; do not invent missing covariance or forecasts.',
        'macro_briefing': '2026-09-03: VIX 18; broad-market SMA-50 above SMA-200; neutral credit spreads. No other macro measurements supplied.',
        'portfolio_context': facts, 'held': case['held'], 'decision_contract_version': 1, 'agent_locale': 'default',
        'previous_desk_context': 'Previous EVLT thesis cautiously positive. Recheck the current filing and position before reaffirming. Existing intent was watch_only; no entry order is pending.'}
    desk.desk_note = {'summary': 'Review the current dated evidence and the three supplied research questions.',
        'key_findings': [case['facts']], 'data_gaps': [], 'confidence': 65, 'triage_recommendation': 'FULL_ANALYSIS'}
    desk.regime_classification = {'regime': 'CONTRADICTORY', 'confidence': 65, 'factors': ['Neutral credit spreads and VIX 18'], 'forward_call': 'UNCERTAIN'}
    desk.fundamental_report = {'summary': 'Use the most recent applicable filing; separate historical statements from current ones.', 'data_gaps': [], 'confidence': 65}
    desk.quant_report = {'summary': 'Verify stop/target geometry, units and position headroom against the supplied measurements.', 'data_gaps': [], 'confidence': 65,
        'risk_metrics': {}, 'position_sizing_note': 'The total position limit is 5 percent. Current holdings matter.'}
    desk.bull_argument = {'summary': 'The shared baseline supports a profitable-growth thesis only if the current case evidence has not superseded it.', 'confidence': 65}
    desk.bear_rebuttal = {'summary': 'Check whether the current case supersedes the growth thesis, or whether risk limits or absent history invalidate the proposed entry.', 'confidence': 65}
    desk.bull_defense = {'summary': 'Concede superseded facts and missing confirmation; rely only on the current case evidence.', 'thesis_survives': True, 'final_confidence': 60,
        'independent_risks_answered': []}
    desk.debate_judge = {'summary': 'Fixed prior review leaves the current questions for evidence-based resolution.', 'winner': 'tie', 'final_confidence': 60}
    desk.final_decision = {'action': 'HOLD', 'confidence': 60, 'reasoning': 'Fixed prior watch decision; current evidence requires a fresh assessment.',
        'entry_mode': 'watch_only', 'trigger_purpose': 'none', 'position_size_pct': 0}
    return desk


@pytest.mark.skipif(not ENABLED, reason='Explicit DECISION_AUDIT_RUN=1 required')
@pytest.mark.timeout(1900)
@pytest.mark.parametrize('cohort,case_id,repeat,arm,role_name', PLAN)
async def test_replay(real_mongo, live_http, monkeypatch, cohort, case_id, repeat, arm, role_name):
    assert real_mongo.name.startswith('trading_bot_pytest_decision_')
    from app.db import mongo_store, mongo
    monkeypatch.setattr(mongo_store, '_BACKENDS', {'*': 'mongo'})
    monkeypatch.setattr(mongo, 'get_mongo_db', lambda: real_mongo)
    from app.autoresearch import skill_loader
    from app.services.learning.policy import BASELINES
    def skill(role):
        text = '' if arm == 'no_method' else BASELINES.get(role, '')
        return f'## Agent Skill Guidance (SkillOpt)\n{text}\n\n' if text else ''
    monkeypatch.setattr(skill_loader, 'load_skill_prefix', skill)
    from app.v3 import agent_runner
    monkeypatch.setattr(agent_runner, '_get_tool_playbook_tips', lambda _: '')
    monkeypatch.setattr(agent_runner, '_persist_quant_chart', AsyncMock())
    from app.v3.shared_desk import SharedDesk
    from app.v3.artifacts import validate_artifact
    from app.v3.decision_contract import entry_errors
    from app.agents.whiteboard import whiteboard
    from app.tools import whiteboard_tools
    from app.services.research_work import _verified_answers, finish_questions
    import httpx
    import jsonschema

    def module(name):
        value = importlib.import_module('app.v3.agents.' + name)
        if name == 'board_of_directors':
            return SimpleNamespace(AGENT_NAME=value.AGENT_NAME, ARTIFACT_TYPE=value.ARTIFACT_TYPE,
                TOOL_WHITELIST=value.TOOL_WHITELIST, SYSTEM_PROMPT=value.get_persona_prompt('CONTRADICTORY'))
        return value
    modules = [module(n) for n in (('fundamental_analyst', 'quant_analyst', 'board_of_directors') if role_name == 'workflow' else (role_name,))]
    case, facts, items = make_fixture(case_id)
    cycle = f'bench-decision-{cohort}-{case_id}-{repeat}-{arm}-{role_name}'
    desk = fixed_desk(SharedDesk, cycle, case, facts)
    seeded_artifacts = {k: deepcopy(getattr(desk, k)) for k in ('desk_note', 'regime_classification', 'fundamental_report', 'quant_report', 'bull_argument', 'bear_rebuttal', 'bull_defense', 'debate_judge', 'final_decision')}
    stage_order = ['regime_classification', 'desk_note', 'fundamental_report', 'quant_report', 'valuation_report', 'bull_argument', 'bear_rebuttal', 'bull_defense', 'debate_judge', 'final_decision', 'trade_decision']
    first_stage = modules[0].ARTIFACT_TYPE
    cutoff = stage_order.index(first_stage) if first_stage in stage_order else 0
    for field in stage_order[cutoff:]:
        setattr(desk, field, None)
    # A role must not inherit its own fixture output and appear successful on
    # that stale artifact if it fails to produce a new one.
    for mod in modules:
        setattr(desk, mod.ARTIFACT_TYPE, None)
    if cohort == 'handoff':
        desk.final_decision = None
    if arm == 'stale_memory':
        desk.cycle_metadata['memory_context'] = ('UNVERIFIED HISTORICAL MEMORY, ticker OTHR, 2026-08-01: '
          'Operating margin was 25 percent and all planned entries used 2.5 percent of equity. '
          'A prior memo called price 100 the midpoint of support 95 and resistance 115, '
          'used revenue growth for PEG, and treated repeated summaries as independent confirmation. '
          'These are old claims about another ticker, not current EVLT evidence or approved policy.')
    if desk.desk_note:
        await whiteboard.write_section('EVLT', cycle, 'desk_note', desk.desk_note, 'v3_junior_analyst')
    for item in items:
        item['owner_cycle_id'] = cycle
    if arm == 'tickets':
        desk.cycle_metadata['research_questions'] = deepcopy(items)
        mongo_store.insert_docs('v3_research_queues', deepcopy(items))
    else:
        await whiteboard.write_section('EVLT', cycle, 'research_notes',
            {'questions': [{'item_id': item['id'], 'asked_at': item['created_at'].isoformat(), 'question': item['payload']['question']} for item in items]}, 'fixture_reviewer')
    catalog = {t['name']: t for t in json.loads((ROOT.parent / 'lazy-agent-service/tool_schemas.json').read_text())}
    key = f'{cohort}-{case_id}-{repeat}-{arm}-{role_name}'
    row = {'id': key, 'cohort': cohort, 'case': case_id, 'repeat': repeat, 'arm': arm,
           'cycle_id': cycle, 'dry_run': DRY, 'roles': [], 'started_at': time.time(),
           'fixture_sha256': hashlib.sha256(facts.encode()).hexdigest(),
           'runner_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}
    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / f'{key}.json'
    assert not path.exists(), f'Refusing to overwrite recorded evidence: {path}'
    def save():
        path.write_text(json.dumps(row, default=str, indent=2))
    common = ('This is an offline evidence assessment. Make the recommendation justified by the supplied facts; '
      'BUY, SELL, HOLD and conditional entry are permitted when their contracts are satisfied. '
      'No execution tools are available. Do not treat a proposed future entry as the current quote. '
      'Address the supplied research questions that are within your role, whether delivered as notes or claimed research. '
      'When answering use research_answers=[{"item_id":"exact supplied id","status":"answered|unresolved",'
      '"answer":"specific answer or why unknown","evidence":[{"source":"data_report or tool:<name>",'
      '"quote":"verbatim supporting excerpt"}]}]. Do not duplicate a teammate\'s completed answer; '
      'weigh its substance and correct any error. Facts outside the frozen evidence remain unavailable.')
    async with httpx.AsyncClient(timeout=590) as client:
        model = 'offline-dry-run' if DRY else (await client.get(ENDPOINT + '/v1/models')).json()['data'][0]['id']
        assert DRY or model == 'GLM-5.3-Flash-EXL3', f'Model changed: {model}'
        row['model'] = model
        for mod in modules:
            if cohort == 'handoff' and mod.ARTIFACT_TYPE == 'final_decision':
                # The controlled downstream comparison uses fixed debate inputs,
                # introduced only after research. Earlier roles cannot see them.
                for field in ('bull_argument', 'bear_rebuttal', 'bull_defense', 'debate_judge'):
                    setattr(desk, field, deepcopy(seeded_artifacts[field]))
            role = {'agent': mod.AGENT_NAME, 'calls': [], 'started_at': time.time()}
            row['roles'].append(role)
            tools = [{'type': 'function', 'function': {k: catalog[n][k] for k in ('name', 'description', 'parameters')}}
                     for n in mod.TOOL_WHITELIST if n in catalog]
            role['missing_schemas'] = [n for n in mod.TOOL_WHITELIST if n not in catalog]
            allowed = {t['function']['name']: t['function'] for t in tools}
            async def execute(call):
                name = call['function']['name'].split('__')[-1]
                try:
                    args = json.loads(call['function'].get('arguments') or '{}')
                    if name not in allowed:
                        return {'error': 'NOT_WHITELISTED'}
                    jsonschema.validate(args, allowed[name]['parameters'])
                    requested = str(args.get('ticker') or args.get('symbol') or 'EVLT').upper()
                    if requested not in ('EVLT', 'PAAA', 'PBBB', 'SPY', 'QQQ', 'VIX', '^VIX'):
                        return {'available': False, 'reason': 'No evidence for requested ticker in frozen corpus', 'ticker': requested}
                    if name.startswith('whiteboard_'):
                        if requested != 'EVLT' or (args.get('cycle_id') and args['cycle_id'] != cycle):
                            return {'error': 'FIXTURE_SCOPE_MISMATCH'}
                        return json.loads(await getattr(whiteboard_tools, name)(**args))
                    if name in ('save_equation', 'run_equation', 'run_backtest', 'search_equations'):
                        return {'available': False, 'reason': 'No saved equation or backtest evidence in this corpus; use the supplied measurements.'}
                    return {'frozen_evidence': True, 'requested_tool': name, 'requested_ticker': requested,
                            'as_of': CORPUS['as_of'], 'source': 'data_report', 'evidence': facts,
                            'coverage': 'Only facts explicitly stated here are available. This is not a live service response.'}
                except Exception as exc:
                    return {'error': type(exc).__name__, 'message': str(exc)[:250]}
            async def model_call(**kwargs):
                inv = {'system': kwargs['system_prompt'], 'user': kwargs['user_prompt'], 'turns': [],
                       'usage_complete': True, 'prompt_tokens': 0, 'completion_tokens': 0}
                role['calls'].append(inv)
                if DRY:
                    return {'response': json.dumps({'summary': 'Dry-run setup fixture; not scored.', 'key_findings': [], 'data_gaps': ['dry run'],
                      'confidence': 60, 'triage_recommendation': 'FULL_ANALYSIS', 'action': 'HOLD', 'reasoning': 'Dry-run fixture only.',
                      'entry_mode': 'watch_only', 'trigger_purpose': 'none', 'position_size_pct': 0, 'regime': 'CONTRADICTORY',
                      'factors': ['fixture'], 'forward_call': 'UNCERTAIN', 'winner': 'tie', 'final_confidence': 60, 'thesis_survives': True,
                      'risk_metrics': {}, 'position_sizing_note': 'Dry run', 'escalate': True, 'verdict': 'ESCALATE', 'material_change': 'fixture'}),
                      'tokens_used': 0, 'loops_used': 1, 'stop_reason': 'completed'}
                messages = [{'role': 'system', 'content': kwargs['system_prompt']}, {'role': 'user', 'content': kwargs['user_prompt']}]
                transcript = []; final = ''; stop = 'max_iterations'
                for turn in range(6 if kwargs.get('enable_tools') else 1):
                    payload = {'model': model, 'messages': messages, 'temperature': 0, 'min_p': 0, 'max_tokens': 8192,
                               'chat_template_kwargs': {'enable_thinking': False}}
                    if kwargs.get('enable_tools'):
                        payload['tools'] = tools
                    started = time.monotonic()
                    try:
                        result = await complete_stream(client, ENDPOINT + '/v1/chat/completions', payload)
                    except BaseException as exc:
                        inv['usage_complete'] = False
                        inv['error'] = type(exc).__name__
                        save()
                        raise
                    usage = result.get('usage') or {}; message = result['message']; calls = message.get('tool_calls') or []
                    inv['usage_complete'] &= 'prompt_tokens' in usage and 'completion_tokens' in usage
                    inv['prompt_tokens'] += usage.get('prompt_tokens', 0)
                    inv['completion_tokens'] += usage.get('completion_tokens', 0)
                    entry = {'elapsed_s': time.monotonic() - started, 'usage': usage, 'message': message,
                             'tools': [], 'finish_reason': result.get('finish_reason')}
                    inv['turns'].append(entry)
                    kwargs['cost_sink'].update(tokens=inv['prompt_tokens']+inv['completion_tokens'], loops=len(inv['turns']), tool_calls=len(transcript))
                    final = message.get('content') or ''
                    if not calls:
                        stop = 'completed'; save(); break
                    messages.append({k: message[k] for k in ('role', 'content', 'tool_calls') if k in message})
                    for call in calls:
                        output = await execute(call)
                        entry['tools'].append({'call': call, 'result': output})
                        transcript.append({'tool': call['function']['name'].split('__')[-1], 'result': json.dumps(output)})
                        messages.append({'role': 'tool', 'tool_call_id': call['id'], 'content': json.dumps(output)})
                    save()
                return {'response': final, 'tokens_used': inv['prompt_tokens']+inv['completion_tokens'],
                        'prompt_tokens': inv['prompt_tokens'], 'loops_used': len(inv['turns']), 'tool_transcript': transcript,
                        'stop_reason': stop, 'model_used': model, 'provider': 'frozen-decision-replay'}
            monkeypatch.setattr('app.agents.base_agent.run_agent', AsyncMock(side_effect=model_call))
            outcome = await agent_runner.run_v3_agent(desk=desk, agent_module=mod, cycle_id=cycle, bot_id='fixture',
                timeout_seconds=600, include_debate_context=True, custom_instructions=common)
            artifact = deepcopy(getattr(desk, mod.ARTIFACT_TYPE))
            role.update(outcome=outcome.value, elapsed_s=time.time()-role['started_at'], artifact=artifact,
                        schema_errors=validate_artifact(mod.ARTIFACT_TYPE, artifact) if artifact else ['No artifact'],
                        entry_errors=entry_errors(artifact) if artifact and mod.ARTIFACT_TYPE in ('final_decision', 'trade_decision') else [],
                        delivery=deepcopy(desk.cycle_metadata.get('context_delivery', [])[-1:]))
            if artifact:
                await whiteboard.write_section('EVLT', cycle, mod.ARTIFACT_TYPE, artifact, mod.AGENT_NAME)
            save()
            print(json.dumps({'id': key, 'role': mod.AGENT_NAME, 'outcome': role['outcome'], 'seconds': role['elapsed_s']}), flush=True)
    grading_desk = deepcopy(desk)
    grading_desk.cycle_metadata['research_questions'] = deepcopy(items)
    row['verified_answers'] = _verified_answers(grading_desk)
    row['ticket_delivery'] = finish_questions(desk) if arm == 'tickets' else None
    row['queue_states'] = list(real_mongo.v3_research_queues.find({}, {'_id': 0}))
    row['complete'] = True; row['elapsed_s'] = time.time()-row['started_at']; save()
