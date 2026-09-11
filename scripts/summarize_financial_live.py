"""Summarize retained runs without equating blocked output or HOLD with quality."""
import argparse
from collections import Counter
import json
from pathlib import Path


def summarize_rows(rows):
    measured = [r for r in rows if 'quality' in r]
    first = sum(r['quality']['first_response_accepted'] for r in measured)
    repairs = sum(r['quality']['repair_attempted'] for r in measured)
    repaired = sum(r['quality']['repair_succeeded'] for r in measured)
    tested = [r for r in rows if r.get('execution_gate', {}).get('tested')]
    return {'rows': len(rows), 'modes': dict(Counter(r.get('mode', 'unknown') for r in rows)), 'completed': sum('outcome' in r for r in rows),
            'scenario_families': dict(Counter(r.get('scenario_family', r.get('case', 'unknown')) for r in rows)),
            'live_calls': sum(c['kind'].startswith('live_') for r in rows for c in r.get('calls', [])),
            'http_200': sum(c.get('http_status') == 200 and c['kind'].startswith('live_') for r in rows for c in r.get('calls', [])),
            'quality_measured_rows': len(measured), 'unmeasured_rows': len(rows) - len(measured),
            'first_response_accepted': first, 'first_response_acceptance_rate': first / len(measured) if measured else None,
            'repair_attempts': repairs, 'repair_successes': repaired, 'repair_success_rate': repaired / repairs if repairs else None,
            'repair_valid_answer_regressions': sum(len(r['quality']['repair_valid_answer_regressions']) for r in measured),
            'prevented_answer_regressions': sum(len(r['quality'].get('prevented_answer_regressions', [])) for r in measured),
            'final_evidence_consistent': sum(r.get('audit', {}).get('status') == 'consistent' for r in rows),
            'final_actions': dict(Counter((r.get('artifact') or {}).get('action', 'NO_DECISION') for r in rows)),
            'accepted_actions': dict(Counter((r.get('artifact') or {}).get('action', 'NO_DECISION') for r in rows if r.get('audit', {}).get('status') == 'consistent')),
            'execution_gate_tested_rows': len(tested),
            'invalid_proposals_admitted': sum(r['execution_gate']['invalid_proposal_admitted'] for r in tested) if tested else None,
            'orders_submitted': sum(r['execution_gate']['orders_submitted'] for r in tested) if tested else None,
            'error_kinds': dict(Counter(e['kind'] for r in rows for e in r.get('audit', {}).get('errors', []))),
            'limitation': 'Evidence consistency and safety checks are separate. Repeated variants are not independent financial scenarios. HOLD frequency is reported; acceptance is not investment merit. Missing measurements are not zero.'}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('cohort', type=Path)
    args = parser.parse_args()
    rows = [json.loads(p.read_text()) for p in sorted(args.cohort.glob('[0-9][0-9].json'))]
    if not rows:
        raise SystemExit('No completed benchmark rows; no quality claim is possible.')
    print(json.dumps(summarize_rows(rows), indent=2))


if __name__ == '__main__':
    main()
