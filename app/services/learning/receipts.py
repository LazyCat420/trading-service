"""Trading-owned artifact receipts; never writes to Prism's database.

The local outbox and validated ledger preserve exact original-output identity.
The lazy-agent-service boundary reads the same Trading database. Upstream
workflow delivery remains excluded until a separately evaluated reader exists.
"""
from datetime import datetime, timezone
import hashlib
import json
import logging

from app.db import mongo_store
from app.services.learning.policy import CONTRACT_VERSION, content_hash

logger = logging.getLogger(__name__)


def record_delivery(*, cycle_id: str, ticker: str, role: str, system: str, user: str,
                    skill_text: str = '', skill_version: int | None = None,
                    memory_ids: list[str] | None = None) -> str:
    key = hashlib.sha256(f'{cycle_id}:{ticker}:{role}:{content_hash(system+user)}'.encode()).hexdigest()
    mongo_store.upsert_doc('learning_delivery_receipts', {'id': key}, {
        'id': key, 'cycle_id': cycle_id, 'ticker': ticker, 'role': role,
        'contract_version': CONTRACT_VERSION, 'created_at': datetime.now(timezone.utc),
        'skill_version': skill_version if skill_text and skill_text in system else None,
        'skill_hash': content_hash(skill_text) if skill_text and skill_text in system else None,
        'memory_ids': memory_ids or [], 'system_hash': content_hash(system),
        'user_hash': content_hash(user), 'system_chars': len(system), 'user_chars': len(user), 'event': 'delivered',
    }, insert_only=True)
    return key


def queue_artifact_receipt(result: dict, *, cycle_id: str, ticker: str, role: str,
                           artifact_type: str, artifact: dict, valid: bool) -> str | None:
    identity = result.get('learning_identity') or {}
    if not identity.get('conversation_id') or not identity.get('output_hash'):
        return None  # /chat has no Prism workflow; unknown identity grants nothing
    from app.v3.arithmetic_audit import audit_artifact
    arithmetic = audit_artifact(artifact)
    arithmetic_invalid = bool(arithmetic['errors'])
    valid = valid and not arithmetic_invalid
    key = hashlib.sha256(f"{identity['conversation_id']}:{identity['agent']}:{identity['output_hash']}".encode()).hexdigest()
    row = {
        'id': key, 'contract_version': CONTRACT_VERSION, 'validator': 'v3_artifact',
        'project': identity['project'], 'profileId': identity.get('profile_id') or 'default',
        'agent': identity['agent'], 'conversationId': identity['conversation_id'],
        'outputHash': identity['output_hash'], 'cycle_id': cycle_id, 'ticker': ticker,
        'role': role, 'artifact_type': artifact_type,
        'artifact_hash': content_hash(json.dumps(artifact, sort_keys=True, default=str)),
        'valid': valid, 'reason': ('arithmetic_inconsistent' if arithmetic_invalid else
                                  'validated_original' if valid else 'degraded_or_repaired'),
        'arithmetic_checked': arithmetic['checked'], 'arithmetic_errors': arithmetic['errors'],
        'created_at': datetime.now(timezone.utc), 'delivery_state': 'pending',
    }
    mongo_store.upsert_doc('learning_artifact_receipts', {'id': key}, row, insert_only=True)
    return key


def deliver_pending(limit: int = 50) -> dict:
    rows = mongo_store.find_docs('learning_artifact_receipts', {'delivery_state': 'pending'}, limit=limit)
    result = {'delivered': 0, 'failed': 0}
    if not rows:
        return result
    for row in rows:
        try:
            payload = {k: v for k, v in row.items() if k not in {'_id', 'delivery_state'}}
            mongo_store.upsert_doc('learning_validation_receipts', {'id': row['id']}, payload, insert_only=True)
            mongo_store.update_docs('learning_artifact_receipts', {'id': row['id']}, {'$set': {
                'delivery_state': 'delivered', 'delivered_at': datetime.now(timezone.utc)}})
            result['delivered'] += 1
        except Exception as exc:
            result['failed'] += 1
            logger.error('[LearningReceipts] delivery failed for %s: %s', row['id'], exc)
    return result
