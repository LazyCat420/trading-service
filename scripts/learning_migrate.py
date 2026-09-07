"""Reversible v2 learning migration. Dry-run by default; snapshot before apply.

Run from trading-service: .venv/bin/python scripts/learning_migrate.py --output PATH
Add --apply only after compatible readers are deployed. No raw evidence deletion.
"""
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import argparse
import hashlib
import json
from datetime import datetime, timedelta, timezone
from bson import json_util
from pymongo import UpdateOne
from app.db import mongo_store
from app.services.learning.policy import BASELINES, BASELINE_VERSION, content_hash, skill_allowed

LEGACY = {'evolution_lessons':'retained_unverified_incident_evidence',
          'procedural_memory':'quarantined_unverified_procedure',
          'semantic_memory':'retained_unverified_fact_evidence',
          'canonical_memories':'quarantined_missing_v2_evidence',
          'agent_skills':'retained_skill_history',
          'working_memory':'retained_unacknowledged_reminder',
          'prospective_memory':'retained_unacknowledged_reminder'}

def fingerprint(rows):
    return hashlib.sha256(json_util.dumps(sorted(rows, key=lambda r:str(r['_id'])), sort_keys=True).encode()).hexdigest()

def run(output: Path, apply=False):
    timestamp = datetime.now(timezone.utc)
    active = mongo_store.find_docs('agent_skills', {'status':'active'})
    legacy = {name: mongo_store.find_docs(name, {'contract_version':{'$ne':2}}) for name in LEGACY}
    expired = mongo_store.find_docs('episodic_observations', {'promoted_to_memory':False, 'created_at':{'$lt':timestamp-timedelta(days=30)}})
    prism_legacy = {}  # Upstream Prism records are excluded at our gateway, never migrated.
    snapshot = {'captured_at':timestamp, 'active_skills':active, 'legacy':legacy,
                'expired_unpromoted':expired, 'prism_legacy':prism_legacy}
    output.parent.mkdir(parents=True, exist_ok=True)
    # Never overwrite the backup of an earlier migration attempt.
    with output.open('x') as f:
        f.write(json_util.dumps(snapshot, indent=2))
    summary = {'snapshot':str(output), 'snapshot_sha256':hashlib.sha256(output.read_bytes()).hexdigest(),
        'active_fingerprint':fingerprint(active), 'active_skills':len(active),
        'unreviewed_active':sum(not skill_allowed(r['agent_name'],r.get('skill_text','')) for r in active),
        'legacy_counts':{k:len(v) for k,v in legacy.items()},
        'expired_unpromoted_retained':len(expired), 'prism_counts':{k:len(v) for k,v in prism_legacy.items()}, 'applied':False}
    if not apply:
        return summary
    mongo_store.ensure_indexes()
    with mongo_store.with_txn() as session:
        current = list(mongo_store._coll('agent_skills').find({'status':'active'}, session=session))
        if fingerprint(current) != summary['active_fingerprint']:
            raise RuntimeError('Active skill versions changed after snapshot; rerun with a new snapshot')
        for row in active:
            if not skill_allowed(row['agent_name'], row.get('skill_text','')):
                mongo_store.update_docs('agent_skills', {'_id':row['_id'], 'status':'active'}, {'$set':{
                    'status':'quarantined', 'quarantined_at':timestamp,
                    'quarantine_reason':'unreviewed_policy_or_role_conflicts',
                    'superseded_by_version':BASELINE_VERSION}}, session=session)
        for role, text in BASELINES.items():
            mongo_store.upsert_doc('agent_skills', {'agent_name':role, 'version':BASELINE_VERSION}, {
                'id':f'reviewed-methods-v2:{role}', 'agent_name':role, 'version':BASELINE_VERSION,
                'skill_text':text, 'skill_hash':content_hash(text), 'status':'active',
                'contract_version':2, 'validation_state':'code_reviewed', 'created_at':timestamp,
                'cycle_id':'learning-migration-20260907', 'action':'REVIEWED_BASELINE',
                'rationale':'Restore role and risk contracts; held-out benchmark recorded separately'}, insert_only=True, session=session)
        mongo_store.upsert_doc('learning_migrations', {'id':'v2-20260907'}, {
            'id':'v2-20260907', **summary, 'applied':True, 'applied_at':timestamp}, insert_only=True, session=session)
    dispositions = []
    for name, rows in {**legacy, 'episodic_observations':expired}.items():
        for row in rows:
            key = f'trading:{name}:{row["_id"]}'
            dispositions.append(UpdateOne({'id':key}, {'$setOnInsert':{
                'id':key, 'database':'trading', 'collection':name, 'source_id':str(row['_id']),
                'disposition':LEGACY.get(name, 'retained_historical_expired_for_current_retrieval'),
                'migrated_at':timestamp, 'snapshot_sha256':summary['snapshot_sha256']}}, upsert=True))
    for name, rows in prism_legacy.items():
        for row in rows:
            key = f'prism:{name}:{row["_id"]}'
            dispositions.append(UpdateOne({'id':key}, {'$setOnInsert':{
                'id':key, 'database':'prism', 'collection':name, 'source_id':str(row['_id']),
                'disposition':'quarantined_missing_deliverable_validation', 'migrated_at':timestamp,
                'snapshot_sha256':summary['snapshot_sha256']}}, upsert=True))
    if dispositions:
        mongo_store._coll('learning_legacy_dispositions').bulk_write(dispositions, ordered=False)
    mongo_store.update_docs('learning_migrations', {'id':'v2-20260907'}, {'$set':{'dispositions_complete':True, 'disposition_count':len(dispositions)}})
    summary.update(applied=True, disposition_count=len(dispositions))
    return summary

if __name__ == '__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--apply', action='store_true')
    args=parser.parse_args()
    print(json.dumps(run(args.output, args.apply), indent=2, default=str))
