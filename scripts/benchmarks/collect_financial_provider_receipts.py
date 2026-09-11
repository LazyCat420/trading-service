"""Read provider snapshots for retained synthetic financial benchmark cases."""
import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from app.db.mongo_store import get_doc_db


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--cohort', required=True)
    args = parser.parse_args()
    assert '/' not in args.cohort and '..' not in args.cohort
    base = ROOT/'docs/benchmarks/evidence/financial-reasoning-2026-09-11'
    output = base/'provider-receipts'/args.cohort
    output.mkdir(parents=True, exist_ok=True)
    db = get_doc_db()
    counts = []
    for path in sorted((base/args.cohort).glob('[0-9][0-9].json')):
        record = json.loads(path.read_text())
        assert record['cycle_id'].startswith('bench-financial-')
        assert record['evidence']['ticker'] == 'EVLT'
        destination = output/path.name
        assert not destination.exists(), 'Preserve every receipt collection'
        events = list(db.pipeline_trace_events.find({'cycle_id':record['cycle_id'], 'stage':'provider.payload'}).sort('created_at', 1))
        snapshots = []
        for event in events:
            blob = db.pipeline_trace_blobs.find_one({'_id':event['snapshot']['hash']})
            assert blob and not blob.get('truncated'), 'Provider snapshot unavailable or truncated'
            payload = json.loads(blob['content'])
            snapshots.append({'created_at':event['created_at'], 'attributes':event.get('attributes'),
                              'snapshot':event['snapshot'], 'payload':payload})
        result = {'cycle_id':record['cycle_id'], 'cohort':args.cohort, 'index':record['index'],
                  'endpoint_calls':sum(c['kind'].startswith('live_') for c in record['calls']), 'provider_attempts':len(snapshots),
                  'scope':'Read-only trading-owned provider input snapshots, including upstream internal recovery attempts. Private reasoning omitted by source recorder.',
                  'snapshots':snapshots}
        destination.write_text(json.dumps(result, indent=2, default=str))
        counts.append({k:result[k] for k in ('index','endpoint_calls','provider_attempts')})
    print(json.dumps(counts))


if __name__ == '__main__':
    main()
