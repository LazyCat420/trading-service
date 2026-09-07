"""Export completed benchmark measurements, excluding private prompts and outputs."""
import hashlib
import json
import os
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
BASE = Path(os.environ.get('MEMORY_BENCHMARK_DIR', ROOT / '.scratch/memory-isolation-20260907'))
DEST = ROOT / 'trading-service/docs/benchmarks/memory-isolation-role-replay-20260907.json'


def read(base, filename):
    return json.loads((base / filename).read_text())


def select(row, fields):
    return {field: row[field] for field in fields if field in row}


def role_cohort(base):
    rows = read(base, 'role-results.json')
    cases = {c['id']: c for c in read(base, 'role-inputs.json')}
    quality = read(base, 'role-quality.json')
    qmap = {(q['case'], q.get('repeat', 0), q['arm']): q for q in quality}
    measurements = []
    for row in rows:
        q = qmap[(row['case'], row.get('repeat', 0), row['arm'])]
        result = select(row, (
            'case', 'arm', 'repeat', 'transport', 'model', 'started_at',
            'whiteboard_writes', 'prompt_tokens', 'completion_tokens',
            'tool_calls', 'tool_errors', 'usage_complete', 'stop', 'elapsed_s',
        ))
        # Exceptions can contain HTTP payloads: expose the class only.
        result['error_type'] = (row.get('error') or '').split(':', 1)[0] or None
        result['fixture_symbol_mismatches'] = []
        for turn in row['turns']:
            for call, returned in zip(turn['message'].get('tool_calls', []), turn['tool_results']):
                try:
                    args = json.loads(call['function'].get('arguments') or '{}')
                except (ValueError, TypeError):
                    continue
                ticker = args.get('ticker') or args.get('symbol') if isinstance(args, dict) else None
                expected_ticker = cases[row['case']]['ticker']
                if returned.get('offline_frozen_evidence') and isinstance(ticker, str) and ticker.upper() != expected_ticker:
                    result['fixture_symbol_mismatches'].append({
                        'tool': call['function']['name'], 'requested_ticker': ticker,
                        'returned_case_ticker': expected_ticker,
                    })
        result['response_sha256'] = hashlib.sha256(row['response'].encode()).hexdigest()
        result['quality'] = select(q, (
            'raw_json', 'parser_accepted', 'final_turn', 'first_pass_usable',
            'required_step_complete', 'complete_with_required_step',
            'frozen_constraints', 'frozen_constraint_applicability', 'tool_errors_by_cause',
        ))
        result['quality']['schema_error_count'] = len(q['schema_errors'])
        result['quality']['decision_contract_error_count'] = len(q['decision_contract_errors'])
        result['quality']['facts'] = dict(Counter(f['status'] for f in q['facts']))
        result['turns'] = [select(t, (
            'load_before', 'first_delta_s', 'headers_s', 'elapsed_s', 'usage', 'finish_reason',
        )) for t in row['turns']]
        measurements.append(result)
    return {'summary': read(base, 'role-summary.json'), 'measurements': measurements}


def component_cohort(base):
    summary = read(base, 'component-summary.json')
    rows = summary.pop('measurements')
    measurements = []
    for row in rows:
        item = select(row, (
            'case', 'arm', 'repeat', 'warmup', 'model', 'started_at', 'removed_chars',
            'payload_hash', 'usage', 'first_content_s', 'finish_reason',
            'load_before', 'valid', 'elapsed_s',
        ))
        item['error_type'] = (row.get('error') or '').split(':', 1)[0] or None
        measurements.append(item)
    return {'summary': summary, 'measurements': measurements}


def main():
    rows = read(BASE, 'role-results.json')
    components = read(BASE, 'component-results.json')
    keys = {(r['case'], r['repeat'], r['arm']) for r in rows}
    expected = {(c['id'], repeat, arm) for c in read(BASE, 'role-inputs.json')
                for repeat in (0, 1) for arm in ('before', 'after')}
    if keys != expected or len(rows) != 32 or any(r.get('transport') != 'stream' for r in rows):
        raise SystemExit('Refusing final export: the fixed 32-run streaming cohort is incomplete.')
    if len(components) != 30 or sum(not r['warmup'] for r in components) != 28:
        raise SystemExit('Refusing final export: sequential component cohort is incomplete.')
    summary = read(BASE, 'role-summary.json')
    if summary['completed_pairs'] != 16 or any(summary['arms'][arm]['runs'] != 16 for arm in ('before', 'after')):
        raise SystemExit('Refusing final export: role summary is stale; rescore and summarize first.')
    last_role_end = max(r['started_at'] + r['elapsed_s'] for r in rows)
    if min(r['started_at'] for r in components) < last_role_end:
        raise SystemExit('Refusing final export: component measurements were not collected after the role cohort.')
    manual = read(BASE, 'role-manual-review.json')
    if len(manual) != 32 or {(r['case'], r['repeat'], r['arm']) for r in manual} != expected:
        raise SystemExit('Refusing final export: manual review coverage is incomplete.')
    result = {
        'experiment': 'upstream-memory-only-role-replay',
        'arms': {'before': 'upstream memory retained', 'after': 'upstream memory removed'},
        'protocol': {
            'unique_cases': 8, 'repetitions': 2, 'matched_pairs': 16,
            'order': 'AB/BA alternates by case and reverses in repetition two',
            'temperature': 0, 'min_p': 0, 'thinking_enabled': False,
            'role_output_cap_tokens': 8192, 'total_role_deadline_seconds': 900,
            'frozen_input_sha256': '8dbcf55ff4b52d9dcbeebf18df30f3d921291d6045dc4ecb144f47315bf0309b',
            'frozen_files_sha256': {name: hashlib.sha256((BASE / name).read_bytes()).hexdigest() for name in ('role-inputs.json', 'role-results.json', 'protocol.md', 'role-manual-review.json')},
            'manual_review': {'attempts_reviewed': len(manual), 'blinded': False, 'type': 'Evidence-grounding review, not an independent outcome score'},
            'privacy': 'Private prompts, source documents, tool arguments/results and generated research are excluded.',
            'completion_metric': 'Usable artifact plus mandatory Junior note; does not certify all-role workflow compliance.',
            'cost_policy': 'Use only matched pairs with complete usage on both sides. Other observed tokens are lower bounds.',
        },
        'primary_streaming': role_cohort(BASE),
        'sequential_component': component_cohort(BASE),
        'exploratory_buffered': {
            'note': 'Separate transport/deadline cohort; seven completed attempts and one administratively censored in-flight attempt with unknown usage.',
            **role_cohort(BASE / 'nonstream-run'),
        },
        'exploratory_initial_component': {
            'note': 'Final two roles overlapped the invalid role setup run; do not pool latency with sequential rerun.',
            **component_cohort(BASE / 'initial-component-run'),
        },
        'invalid_setup': 'Archived locally, excluded from model-quality comparisons; local dependency setup failure and interrupted work.',
    }
    DEST.write_text(json.dumps(result, indent=2) + '\n')
    print(DEST)


if __name__ == '__main__':
    main()
