#!/usr/bin/env python3
"""Opt-in staging cycle with real stored data and real inference.

One or more tickers (`--ticker`, repeatable). `--transport production` drops the
raw-HTTP model shim and runs the real `base_agent.run_agent` (lazycat-sdk,
retry wrapper, context budget, tool loop), with network access allowlisted to
the Prism host alone. `--tools` leaves each agent's real TOOL_WHITELIST in
place. `--questions N` seeds real research-queue rows through the production
writer and mints a NON-synthetic cycle id, because `claim_for_ticker` returns
nothing for a `bench-` id -- which is why no staging run had ever carried a
compound question.

Runs the production PipelineService and orchestrator against a unique temporary
Mongo database. Copies bounded source rows read-only; never changes production
state. Disables collection refresh, agent tools, orders, notifications and
post-cycle reviewers. Keeps the resulting state, prompts and persisted result
in a local receipt before dropping only the temporary database.
"""
from __future__ import annotations

import argparse
import asyncio
import contextvars
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
#: The one host the production transport is allowed to reach. Everything else
#: still raises, and the URL is recorded, so a collector or broker call that
#: staging was supposed to have disabled shows up as evidence instead of silence.
PRISM_NETLOC = '10.0.0.16:5591'
PREFIX = 'financial_validation_'
QUIESCENT = {'idle', 'done', 'stopped', 'error', 'interrupted'}
#: Answerable from the stored data report alone, because a questions run with
#: tools disabled has nothing else to cite and _verified_answers requires a
#: quoted source. A question no delivered evidence can settle would defer for a
#: reason that says nothing about the delivery path under test.
QUESTION_BANK = (
    'What is the current 14-day RSI, and does it sit in oversold, neutral or overbought territory?',
    'What is the latest reported trailing P/E, and how does it compare with the sector median in the same report?',
    'Is the last close above or below the 200-day moving average, and by what percentage?',
)


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


def copy_sources(source, target, requested):
    """Bounded source copy; no production decisions, queues, or memories copied."""
    from bson import json_util
    from app.quant.returns import _one_vendor
    from app.db.collections import collection_for
    bots = list(source.bots.find({'is_active': True}).sort('created_at', 1).limit(1))
    if not bots:
        raise RuntimeError('No existing paper portfolio to validate')
    bot_id = bots[0]['bot_id']
    positions = list(source.positions.find({'bot_id': bot_id}))
    tickers = sorted({*requested, *(p['ticker'] for p in positions)})
    manifest = {}
    collected: dict[str, list] = {}
    def put(name, rows):
        if rows:
            target[collection_for(name)].insert_many(deepcopy(rows))
        # Accumulated, because `put` is called once per ticker for the
        # per-ticker collections. Digesting only the latest call would have
        # made the manifest name a row count it did not cover.
        collected.setdefault(name, []).extend(rows)
        manifest[name] = {'rows': len(collected[name]),
                          'sha256': hashlib.sha256(
                              json_util.dumps(collected[name], sort_keys=True).encode()).hexdigest()}
    put('bots', bots)
    put('positions', positions)
    # Per-ticker collections are copied once per REQUESTED ticker. A single
    # {'ticker': {'$in': [...]}} query with one limit would hand the whole
    # budget to whichever symbol sorted first and leave the rest starved.
    for ticker in requested:
        for name, query, sort, limit in (
            ('technicals', {'ticker': ticker}, 'date', 30),
            ('fundamentals', {'ticker': ticker}, 'snapshot_date', 30),
            ('balance_sheet', {'ticker': ticker}, 'period_end', 8),
            ('financial_history', {'ticker': ticker}, 'period_end', 40),
            ('news_articles', {'ticker': ticker}, 'published_at', 20),
        ):
            put(name, list(source[collection_for(name)].find(query).sort(sort, -1).limit(limit)))
    for name, query, sort, limit in (
        ('company_registry', {'symbol': {'$in': tickers}}, 'symbol', 100),
        ('runtime_parameters', {'status': 'active'}, 'created_at', 200),
        ('macro_indicators', {'country': 'US'}, 'date', 500),
    ):
        put(name, list(source[collection_for(name)].find(query).sort(sort, -1).limit(limit)))
    prices = []
    for symbol in tickers:
        prices.extend(source[collection_for('price_history')].find(_one_vendor(symbol, {'ticker': symbol})).sort('date', -1).limit(550 if symbol in requested else 20))
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
    # `bench-` is in SYNTHETIC_CYCLE_PREFIXES, and ResearchQueueService
    # .claim_for_ticker returns [] for a synthetic id -- so a bench- cycle can
    # never carry a research question, whatever is in the queue. A questions run
    # mints a non-synthetic id instead; isolation comes from the throwaway
    # database and the driver guard, not from the name.
    cycle_id = (('cycle-v3-staging-' if args.questions else 'bench-financial-cycle-')
                + uuid.uuid4().hex[:12])
    receipt['cycle_id'] = cycle_id
    db = client[db_name]
    original_request = httpx.AsyncClient.request
    original_send = httpx.AsyncClient.send
    #: True only inside the shim's own call to Prism. The guards below sit on
    #: AsyncClient.send, which the shim's own request necessarily passes
    #: through, so without this the shim would refuse itself.
    in_model_transport = contextvars.ContextVar('financial_staging_transport', default=False)

    def save():
        args.out.write_text(json.dumps(receipt, indent=2, default=str, allow_nan=False))

    async def model(**kwargs):
        from app.services.prism_agent_registry import resolve_agent_id
        agent_name = kwargs.get('agent_name') or 'v3_board_of_directors'
        agent_id = resolve_agent_id(agent_name)
        tools, iterations = [], 1
        if args.tools and kwargs.get('enable_tools'):
            from app.agents.tool_whitelists import get_agent_tools, get_agent_budget_turns
            tools = get_agent_tools(agent_name) or []
            iterations = get_agent_budget_turns(agent_name, True)
        body = {'project': 'vllm-trading-bot', 'username': 'lazycat', 'provider': 'vllm', 'model': 'nemotron35',
                'agent': agent_id, 'conversationId': str(uuid.uuid4()), 'createSession': True,
                'systemPrompt': kwargs.get('system_prompt', ''),
                'messages': [{'role': 'user', 'content': kwargs.get('user_prompt', '')}],
                'maxTokens': kwargs.get('max_tokens', 8192), 'enabledTools': tools, 'maxIterations': iterations,
                'temperature': 0, 'functionCallingEnabled': bool(tools), 'agenticLoopEnabled': bool(tools),
                'thinkingEnabled': False, 'workspaceEnabled': False}
        call = {'agent': kwargs.get('agent_name'), 'started_at': datetime.now(timezone.utc).isoformat(), 'body': body}
        receipt['calls'].append(call)
        save()
        started = time.monotonic()
        token = in_model_transport.set(True)
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
            in_model_transport.reset(token)
            call['elapsed_s'] = time.monotonic() - started
            save()

    def _permitted(url):
        from urllib.parse import urlsplit
        return urlsplit(str(url)).netloc == PRISM_NETLOC

    def _refuse(url):
        receipt['blocked_http'].append(str(url)[:200])
        save()
        raise RuntimeError('External HTTP disabled in staging: ' + str(url)[:200])

    def _allowed(url):
        # transport=production runs the real lazycat-sdk client, which must
        # reach Prism and nothing else.
        return in_model_transport.get() or (args.transport == 'production' and _permitted(url))

    async def unavailable_http(self, method, url, *rest, **kwargs):
        if _allowed(url):
            return await original_request(self, method, url, *rest, **kwargs)
        _refuse(url)

    async def guarded_send(self, request, *rest, **kwargs):
        # The SDK streams with client.send(req, stream=True) and client.stream(),
        # NEITHER of which passes through AsyncClient.request -- so guarding
        # `request` alone would have left the tool loop's own transport
        # unguarded, which is exactly the path --tools turns on.
        if _allowed(request.url):
            return await original_send(self, request, *rest, **kwargs)
        _refuse(request.url)

    def guarded_sync(original):
        # Each library keeps its OWN unpatched callable: calling httpx's
        # request for a requests.Session would bind the wrong implementation.
        def guard(self, method, url, *rest, **kwargs):
            if args.transport == 'production' and _permitted(url):
                return original(self, method, url, *rest, **kwargs)
            _refuse(url)
        return guard

    async def no_order(*args, **kwargs):
        receipt['order_attempts'] += 1
        raise RuntimeError('Order execution disabled by staging')

    async def noop_async(*args, **kwargs):
        return None

    try:
        async with AsyncExitStack() as stack:
            stack.enter_context(staging_writes_only(db_name, receipt['production_write_attempts']))
            receipt['source_copy'] = copy_sources(source, db, args.tickers)
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
            receipt['source_capture'] = {t: capture(t) for t in args.tickers}
            for ticker, captured in receipt['source_capture'].items():
                if captured['checks']['errors']:
                    raise RuntimeError('Source capture failed for ' + ticker)
            async def stored_report(ticker, **kwargs):
                if ticker not in receipt['source_capture']:
                    raise RuntimeError('Unexpected ticker in staging: ' + str(ticker))
                return '\n\n'.join(receipt['source_capture'][ticker]['sections'].values())
            stack.enter_context(patch.object(data_report, 'build_ticker_data_report', stored_report))
            if args.transport == 'shim':
                stack.enter_context(patch('app.agents.base_agent.run_agent', model))
            if not args.tools:
                for file in (ROOT / 'app/v3/agents').glob('*.py'):
                    if file.stem.startswith('_'):
                        continue
                    module = importlib.import_module('app.v3.agents.' + file.stem)
                    if hasattr(module, 'TOOL_WHITELIST'):
                        stack.enter_context(patch.object(module, 'TOOL_WHITELIST', []))
            else:
                # tool_optimizer prunes live agents' schemas by success rate read
                # from tool_usage_stats. Staging rows land in the throwaway
                # database, but they are tagged anyway so that a row which ever
                # escapes the namespace is still excluded from the pruner.
                from app.services.logging import tool_logging
                _log_call = tool_logging.log_tool_call
                stack.enter_context(patch.object(
                    tool_logging, 'log_tool_call',
                    lambda *a, **k: _log_call(*a, **{**k, 'service_source': 'contract-test'})))
            if args.orders == 'blocked':
                stack.enter_context(patch.object(paper_trader, 'buy', no_order))
                stack.enter_context(patch.object(paper_trader, 'sell', no_order))
            else:
                # The real paper trader, writing to the throwaway database that
                # holds the copied book. The driver guard still refuses any
                # write outside it, so an order can be placed and inspected
                # without a production row existing anywhere. This is the only
                # way to reach the BUY sizing and capacity gates at all:
                # trade=false tags the decision REASON_TRADE_DISABLED and skips
                # the whole execution block.
                _real_buy, _real_sell = paper_trader.buy, paper_trader.sell
                async def counted_buy(*call_args, **call_kwargs):
                    receipt['order_attempts'] += 1
                    outcome = await _real_buy(*call_args, **call_kwargs)
                    receipt['orders'].append({'side': 'BUY', 'kwargs': {k: v for k, v in call_kwargs.items()
                                                                       if k != 'bot_id'}, 'result': outcome})
                    save()
                    return outcome
                async def counted_sell(*call_args, **call_kwargs):
                    receipt['order_attempts'] += 1
                    outcome = await _real_sell(*call_args, **call_kwargs)
                    receipt['orders'].append({'side': 'SELL', 'kwargs': {k: v for k, v in call_kwargs.items()
                                                                        if k != 'bot_id'}, 'result': outcome})
                    save()
                    return outcome
                stack.enter_context(patch.object(paper_trader, 'buy', counted_buy))
                stack.enter_context(patch.object(paper_trader, 'sell', counted_sell))
            stack.enter_context(patch.object(httpx.AsyncClient, 'request', unavailable_http))
            stack.enter_context(patch.object(httpx.AsyncClient, 'send', guarded_send))
            stack.enter_context(patch.object(httpx.Client, 'request', guarded_sync(httpx.Client.request)))
            stack.enter_context(patch.object(requests.sessions.Session, 'request',
                                             guarded_sync(requests.sessions.Session.request)))
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
            if args.tools:
                # Claiming "not exercised" while tools ARE exercised would put a
                # false statement in the receipt the run exists to produce.
                tools_not_exercised = llm_preflight.tool_calls_are_parsed
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
            if args.questions:
                # Seeded through the production writer, then claimed by the
                # production claim path -- not handed to the orchestrator.
                from app.services.research_queue_service import ResearchQueueService
                from app.schemas.dossier_schemas import QueueType
                seeded = []
                for index in range(args.questions):
                    question = QUESTION_BANK[index % len(QUESTION_BANK)]
                    seeded.append(ResearchQueueService.enqueue_item(
                        ticker=args.tickers[0], queue_type=QueueType.DEEP_DIVE_QUEUE,
                        priority=90 - index, reason=question, source_agent='financial_staging',
                        payload={'question': question, 'question_id': f'staging-q{index + 1}'}))
                receipt['seeded_questions'] = seeded
                save()
            receipt['start_result'] = await PipelineService.start_cycle(list(args.tickers), cycle_id=cycle_id,
                max_tickers=len(args.tickers), collect=False, analyze=True,
                trade=args.orders == 'staging', analysis_mode='full',
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
                contract = result.get('decision_contract') or {}
                checks.append({'ticker': row.get('ticker'), 'financial_evidence_version': result.get('financial_evidence_version'),
                    'reasoning_version': (decision or {}).get('financial_reasoning_version'),
                    'audit': audit_decision(decision, record) if record and decision else {'status': 'missing'},
                    'execution_errors': execution_errors(result), 'policy_action': result.get('policy_action'),
                    # The whole point of a contract run: WHICH producer spoke,
                    # whether it altered the Board, and exactly what it altered.
                    'action': result.get('action'), 'confidence': result.get('confidence'),
                    'decision_producer': result.get('decision_producer'),
                    'decision_relation': contract.get('decision_relation'),
                    'changed_fields': contract.get('changed_fields'),
                    'contract_status': contract.get('status'), 'contract_errors': contract.get('errors'),
                    'trade_attempted': result.get('trade_attempted'), 'trade_executed': result.get('trade_executed')})
            receipt['financial_checks'] = checks
            research = []
            for desk_row in receipt['persisted_desks']:
                data = desk_row.get('desk_data') or {}
                if isinstance(data, str):
                    data = json.loads(data)
                metadata = data.get('cycle_metadata') or {}
                asked = metadata.get('research_questions') or []
                research.append({'ticker': desk_row.get('ticker'), 'questions_required': len(asked),
                                 'question_ids': [item.get('id') for item in asked],
                                 'completion': metadata.get('research_completion'),
                                 'answers_by_artifact': {name: [a.get('item_id') for a in (data.get(name) or {}).get('research_answers') or []]
                                                         for name in ('fundamental_report', 'quant_report', 'valuation_report', 'desk_note')
                                                         if isinstance(data.get(name), dict)}})
            receipt['research'] = research
            receipt['passed'] = (receipt['final_state'].get('status') == 'done'
                and len(checks) == len(args.tickers)
                and all(check['audit']['status'] == 'consistent' and check['reasoning_version'] == 2
                        and check['financial_evidence_version'] == 1 and not check['execution_errors']
                        for check in checks)
                and (receipt['orders_in_staging'] == 0 if args.orders == 'blocked' else True)
                and not receipt['production_write_attempts'])
    finally:
        # Only the exact random namespace created by this invocation may be dropped.
        if valid_staging_database(db_name):
            client.drop_database(db_name)
            receipt['staging_database_removed'] = db_name not in client.list_database_names()
        client.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--ticker', action='append', dest='tickers', metavar='TICKER',
                        help='Repeatable; defaults to AAPL.')
    parser.add_argument('--questions', type=int, default=0, metavar='N',
                        help='Seed N research-queue rows for the first ticker through the '
                             'production writer and let the production claim path deliver them.')
    parser.add_argument('--transport', choices=('shim', 'production'), default='shim',
                        help='shim posts to Prism directly; production runs the real run_agent.')
    parser.add_argument('--tools', action='store_true',
                        help='Leave each agent real TOOL_WHITELIST in place.')
    parser.add_argument('--orders', choices=('blocked', 'staging'), default='blocked',
                        help='blocked runs with trade=false and refuses every order. staging '
                             'runs the cycle with trade=true and lets the real paper trader '
                             'write into the throwaway database, which is the only way to '
                             'reach the BUY sizing and capacity gates.')
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--timeout', type=int, default=1200)
    args = parser.parse_args()
    args.tickers = [t.strip().upper() for raw in (args.tickers or ['AAPL']) for t in raw.split(',') if t.strip()]
    if not args.tickers or len(set(args.tickers)) != len(args.tickers):
        parser.error('At least one ticker, no duplicates')
    for ticker in args.tickers:
        if not re.fullmatch(r'[A-Z][A-Z0-9.\-]{0,9}', ticker):
            parser.error('Invalid ticker: ' + ticker)
    if args.questions and not 1 <= args.questions <= 3:
        parser.error('claim_for_ticker claims at most 3 questions per ticker')
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open('x') as output:
        output.write('{}')
    logging.basicConfig(level=logging.WARNING)
    receipt = {'started_at': datetime.now(timezone.utc).isoformat(), 'source_hashes': source_hashes(),
        'calls': [], 'states': [], 'production_write_attempts': [], 'order_attempts': 0, 'passed': False,
        'blocked_http': [], 'orders': [],
        'mode': {'tickers': args.tickers, 'questions': args.questions,
                 'transport': args.transport, 'tools': args.tools, 'orders': args.orders},
        'limitations': ['Stored real data; collection refresh disabled.',
                       'All agents use real inference; tools are '
                       + ('enabled with each agent real whitelist.' if args.tools else 'disabled.'),
                       'Paper portfolio copied into isolated database; no real broker validation.',
                       ('Transport is the real run_agent (lazycat-sdk, retries, context budget).'
                        if args.transport == 'production' else
                        'Transport is the raw-HTTP shim, not the real run_agent.'),
                       ('%d ticker(s) run concurrently.' % len(args.tickers)),
                       'Acceptance measures the contract and the pipeline, never investment merit.',
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
