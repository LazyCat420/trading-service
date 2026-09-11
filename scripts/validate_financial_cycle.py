#!/usr/bin/env python3
"""Opt-in single-ticker staging cycle with real stored data and real inference.

Runs the production PipelineService and orchestrator against a unique temporary
Mongo database. Copies bounded source rows read-only; never changes production
state. Disables collection refresh, agent tools, orders, notifications and
post-cycle reviewers. Keeps the resulting state, prompts and persisted result
in a local receipt before dropping only the temporary database.
"""
from __future__ import annotations

import argparse
import asyncio
from contextlib import AsyncExitStack, ExitStack, contextmanager
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import importlib
import json
import logging
import os
from pathlib import Path
import re
import sys
import time
from unittest.mock import patch
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.test_live_financial_record import capture, source_hashes

ENDPOINT = 'http://10.0.0.16:5591/prism-proxy/agent?stream=false'
PREFIX = 'financial_validation_'
QUIESCENT = {'idle', 'done', 'stopped', 'error', 'interrupted'}


def valid_staging_database(name):
    return bool(re.fullmatch(PREFIX + r'[a-f0-9]{32}', name))


@contextmanager
def staging_writes_only(name, violations):
    """Enforce the namespace at the driver, including direct collection writes."""
    if not valid_staging_database(name):
        raise ValueError('Invalid staging database name')
    from pymongo.collection import Collection
    with ExitStack() as stack:
        for method in ('insert_one', 'insert_many', 'update_one', 'update_many',
                       'replace_one', 'delete_one', 'delete_many', 'bulk_write',
                       'find_one_and_update', 'find_one_and_replace', 'find_one_and_delete',
                       'create_index', 'create_indexes', 'drop', 'drop_index', 'drop_indexes'):
            original = getattr(Collection, method)
            def guarded(collection, *args, _original=original, _method=method, **kwargs):
                if collection.database.name != name:
                    violations.append({'method': _method, 'collection': collection.name})
                    raise RuntimeError('Staging refuses writes outside its temporary database')
                return _original(collection, *args, **kwargs)
            stack.enter_context(patch.object(Collection, method, guarded))
        yield


def copy_sources(source, target, ticker):
    """Bounded source copy; no production decisions, queues, or memories copied."""
    from bson import json_util
    from app.quant.returns import _one_vendor
    from app.db.collections import collection_for
    bots = list(source.bots.find({'is_active': True}).sort('created_at', 1).limit(1))
    if not bots:
        raise RuntimeError('No existing paper portfolio to validate')
    bot_id = bots[0]['bot_id']
    positions = list(source.positions.find({'bot_id': bot_id}))
    tickers = sorted({ticker, *(p['ticker'] for p in positions)})
    manifest = {}
    def put(name, rows):
        if rows:
            target[collection_for(name)].insert_many(deepcopy(rows))
        manifest[name] = {'rows': len(rows),
                          'sha256': hashlib.sha256(json_util.dumps(rows, sort_keys=True).encode()).hexdigest()}
    put('bots', bots)
    put('positions', positions)
    for name, query, sort, limit in (
        ('technicals', {'ticker': ticker}, 'date', 30),
        ('fundamentals', {'ticker': ticker}, 'snapshot_date', 30),
        ('balance_sheet', {'ticker': ticker}, 'period_end', 8),
        ('financial_history', {'ticker': ticker}, 'period_end', 40),
        ('company_registry', {'symbol': {'$in': tickers}}, 'symbol', 100),
        ('runtime_parameters', {'status': 'active'}, 'created_at', 200),
        ('macro_indicators', {'country': 'US'}, 'date', 500),
        ('news_articles', {'ticker': ticker}, 'published_at', 20),
    ):
        put(name, list(source[collection_for(name)].find(query).sort(sort, -1).limit(limit)))
    prices = []
    for symbol in tickers:
        prices.extend(source[collection_for('price_history')].find(_one_vendor(symbol, {'ticker': symbol})).sort('date', -1).limit(550 if symbol == ticker else 20))
    put('price_history', prices)
    return manifest


async def run(args, receipt):
    import httpx
    import pymongo
    from app.config import settings
    from app.db import mongo, mongo_store
    logging.getLogger('app.db.mongo').setLevel(logging.CRITICAL)
    client = pymongo.MongoClient(settings.PRISM_MONGO_URI, serverSelectionTimeoutMS=5000)
    source = client[settings.TRADING_MONGO_DB]
    state = source.pipeline_state.find_one({'singleton_id': 'current'})
    if not state or state.get('status') not in QUIESCENT:
        raise RuntimeError('Production pipeline not confirmed quiescent; no staging run started')
    receipt['production_preflight'] = {'status': state['status'], 'cycle_id': state.get('cycle_id')}
    db_name = PREFIX + uuid.uuid4().hex
    receipt['staging_database'] = db_name
    cycle_id = 'bench-financial-cycle-' + uuid.uuid4().hex[:12]
    receipt['cycle_id'] = cycle_id
    db = client[db_name]
    original_request = httpx.AsyncClient.request

    def save():
        args.out.write_text(json.dumps(receipt, indent=2, default=str, allow_nan=False))

    async def model(**kwargs):
        from app.services.prism_agent_registry import resolve_agent_id
        agent_id = resolve_agent_id(kwargs.get('agent_name') or 'v3_board_of_directors')
        body = {'project': 'vllm-trading-bot', 'username': 'lazycat', 'provider': 'vllm', 'model': 'nemotron35',
                'agent': agent_id, 'conversationId': str(uuid.uuid4()), 'createSession': True,
                'systemPrompt': kwargs.get('system_prompt', ''),
                'messages': [{'role': 'user', 'content': kwargs.get('user_prompt', '')}],
                'maxTokens': kwargs.get('max_tokens', 8192), 'enabledTools': [], 'maxIterations': 1,
                'temperature': 0, 'functionCallingEnabled': False, 'agenticLoopEnabled': False,
                'thinkingEnabled': False, 'workspaceEnabled': False}
        call = {'agent': kwargs.get('agent_name'), 'started_at': datetime.now(timezone.utc).isoformat(), 'body': body}
        receipt['calls'].append(call)
        save()
        started = time.monotonic()
        try:
            async with httpx.AsyncClient(timeout=180, trust_env=False) as http:
                response = await original_request(http, 'POST', ENDPOINT, json=body,
                    headers={'x-project': 'vllm-trading-bot', 'x-username': 'lazycat'})
            call['http_status'] = response.status_code
            if response.is_error:
                call['http_error'] = response.text[:2000]
            response.raise_for_status()
            data = response.json()
            call.update(response=data.get('finalText') or data.get('text'), usage=data.get('usage'))
            return {'response': call['response'], 'tokens_used': (data.get('usage') or {}).get('outputTokens', 0),
                    'loops_used': 1, 'stop_reason': 'completed'}
        except BaseException as exc:
            call['error'] = type(exc).__name__
            raise
        finally:
            call['elapsed_s'] = time.monotonic() - started
            save()

    async def unavailable_http(*args, **kwargs):
        raise RuntimeError('External HTTP disabled in staging except explicit model transport')

    def unavailable_sync(*args, **kwargs):
        raise RuntimeError('External HTTP disabled in staging')

    async def no_order(*args, **kwargs):
        receipt['order_attempts'] += 1
        raise RuntimeError('Order execution disabled by staging')

    async def noop_async(*args, **kwargs):
        return None

    try:
        async with AsyncExitStack() as stack:
            stack.enter_context(staging_writes_only(db_name, receipt['production_write_attempts']))
            receipt['source_copy'] = copy_sources(source, db, args.ticker)
            # Set both database entry points before importing cycle modules.
            for obj, attr, value in ((settings, 'TRADING_MONGO_DB', db_name),
                                     (settings, 'PRISM_MONGO_DB', db_name),
                                     (mongo_store, 'TRADING_MONGO_DB', db_name),
                                     (mongo, '_mongo_client', client),
                                     (mongo_store, '_BACKENDS', {'*': 'mongo'})):
                stack.enter_context(patch.object(obj, attr, value))
            from app.services.pipeline_service import PipelineService
            from app.services.pipeline_state import PipelineStateDB
            from app.v3 import orchestrator
            from app.trading import paper_trader
            from app.services import llm_preflight, degraded_alert
            from app.v3 import data_report
            import requests

            # Real production source builders run twice: this capture supplies
            # the read-only report; the orchestrator constructs its own snapshots.
            receipt['source_capture'] = capture(args.ticker)
            if receipt['source_capture']['checks']['errors']:
                raise RuntimeError('Source capture failed')
            async def stored_report(ticker, **kwargs):
                if ticker != args.ticker:
                    raise RuntimeError('Unexpected ticker in staging')
                return '\n\n'.join(receipt['source_capture']['sections'].values())
            stack.enter_context(patch.object(data_report, 'build_ticker_data_report', stored_report))
            stack.enter_context(patch('app.agents.base_agent.run_agent', model))
            for file in (ROOT / 'app/v3/agents').glob('*.py'):
                if file.stem.startswith('_'):
                    continue
                module = importlib.import_module('app.v3.agents.' + file.stem)
                if hasattr(module, 'TOOL_WHITELIST'):
                    stack.enter_context(patch.object(module, 'TOOL_WHITELIST', []))
            stack.enter_context(patch.object(paper_trader, 'buy', no_order))
            stack.enter_context(patch.object(paper_trader, 'sell', no_order))
            stack.enter_context(patch.object(httpx.AsyncClient, 'request', unavailable_http))
            stack.enter_context(patch.object(httpx.Client, 'request', unavailable_sync))
            stack.enter_context(patch.object(requests.sessions.Session, 'request', unavailable_sync))
            stack.enter_context(patch('app.services.pipeline_service.send_system_log', lambda *a, **k: None))
            stack.enter_context(patch('app.telemetry.send_system_log', lambda *a, **k: None))
            for name in ('alert_preflight_abort', 'alert_phase_abort', 'maybe_alert_degraded_streak'):
                stack.enter_context(patch.object(degraded_alert, name, lambda *a, **k: False))
            stack.enter_context(patch('app.services.bench_reporter.emit_bench_run', noop_async))
            stack.enter_context(patch('app.cognition.evolution.evaluator.run_post_cycle_evaluation', noop_async))
            stack.enter_context(patch('app.services.memory.consolidator.maybe_consolidate', noop_async))
            async def actual_preflight():
                result = await model(agent_name='v3_board_of_directors', system_prompt='Reply with JSON only.',
                                     user_prompt='Return {"ready":true}. No tools.', max_tokens=64)
                try:
                    ready = json.loads(result['response']) == {'ready': True}
                except (TypeError, ValueError):
                    ready = False
                return ready, 'Real tool-disabled staging inference'
            async def tools_not_exercised():
                return True, 'Not exercised: staging disables all agent tools'
            stack.enter_context(patch.object(llm_preflight, 'llm_can_answer', actual_preflight))
            stack.enter_context(patch.object(llm_preflight, 'tool_calls_are_parsed', tools_not_exercised))
            original_save = PipelineStateDB.save_state
            def save_state(value):
                status = value.get('status')
                if not receipt['states'] or receipt['states'][-1]['status'] != status:
                    receipt['states'].append({'status': status, 'at': datetime.now(timezone.utc).isoformat()})
                    save()
                return original_save(value)
            stack.enter_context(patch.object(PipelineStateDB, 'save_state', save_state))
            receipt['states'].append({'status': PipelineStateDB.get_state(summary_only=True)['status'],
                                      'at': datetime.now(timezone.utc).isoformat()})
            async def drain_owned_tasks():
                pending = [t for t in asyncio.all_tasks() if t is not asyncio.current_task() and not t.done()]
                for task in pending:
                    task.cancel()
                await asyncio.gather(*pending, return_exceptions=True)
                # Timed-out to_thread builders must finish before source and
                # namespace patches are restored. This process owns the executor.
                await asyncio.get_running_loop().shutdown_default_executor()
            stack.push_async_callback(drain_owned_tasks)
            receipt['start_result'] = await PipelineService.start_cycle([args.ticker], cycle_id=cycle_id,
                max_tickers=1, collect=False, analyze=True, trade=False, analysis_mode='full',
                trigger_type='financial_staging', start_fresh=True)
            save()
            if PipelineService._cycle_task is None:
                raise RuntimeError('Cycle did not start')
            await asyncio.wait_for(PipelineService._cycle_task, timeout=args.timeout)
            receipt['final_state'] = PipelineStateDB.get_state(summary_only=True)
            receipt['persisted_analyses'] = list(db.analysis_results.find({'cycle_id': cycle_id}, {'_id': 0}))
            receipt['persisted_desks'] = list(db.shared_desk.find({'cycle_id': cycle_id}, {'_id': 0}))
            receipt['events'] = list(db.pipeline_events.find({'cycle_id': cycle_id}, {'_id': 0}))
            receipt['orders_in_staging'] = db.trades.count_documents({})
            from app.v3.financial_claims import audit_decision, execution_errors
            checks = []
            for row in receipt['persisted_analyses']:
                result = row.get('result_json') or {}
                if isinstance(result, str):
                    result = json.loads(result)
                record, decision = result.get('financial_evidence_record'), result.get('financial_decision')
                checks.append({'ticker': row.get('ticker'), 'financial_evidence_version': result.get('financial_evidence_version'),
                    'reasoning_version': (decision or {}).get('financial_reasoning_version'),
                    'audit': audit_decision(decision, record) if record and decision else {'status': 'missing'},
                    'execution_errors': execution_errors(result), 'policy_action': result.get('policy_action')})
            receipt['financial_checks'] = checks
            receipt['passed'] = (receipt['final_state'].get('status') == 'done' and len(checks) == 1
                and checks[0]['audit']['status'] == 'consistent' and checks[0]['reasoning_version'] == 2
                and checks[0]['financial_evidence_version'] == 1 and not checks[0]['execution_errors']
                and receipt['order_attempts'] == 0 and receipt['orders_in_staging'] == 0
                and not receipt['production_write_attempts'])
    finally:
        # Only the exact random namespace created by this invocation may be dropped.
        if valid_staging_database(db_name):
            client.drop_database(db_name)
            receipt['staging_database_removed'] = db_name not in client.list_database_names()
        client.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--ticker', default='AAPL')
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--timeout', type=int, default=1200)
    args = parser.parse_args()
    args.ticker = args.ticker.upper()
    if not re.fullmatch(r'[A-Z][A-Z0-9.\-]{0,9}', args.ticker):
        parser.error('One ticker required')
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open('x') as output:
        output.write('{}')
    logging.basicConfig(level=logging.WARNING)
    receipt = {'started_at': datetime.now(timezone.utc).isoformat(), 'source_hashes': source_hashes(),
        'calls': [], 'states': [], 'production_write_attempts': [], 'order_attempts': 0, 'passed': False,
        'limitations': ['Stored real data; collection refresh disabled.',
                       'All agents use real inference but tools are disabled.',
                       'Paper portfolio copied into isolated database; no real broker validation.',
                       'One ticker does not measure multi-ticker concurrency or investment quality.',
                       'Production lifecycle ends at done, not an invented completed-to-idle sequence.']}
    receipt['source_hashes']['scripts/validate_financial_cycle.py'] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    try:
        asyncio.run(run(args, receipt))
    except Exception as exc:
        receipt['error'] = type(exc).__name__ + ': ' + str(exc)
        receipt['passed'] = False
    finally:
        receipt['finished_at'] = datetime.now(timezone.utc).isoformat()
        args.out.write_text(json.dumps(receipt, indent=2, default=str, allow_nan=False))
    print(json.dumps({key: receipt.get(key) for key in ('passed', 'error', 'staging_database_removed')}))
    return 0 if receipt['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
