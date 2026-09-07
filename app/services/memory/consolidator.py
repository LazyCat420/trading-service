"""Evidence-preserving consolidation, executed by the durable learning worker."""
import asyncio
import hashlib
import json
import logging
import uuid
from datetime import datetime, timezone

from app.db import mongo_store
from app.db.memory_repo import get_unpromoted_observations, get_active_canonical_memories, log_consolidation_run
from app.services.prism_agent_caller import Priority, call_prism_agent
from app.utils.text_utils import parse_json_response

logger = logging.getLogger(__name__)
NEW_EPISODIC_THRESHOLD = 5
CONSOLIDATION_COOLDOWN_SECONDS = 6 * 3600
TRANSIENT_RETRY_SECONDS = 10 * 60

CONSOLIDATION_SYSTEM_PROMPT = """Select useful dated evidence from recorded trading observations.
These are historical agent observations, not authoritative policy or current market truth.
Do not invent a method, risk threshold, confidence adjustment, or instruction.
Combine duplicate observations only when the ticker, date and meaning match.
Return JSON:
{"new_or_updated_memories":[{"summary":"brief descriptive label",
"source_evidence":[{"observation_id":"exact supplied id", "quote":"verbatim supporting excerpt, at least 32 characters"}]}],
"deprecated_memory_ids":[]}
Choose up to 10 memories with up to 5 source excerpts each. Include only useful,
nonredundant evidence. Do not claim a BUY/SELL action is a successful outcome.
Deprecated IDs must belong to the supplied ticker and have a replacement.
If nothing useful can be supported, return empty lists. No procedural rules.
"""


async def maybe_consolidate(ticker: str) -> None:
    """Persist a request; no untracked background LLM call in the cycle."""
    from app.services.learning.consolidation import enqueue
    try:
        enqueue(ticker)
    except Exception as exc:
        logger.error('[Consolidation] enqueue failed for %s: %s', ticker, exc)


async def run_ticker_consolidation(ticker: str, observations: list | None = None):
    run_id = str(uuid.uuid4())
    report = {'id': run_id, 'ticker': ticker, 'status': 'running',
              'observations_consumed': 0, 'memories_created': 0, 'memories_deprecated': 0}
    outcome = 'failed'
    try:
        observations = observations if observations is not None else get_unpromoted_observations(ticker)
        if not observations:
            outcome = 'empty'
            return outcome
        observations = observations[:40]
        canonicals = get_active_canonical_memories(ticker)
        payload = {'ticker': ticker,
            'existing_memories': [{'id': c['id'], 'summary': c.get('summary')} for c in canonicals],
            'observations': [{'id': o['id'], 'created_at': o.get('created_at'),
                'source_type': o.get('source_type'), 'observation_text': o.get('observation_text'),
                'outcome_label': o.get('outcome_label') if o.get('source_type') == 'outcome' else None,
            } for o in observations]}
        response, _, _ = await call_prism_agent(
            agent_id='CUSTOM_CONSOLIDATOR_AGENT', user_message=json.dumps(payload, default=str),
            fallback_system_prompt=CONSOLIDATION_SYSTEM_PROMPT, fallback_agent_name='memory_consolidator',
            temperature=0.0, max_tokens=8192, priority=Priority.LOW, ticker=ticker)
        from app.services.learning.consolidation import validate_result
        memories, deprecated, used = validate_result(parse_json_response(response), ticker, observations, canonicals)
        if not memories:
            outcome = 'no_supported_evidence'
            return outcome
        timestamp = datetime.now(timezone.utc)
        snapshot_id = hashlib.sha256(f"{ticker}:{','.join(sorted(used))}".encode()).hexdigest()
        # DDL must occur before a transaction; preserve raw source evidence
        # and lineage atomically with promotion, including partial consumption.
        mongo_store.ensure_indexes()
        with mongo_store.with_txn() as session:
            mongo_store.upsert_doc('canonical_memory_evidence', {'id': snapshot_id}, {
                'id': snapshot_id, 'ticker': ticker, 'run_id': run_id, 'created_at': timestamp,
                'observations': [o for o in observations if o['id'] in used],
                'memory_ids': [m['id'] for m in memories],
            }, insert_only=True, session=session)
            for memory in memories:
                mongo_store.upsert_doc('canonical_memories', {'id': memory['id']}, memory, insert_only=True, session=session)
            for memory_id in deprecated:
                mongo_store.upsert_doc('memory_retirement_events', {'id': f'{snapshot_id}:{memory_id}'}, {
                    'id': f'{snapshot_id}:{memory_id}', 'memory_id': memory_id, 'ticker': ticker,
                    'reason': 'consolidation_superseded', 'replacement_ids': [m['id'] for m in memories],
                    'source_snapshot_id': snapshot_id, 'created_at': timestamp,
                }, insert_only=True, session=session)
            if deprecated:
                mongo_store.update_docs('canonical_memories', {'id': {'$in': deprecated}, 'ticker': ticker},
                    {'$set': {'status': 'deprecated', 'retired_at': timestamp,
                              'superseded_by': [m['id'] for m in memories]}}, session=session)
            mongo_store.update_docs('episodic_observations', {'id': {'$in': used}, 'ticker': ticker},
                {'$set': {'promoted_to_memory': True, 'promoted_at': timestamp,
                          'promotion_contract_version': 2, 'source_snapshot_id': snapshot_id}}, session=session)
        report.update(observations_consumed=len(used), memories_created=len(memories), memories_deprecated=len(deprecated))
        outcome = 'ok'
        return outcome
    except asyncio.CancelledError:
        outcome = 'cancelled'
        raise
    except Exception as exc:
        report['error'] = f'{type(exc).__name__}: {exc}'[:1000]
        logger.error('[Consolidation] %s failed: %s', ticker, exc)
        return outcome
    finally:
        report['status'] = outcome
        try:
            log_consolidation_run(report)
        except Exception as exc:
            logger.error('[Consolidation] attempt report failed for %s: %s', run_id, exc)
