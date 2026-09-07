"""Idempotent learning records with durable indexing and explicit eligibility.

Incidents are recommendations for operations, never live analyst instructions.
Raw model proposals remain candidates. Reviewed methods are served by policy.py.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import re
import uuid

from app.db import mongo_store
from app.services.learning.policy import CONTRACT_VERSION, content_hash
from app.utils.poison_guard import is_poisoned_response

KINDS = {'incident', 'procedure', 'fact'}


def now():
    return datetime.now(timezone.utc)


def write(text: str, *, cycle_id: str, producer: str, kind: str = 'incident',
          ticker: str | None = None, role: str | None = None,
          source_refs: list[str], evidence: str = '') -> str:
    text = text.strip()
    if kind not in KINDS or not cycle_id or not producer or not source_refs:
        raise ValueError('Learning record needs kind, full cycle identity, producer and source references')
    if len(text) < 10 or len(text) > 4000 or is_poisoned_response(text):
        raise ValueError('Learning content is empty, oversized or an error response')
    normalized = ' '.join(text.casefold().split())
    key = hashlib.sha256(f'{kind}|{ticker or ""}|{role or ""}|{normalized}'.encode()).hexdigest()
    timestamp = now()
    # Source occurrences are separate immutable rows so merging duplicates
    # cannot overwrite provenance or turn repeated assertions into validation.
    occurrence = hashlib.sha256(f'{key}|{cycle_id}|{producer}|{sorted(source_refs)}'.encode()).hexdigest()
    mongo_store.upsert_doc('learning_events', {'id': occurrence}, {
        'id': occurrence, 'record_id': key, 'event': 'observed',
        'cycle_id': cycle_id, 'producer': producer, 'source_refs': source_refs,
        'evidence': evidence[:8000], 'created_at': timestamp,
    }, insert_only=True)
    mongo_store.upsert_doc('learning_records', {'id': key}, {
        'id': key, 'contract_version': CONTRACT_VERSION, 'kind': kind,
        'text': text, 'content_hash': content_hash(text), 'ticker': ticker,
        'role': role, 'producer': producer, 'source_refs': source_refs,
        'first_cycle_id': cycle_id, 'created_at': timestamp,
        'status': 'candidate', 'validation_state': 'unverified',
        'valid_from': timestamp, 'valid_until': timestamp + timedelta(days=14 if kind == 'incident' else 30),
        'index_state': 'pending', 'index_retry_at': timestamp, 'index_attempts': 0,
    }, insert_only=True)
    mongo_store.update_docs('learning_records', {'id': key}, {
        '$set': {'last_observed_at': timestamp, 'last_cycle_id': cycle_id}})
    return key


def recall_incidents(query: str, limit: int = 3) -> list[dict]:
    """Pure read for the auditor. Unverified incidents are labelled as such."""
    words = set(re.findall(r'[a-z0-9_]{4,}', query.lower()))
    rows = mongo_store.find_docs('learning_records', {
        'kind': 'incident', 'status': {'$in': ['candidate', 'active']},
        'valid_until': {'$gt': now()},
    }, sort=[('last_observed_at', -1)], limit=200)
    ranked = sorted(rows, key=lambda r: len(words & set(re.findall(r'[a-z0-9_]{4,}', r['text'].lower()))), reverse=True)
    picked = []
    for row in ranked:
        terms = set(re.findall(r'[a-z0-9_]{4,}', row['text'].lower()))
        if not words & terms:
            continue
        if any(len(terms & t) / max(1, len(terms | t)) > .6 for _, t in picked):
            continue
        picked.append((row, terms))
        if len(picked) >= limit:
            break
    return [r for r, _ in picked]


def index_pending(limit: int = 5) -> dict:
    """Lease work from the record itself; a failed embedding cannot lose a write."""
    from app.db.vector_store import vector_store
    from app.services.embedding_service import embedder
    result = {'indexed': 0, 'failed': 0}
    for _ in range(limit):
        timestamp, token = now(), uuid.uuid4().hex
        row = mongo_store.find_one_and_update('learning_records', {
            'status': {'$in': ['candidate', 'active']}, 'valid_until': {'$gt': timestamp},
            '$or': [
                {'index_state': {'$in': ['pending', 'failed']}, 'index_retry_at': {'$lte': timestamp}},
                {'index_state': 'indexing', 'index_lease_until': {'$lt': timestamp}},
            ],
        }, {'$set': {'index_state': 'indexing', 'index_lease': token,
                    'index_lease_until': timestamp + timedelta(minutes=5)},
            '$inc': {'index_attempts': 1}}, sort=[('created_at', 1)], return_after=True)
        if not row:
            break
        query = {'id': row['id'], 'index_lease': token, 'content_hash': row['content_hash']}
        try:
            vector = embedder.embed_text(row['text'])
            eid = vector_store.store_embedding('learning_records', row['id'], row.get('ticker'), row['text'], list(vector))
            if not eid:
                raise ValueError('Embedding was rejected or not persisted')
            mongo_store.update_docs('learning_records', query, {'$set': {
                'index_state': 'ready', 'indexed_content_hash': row['content_hash'], 'indexed_at': now(),
            }, '$unset': {'index_lease': '', 'index_lease_until': '', 'index_error': ''}})
            result['indexed'] += 1
        except Exception as exc:
            mongo_store.update_docs('learning_records', query, {'$set': {
                'index_state': 'failed', 'index_error': str(exc)[:500],
                'index_retry_at': now() + timedelta(minutes=min(360, 10 * 2 ** min(row.get('index_attempts', 1), 5))),
            }, '$unset': {'index_lease': '', 'index_lease_until': ''}})
            result['failed'] += 1
    return result


def retire_expired() -> int:
    timestamp = now()
    return mongo_store.update_docs('learning_records', {
        'status': {'$in': ['candidate', 'active']}, 'valid_until': {'$lte': timestamp},
    }, {'$set': {'status': 'retired', 'retired_at': timestamp, 'retired_reason': 'validity_expired'}})


def collect_retired_indexes(limit: int = 200) -> dict:
    """Delete derived vectors only; retained source text can be reindexed."""
    result = {'checked': 0, 'deleted': 0}
    for source in ('learning_records', 'canonical_memories'):
        rows = mongo_store.find_docs(source, {'status': {'$in': ['retired', 'deprecated', 'quarantined']},
                    'index_state': {'$ne': 'retired'}}, limit=limit)
        for row in rows:
            result['checked'] += 1
            result['deleted'] += mongo_store.delete_docs('embeddings', {'source_table': source, 'source_id': row['id']})
            mongo_store.update_docs(source, {'id': row['id'], 'status': row['status']}, {'$set': {'index_state': 'retired'}})
    return result
