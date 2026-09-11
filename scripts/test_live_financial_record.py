#!/usr/bin/env python3
"""Read-only audit of real stored market snapshots through production builders.

Does not trigger collectors, agents, orders, or production writes. Stored source
ages are retained: this verifies ingestion into SharedDesk, not feed freshness.
"""
from __future__ import annotations

import argparse
from contextlib import ExitStack, contextmanager
from datetime import datetime, timezone
import hashlib
import json
import logging
from pathlib import Path
import re
import sys
import time
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


@contextmanager
def read_only_store():
    from app.db import mongo_store
    from pymongo.collection import Collection
    attempts = []

    def refuse(*args, **kwargs):
        attempts.append('write refused')
        raise RuntimeError('Financial source probe forbids database writes')

    with ExitStack() as stack:
        for name in ('insert_docs', 'upsert_doc', 'bulk_upsert', 'update_docs',
                     'delete_docs', 'find_one_and_update', 'ensure_indexes'):
            stack.enter_context(patch.object(mongo_store, name, refuse))
        for name in ('insert_one', 'insert_many', 'update_one', 'update_many',
                     'replace_one', 'delete_one', 'delete_many', 'bulk_write',
                     'find_one_and_update', 'find_one_and_replace', 'find_one_and_delete',
                     'create_index', 'create_indexes', 'drop', 'drop_index', 'drop_indexes'):
            stack.enter_context(patch.object(Collection, name, refuse))
        yield attempts


def source_hashes():
    paths = ('app/v3/orchestrator.py', 'app/v3/financial_evidence.py', 'app/v3/financial_reasoning.py',
             'app/v3/financial_claims.py', 'app/v3/agent_runner.py',
             'app/quant/technical_baseline.py',
             'scripts/test_live_financial_record.py')
    return {p: hashlib.sha256((ROOT / p).read_bytes()).hexdigest() for p in paths}


def verify_record(record):
    from app.v3.financial_evidence import calculated_facts, number
    from app.v3.financial_reasoning import reasoning_catalog
    facts = calculated_facts(record)
    errors = []
    for key, fact in facts.items():
        if fact['status'] not in ('known', 'unknown'):
            errors.append(f'{key}: invalid status')
        if (fact['value'] is None) != (fact['status'] == 'unknown'):
            errors.append(f'{key}: value/status mismatch')
        if not fact.get('unit') or not fact.get('source'):
            errors.append(f'{key}: missing unit or provenance')
        if fact['status'] == 'known' and fact['unit'] not in ('category', 'boolean') and number(fact['value']) is None:
            errors.append(f'{key}: non-finite numeric value')
    catalog = reasoning_catalog(record)
    for key, step in catalog.items():
        if not step['fact_ids'] or any(i not in facts for i in step['fact_ids']):
            errors.append(f'{key}: invalid catalog reference')
    return {'errors': errors, 'known_facts': sum(f['status'] == 'known' for f in facts.values()),
            'unknown_facts': sum(f['status'] == 'unknown' for f in facts.values()),
            'calculated_facts': {k: f for k, f in facts.items() if k.startswith('calc_')},
            'catalog': catalog}


def capture(ticker):
    from app.quant.technical_baseline import build_technical_baseline_block
    from app.quant.fundamental_block import build_fundamental_block
    from app.quant.valuation_block import build_valuation_block
    from app.v3.financial_evidence import build_record
    from app.v3.shared_desk import SharedDesk
    from app.db import mongo_store
    desk = SharedDesk(ticker=ticker, cycle_id='financial-source-audit')
    desk.cycle_metadata = {'timestamp': datetime.now(timezone.utc).isoformat()}
    sections, timings = {}, {}
    for key, builder in (('technical', build_technical_baseline_block),
                         ('fundamental', build_fundamental_block),
                         ('valuation', build_valuation_block)):
        snapshot = {}
        started = time.monotonic()
        sections[key] = builder(ticker, snapshot_sink=snapshot)
        timings[key] = time.monotonic() - started
        desk.cycle_metadata[f'financial_{key}_snapshot'] = snapshot
    record = build_record(desk)
    checks = verify_record(record)
    # A returned all-unknown record is valid structurally but NOT a successful
    # real-data ingestion test. Require actual price and ratio observations.
    for key in ('technical', 'fundamental'):
        if not desk.cycle_metadata[f'financial_{key}_snapshot']:
            checks['errors'].append(f'{key}: empty source snapshot')
    from app.quant.returns import _one_vendor
    latest = mongo_store.find_docs('price_history', _one_vendor(ticker, {'ticker': ticker}), sort=[('date', -1)], limit=1,
                                  projection={'date': 1, 'close': 1, 'source': 1, '_id': 0})
    return {'ticker': ticker, 'record': record, 'snapshots': desk.cycle_metadata,
            'sections': sections, 'builder_elapsed_s': timings, 'checks': checks,
            'latest_stored_price': latest,
            'limitations': ['Stored vendor data, no collector refresh or quote freshness claim.',
                            'No portfolio captured; headroom and reservations remain unknown.',
                            'No model or broker invocation.']}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--ticker', default='AAPL')
    parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args()
    ticker = args.ticker.strip().upper()
    if not re.fullmatch(r'[A-Z][A-Z0-9.\-]{0,9}', ticker):
        parser.error('One ticker symbol is required')
    if args.out.exists():
        parser.error('Output exists; preserve every attempt with a new path')
    logging.getLogger('app.db.mongo').setLevel(logging.CRITICAL)
    report = {'started_at': datetime.now(timezone.utc).isoformat(), 'source_hashes': source_hashes()}
    try:
        with read_only_store() as attempts:
            report.update(capture(ticker))
        report['write_attempts'] = attempts
        report['passed'] = not report['checks']['errors'] and not attempts
    except Exception as exc:
        report.update(passed=False, error=type(exc).__name__)
    finally:
        report['finished_at'] = datetime.now(timezone.utc).isoformat()
        args.out.parent.mkdir(parents=True, exist_ok=True)
        with args.out.open('x') as output:
            json.dump(report, output, indent=2, default=str, allow_nan=False)
        from app.db import mongo
        if mongo._mongo_client is not None:
            mongo._mongo_client.close()
    print(json.dumps({'passed': report['passed'], 'errors': report.get('checks', {}).get('errors'),
                      'known_facts': report.get('checks', {}).get('known_facts'), 'out': str(args.out)}))
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
