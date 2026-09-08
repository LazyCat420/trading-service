"""Durable, leased consolidation independent of ticker selection."""
from __future__ import annotations

import asyncio
import hashlib
import uuid
from datetime import datetime, timedelta, timezone

from app.db import mongo_store
from app.services.learning import health
from app.services.cycle_scope import exclude_synthetic, is_synthetic_cycle

THRESHOLD = 5
LEASE_SECONDS = 900


def now():
    return datetime.now(timezone.utc)


def source_query(ticker: str | None = None) -> dict:
    query = {**exclude_synthetic(), 'promoted_to_memory': False, 'created_at': {'$gte': now() - timedelta(days=30)}}
    if ticker is not None:
        query['ticker'] = ticker
    return query


def enqueue(ticker: str) -> None:
    mongo_store.upsert_doc('memory_consolidation_jobs', {'id': ticker}, {
        'id': ticker, 'ticker': ticker, 'state': 'pending', 'retry_at': now(),
        'created_at': now(), 'attempts': 0,
    }, insert_only=True)
    # New evidence can re-open an empty/review-required job, but not a lease.
    latest = mongo_store.find_docs('episodic_observations', source_query(ticker), sort=[('created_at', -1)], limit=1)
    if latest:
        fingerprint = latest[0]['id']
        mongo_store.update_docs('memory_consolidation_jobs', {
            'id': ticker, 'state': {'$in': ['done', 'review_required']},
            'last_observation_id': {'$ne': fingerprint},
        }, {'$set': {'state': 'pending', 'retry_at': now(), 'attempts': 0}})


def discover() -> dict:
    rows = mongo_store.aggregate('episodic_observations', [
        {'$match': {**source_query(), 'ticker': {'$ne': None}}},
        {'$group': {'_id': '$ticker', 'count': {'$sum': 1}, 'oldest': {'$min': '$created_at'}}},
        {'$match': {'count': {'$gte': THRESHOLD}}}, {'$sort': {'oldest': 1}},
    ])
    for row in rows:
        enqueue(row['_id'])
    return {'eligible_tickers': len(rows), 'unpromoted': sum(r['count'] for r in rows)}


async def process_one() -> dict:
    from app.services.memory.consolidator import run_ticker_consolidation
    timestamp, lease = now(), uuid.uuid4().hex
    job = mongo_store.find_one_and_update('memory_consolidation_jobs', {
        '$or': [
            {'state': {'$in': ['pending', 'retry']}, 'retry_at': {'$lte': timestamp}},
            {'state': 'running', 'lease_until': {'$lt': timestamp}},
        ],
    }, {'$set': {'state': 'running', 'lease': lease, 'lease_until': timestamp + timedelta(seconds=LEASE_SECONDS)},
        '$inc': {'attempts': 1}}, sort=[('retry_at', 1)])
    if not job:
        return {'state': 'idle'}
    outcome, detail = 'failed', ''
    obs = []
    try:
        obs = mongo_store.find_docs('episodic_observations', source_query(job['ticker']), sort=[('created_at', 1)], limit=40)
        if len(obs) < THRESHOLD:
            outcome = 'empty'
        else:
            outcome = await asyncio.wait_for(run_ticker_consolidation(job['ticker'], observations=obs), timeout=600)
    except asyncio.CancelledError:
        # Release without pretending the attempt completed. Idempotent content
        # IDs and source receipts let the next lease resume after a restart.
        mongo_store.update_docs('memory_consolidation_jobs', {'id': job['id'], 'lease': lease}, {
            '$set': {'state': 'retry', 'retry_at': now()}, '$unset': {'lease': '', 'lease_until': ''}})
        raise
    except Exception as exc:
        detail = f'{type(exc).__name__}: {exc}'[:500]
    attempts = int(job.get('attempts') or 1)
    state = 'done' if outcome in {'ok', 'empty'} else 'review_required' if attempts >= 3 else 'retry'
    if outcome == 'ok' and mongo_store.count_docs('episodic_observations', source_query(job['ticker'])) >= THRESHOLD:
        state, attempts = 'retry', 0
    latest = mongo_store.find_docs('episodic_observations', source_query(job['ticker']), sort=[('created_at', -1)], limit=1)
    last_id = latest[0]['id'] if latest else None
    mongo_store.update_docs('memory_consolidation_jobs', {'id': job['id'], 'lease': lease}, {
        '$set': {'state': state, 'last_outcome': outcome, 'last_error': detail,
                 'last_observation_id': last_id, 'finished_at': now(), 'attempts': attempts,
                 'retry_at': now() + timedelta(minutes=min(360, 10 * 2 ** max(0, min(attempts - 1, 5))))},
        '$unset': {'lease': '', 'lease_until': ''},
    })
    health.record('consolidation', state, ticker=job['ticker'], outcome=outcome, error=detail)
    return {'ticker': job['ticker'], 'state': state, 'outcome': outcome}


def validate_result(parsed: dict, ticker: str, observations: list[dict], canonicals: list[dict]) -> tuple[list, list, list]:
    """Accept extractive, dated historical evidence; never model-authored policy.

    Only observations actually quoted are consumed. All source rows are archived
    before promotion. Caller must transactionally persist memories and lineage.
    """
    if not isinstance(parsed, dict):
        raise ValueError('Consolidation response must be an object')
    by_id = {o['id']: o for o in observations if not is_synthetic_cycle(o.get('cycle_id'))}
    existing = {m['id'] for m in canonicals if m.get('ticker') == ticker}
    requested_deprecations = parsed.get('deprecated_memory_ids') or []
    if not isinstance(requested_deprecations, list) or any(i not in existing for i in requested_deprecations):
        raise ValueError('Consolidation tried to retire an unknown/out-of-scope memory')
    memories, used = [], set()
    raw = parsed.get('new_or_updated_memories') or []
    if not isinstance(raw, list):
        raise ValueError('Memory candidates must be a list')
    for candidate in raw[:10]:
        if not isinstance(candidate, dict):
            raise ValueError('Memory candidate must be an object')
        evidence = candidate.get('source_evidence')
        if not isinstance(evidence, list) or not evidence:
            continue
        verified = []
        for ev in evidence[:5]:
            if not isinstance(ev, dict):
                raise ValueError('Malformed source evidence')
            oid, quote = ev.get('observation_id'), ev.get('quote')
            source = by_id.get(oid)
            if not source or not isinstance(quote, str) or not 32 <= len(quote.strip()) <= 1500:
                raise ValueError('Missing observation or supporting quote')
            norm = lambda value: ' '.join(str(value or '').split()).casefold()
            if norm(quote) not in norm(source.get('observation_text')):
                raise ValueError('Quote is not present in the supplied observation')
            from app.v3.arithmetic_audit import has_arithmetic_errors
            if has_arithmetic_errors(quote):
                raise ValueError('Supporting quote contains inconsistent arithmetic')
            verified.append({'observation_id': oid, 'quote': quote.strip(),
                             'source_type': source.get('source_type'), 'as_of': source.get('created_at'),
                             'cycle_id': source.get('cycle_id')})
        from app.utils.tz import ensure_aware
        dates = [ensure_aware(e['as_of']) for e in verified]
        if any(d is None or d > now() for d in dates):
            raise ValueError('Source evidence needs a valid, non-future date')
        as_of = min(dates)  # the oldest supporting fact bounds this combined record
        # Snapshot observations are explicitly historical. Their evidence date
        # stays fixed; fresh processing does not turn old data into current data.
        summary = '\n'.join(f"Recorded {str(e['as_of'])[:10]}: {e['quote']}" for e in verified)
        digest = hashlib.sha256(f'{ticker}:{summary}'.encode()).hexdigest()
        memories.append({'id': digest, 'ticker': ticker, 'type': 'historical_observation',
            'summary': summary, 'proposed_summary': candidate.get('summary', ''),
            'tags': [], 'status': 'active', 'contract_version': 2,
            'validation_state': 'source_verified', 'source_evidence': verified,
            'valid_from': as_of, 'valid_until': as_of + timedelta(days=30),
            'last_validated_at': now(), 'created_at': now(), 'updated_at': now(),
            'confidence_score': 1.0, 'evidence_count': len(verified),
            'confidence_basis': 'quote_match_only_not_market_truth'})
        used.update(e['observation_id'] for e in verified)
    if not memories:
        return [], [], []  # deprecation-only output cannot consume evidence
    return memories, requested_deprecations, sorted(used)
