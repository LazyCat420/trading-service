"""Pure eligibility checks. Rewriting a record does not refresh its evidence."""
from datetime import datetime, timezone
from app.utils.tz import ensure_aware
from app.services.cycle_scope import is_synthetic_cycle


def eligible_memory(memory: dict, *, as_of: datetime | None = None) -> bool:
    at = ensure_aware(as_of) if as_of else datetime.now(timezone.utc)
    if memory.get('status') != 'active' or memory.get('contract_version') != 2:
        return False
    if memory.get('validation_state') != 'source_verified' or not memory.get('source_evidence'):
        return False
    evidence = memory.get('source_evidence')
    if not isinstance(evidence, list) or any(
        not isinstance(source, dict) or is_synthetic_cycle(source.get('cycle_id'))
        for source in evidence
    ):
        return False
    valid_from = ensure_aware(memory.get('valid_from'))
    valid_until = ensure_aware(memory.get('valid_until'))
    return bool(valid_from and valid_until and valid_from <= at < valid_until)


def eligible_search_hits(hits: list[dict]) -> list[dict]:
    """Generic dense/hybrid search cannot bypass learned-content eligibility."""
    from app.db import mongo_store
    blocked = {'evolution_lessons', 'procedural_memory', 'semantic_memory', 'learning_records'}
    ids = [r['source_id'] for r in hits if r.get('source_table') == 'canonical_memories']
    valid = {}
    if ids:
        rows = mongo_store.find_docs('canonical_memories', {'id': {'$in': ids}})
        valid = {m['id']: m for m in rows if eligible_memory(m)}
    result = []
    for row in hits:
        if row.get('source_table') in blocked:
            continue
        if row.get('source_table') == 'canonical_memories':
            if row['source_id'] not in valid:
                continue
            row = {**row, 'content_preview': valid[row['source_id']]['summary']}
        result.append(row)
    return result
