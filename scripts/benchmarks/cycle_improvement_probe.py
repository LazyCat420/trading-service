"""Opt-in frozen arithmetic/trigger handoff comparison; no database or real tools."""
import asyncio
from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path
import time

import httpx

from app.v3.arithmetic_audit import arithmetic_handoff, audit_artifact, board_plan_math
from app.v3.decision_contract import prompt_block
from app.v3.shared_desk import SharedDesk

ROOT = Path(__file__).resolve().parents[2]
CASES = [
    {'id': 'cash_decline', 'role': 'Board',
     'peer': 'Verified balance sheet: cash down from $94.6B to $75.5B (-25% YoY), debt up from $44.9B to $64.1B (+43%).',
     'evidence': 'Cash in two comparable annual snapshots: prior 94.6 billion, current 75.5 billion. Debt prior 44.9 billion, current 64.1 billion. A prior peer labels the cash decline -25% and verified. These supplied figures are the full evidence; no external verification receipt exists.',
     'questions': 'Return cash_change_pct (signed percent, current minus prior over prior), peer_cash_arithmetic_correct (boolean), and independently_verified (boolean).',
     'expected': {'cash_change_pct': -20.1902748414, 'peer_cash_arithmetic_correct': False, 'independently_verified': False}},
    {'id': 'zero_base', 'role': 'Board',
     'peer': 'PPI m/m forecast 0.4 vs 0.0 prior is a genuine 4x acceleration forecast.',
     'evidence': 'Prior reported monthly PPI rate is 0.0 percent. The upcoming monthly PPI forecast is 0.4 percent. No actual value for the upcoming release exists. A peer calls this a fourfold acceleration.',
     'questions': 'Return forecast_to_prior_multiple (number or null), percentage_point_change (number), and release_has_occurred (boolean).',
     'expected': {'forecast_to_prior_multiple': None, 'percentage_point_change': 0.4, 'release_has_occurred': False}},
    {'id': 'changed_stop', 'role': 'Synthesizer',
     'peer': 'Earlier quantitative plan has R:R 3.09:1 using entry 499.70, stop 476.04 and target 572.92.',
     'evidence': 'Reference close 499.70. The current Board specifies BUY enter_now, size 1.5 percent of equity, stop 470.13, target 572.92. An older quant plan used stop 476.04 and R:R 3.09:1. No live execution quote is provided. Preserve the current Board fields unless supported evidence justifies a declared override.',
     'questions': 'Return current_plan_reward_risk (number, reward divided by risk at reference close), current_stop (number), and reference_is_live_execution_quote (boolean).',
     'expected': {'current_plan_reward_risk': 2.4761582685, 'current_stop': 470.13, 'reference_is_live_execution_quote': False}},
    {'id': 'fixed_price_entry', 'role': 'Board',
     'peer': 'A peer proposes waiting until price is at or below 95. It says rsi_14_oversold at 35 implies that price by construction, and proposes sma_50_drop value 95 as equivalent.',
     'evidence': 'The intended entry prerequisite is a fixed stock price <=95. Current price is 96, current SMA-50 is 100, RSI-14 is 34. Available monitor implementations: price_below compares current price <=value; price_above compares price >=value; sma_50_drop compares price <current SMA-50 and ignores value as a fixed price; rsi_14_oversold compares RSI <=value. Conditions wake re-analysis only, never place an immediate order.',
     'questions': 'Return matching_trigger_type (string), entry_price_condition_met_now (boolean), and wake_places_order (boolean).',
     'expected': {'matching_trigger_type': 'price_below', 'entry_price_condition_met_now': False, 'wake_places_order': False}},
]
CALCULATOR = json.loads((ROOT/'tests/benchmarks/fixtures/calculator_schema_v1.json').read_text())


def corrected_context(case):
    desk = SharedDesk(ticker='EVLT', cycle_id='bench-cycle-improvement')
    desk.cycle_metadata = {'decision_contract_version': 1, 'technical_baseline_context': '  - close: 499.70   [price: historical reference]'}
    desk.append_artifact('bear_rebuttal', {'summary': case['peer']})
    if case['id'] == 'changed_stop':
        desk.final_decision = {'action': 'BUY', 'entry_mode': 'enter_now', 'trigger_purpose': 'none', 'position_size_pct': 1.5, 'stop_loss': 470.13, 'take_profit': 572.92}
    blocks = [arithmetic_handoff(desk, include_debate=True), board_plan_math(desk)]
    if case['id'] == 'fixed_price_entry':
        blocks.append(prompt_block(desk, 'final_decision'))
    return '\n\n'.join(x for x in blocks if x)


def execute_calculator(args):
    if not isinstance(args, dict) or set(args)-{'operation', 'a', 'b'}:
        raise ValueError('Invalid calculator arguments')
    if not isinstance(args.get('a'), str) or ('b' in args and not isinstance(args['b'], str)):
        raise ValueError('Calculator operands must be numeric strings')
    a = Decimal(args['a']); b = Decimal(args.get('b', '0'))
    if not a.is_finite() or not b.is_finite() or max(len(str(a)), len(str(b))) > 128:
        raise ValueError('Invalid numeric operand')
    operation = args.get('operation')
    if operation == 'add': result = a+b
    elif operation == 'subtract': result = a-b
    elif operation == 'multiply': result = a*b
    elif operation == 'divide': result = a/b
    elif operation == 'modulo': result = a % b
    elif operation == 'sqrt': result = a.sqrt()
    elif operation == 'power':
        if abs(b) > 100: raise ValueError('Exponent exceeds fixture resource bound')
        result = a ** b
    else: raise ValueError('Unknown operation')
    return {'operation': operation, 'firstOperand': str(a), 'b': str(b), 'result': str(result), 'source': 'synthetic Decimal calculator fixture'}


async def main():
    if os.environ.get('CYCLE_IMPROVEMENT_PROBE') != '1':
        raise SystemExit('Set CYCLE_IMPROVEMENT_PROBE=1 explicitly')
    path = Path(os.environ.get('CYCLE_IMPROVEMENT_OUTPUT', '/tmp/cycle-improvement-model-probe.json'))
    if path.exists():
        raise SystemExit('Output already exists; preserve all attempts')
    model = 'GLM-5.3-Flash-EXL3'
    sources = ['app/v3/arithmetic_audit.py', 'app/v3/decision_contract.py', 'scripts/benchmarks/cycle_improvement_probe.py', 'tests/benchmarks/fixtures/calculator_schema_v1.json']
    report = {'version': 1, 'started_at': datetime.now(timezone.utc).isoformat(), 'model': model,
              'max_tokens': 3072, 'max_turns': 4, 'timeout_seconds_per_turn': 120, 'temperature': 0,
              'cases': CASES, 'source_hashes': {p:hashlib.sha256((ROOT/p).read_bytes()).hexdigest() for p in sources},
              'limitation': 'Four narrow synthetic consistency cases repeated twice. Same evidence/questions/tools/model, current arm adds production correction blocks. Direct provider loop; no production latency, native think-loop, profitability or general decision-quality inference.',
              'attempts': []}
    def save(): path.write_text(json.dumps(report, indent=2))
    save()
    async with httpx.AsyncClient(timeout=120) as client:
        for repeat in range(2):
            for index, case in enumerate(CASES):
                for arm in (['baseline', 'current'] if (repeat+index)%2 == 0 else ['current', 'baseline']):
                    attempt = {'case':case['id'], 'repeat':repeat, 'arm':arm, 'turns':[], 'tool_errors':0, 'usage_complete':True, 'known_tokens':0, 'final':None}
                    report['attempts'].append(attempt); save()
                    system = f"You are the trading {case['role']}. Assess the supplied evidence and peer claims critically. Distinguish a calculation from external verification. Preserve uncertainty. Return a JSON object with the requested answer fields plus a concise rationale. You may use the calculator; no other research is available."
                    user = f"Synthetic case EVLT.\nEVIDENCE:\n{case['evidence']}\n\nPRIOR PEER:\n{case['peer']}\n\nQUESTIONS:\n{case['questions']}"
                    if arm == 'current': user += '\n\n' + corrected_context(case)
                    messages = [{'role':'system','content':system}, {'role':'user','content':user}]
                    attempt['delivered_user'] = user
                    for turn in range(4):
                        payload = {'model':model, 'messages':messages, 'tools':[{'type':'function', 'function':CALCULATOR}], 'temperature':0, 'min_p':0, 'max_tokens':3072, 'chat_template_kwargs':{'enable_thinking':False,'thinking':False}}
                        started=time.monotonic()
                        try:
                            response=await client.post('http://10.0.0.141:8000/v1/chat/completions', json=payload)
                            response.raise_for_status(); result=response.json(); choice=result['choices'][0]; message=choice['message']; usage=result.get('usage') or {}
                            attempt['usage_complete'] &= 'prompt_tokens' in usage and 'completion_tokens' in usage
                            attempt['known_tokens'] += usage.get('prompt_tokens',0)+usage.get('completion_tokens',0)
                            safe={k:message[k] for k in ['role','content','tool_calls'] if k in message}
                            attempt['turns'].append({'elapsed_s':time.monotonic()-started,'usage':usage,'finish_reason':choice.get('finish_reason'),'message':safe,'tool_results':[]})
                            messages.append(safe)
                            if not message.get('tool_calls'):
                                attempt['final']=message.get('content'); break
                            for call in message['tool_calls']:
                                try:
                                    if call['function']['name'] != 'evaluate_expression': raise ValueError('Unavailable tool')
                                    value=execute_calculator(json.loads(call['function']['arguments']))
                                except Exception as exc:
                                    attempt['tool_errors']+=1; value={'error':type(exc).__name__,'message':str(exc)[:120]}
                                attempt['turns'][-1]['tool_results'].append(value)
                                messages.append({'role':'tool','tool_call_id':call['id'],'content':json.dumps(value)})
                            save()
                        except Exception as exc:
                            attempt['error']=type(exc).__name__; attempt['usage_complete']=False
                            if isinstance(exc, httpx.HTTPStatusError):
                                attempt['http_status']=exc.response.status_code
                                attempt['http_error']=exc.response.text[:1500]
                            break
                    try:
                        final=json.loads((attempt['final'] or '').strip().removeprefix('```json').removesuffix('```').strip())
                        grades={}
                        for key, expected in case['expected'].items():
                            got=final.get(key, '__missing__')
                            grades[key]=(type(got) in (int,float) and abs(got-expected)<0.001) if type(expected) in (int,float) else type(got)==type(expected) and got==expected
                        attempt['answer_checks']=grades; attempt['all_answers_correct']=all(grades.values())
                    except (ValueError, TypeError, AttributeError): attempt['answer_checks']={}; attempt['all_answers_correct']=False
                    save(); print(json.dumps({k:attempt.get(k) for k in ['case','repeat','arm','all_answers_correct','tool_errors','error']}),flush=True)
    report['finished_at']=datetime.now(timezone.utc).isoformat(); save()

if __name__ == '__main__': asyncio.run(main())
