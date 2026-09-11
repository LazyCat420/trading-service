"""Offline release checks for retained model responses; never calls a provider."""
from copy import deepcopy
import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from app.v3.agent_runner import _parse_artifact
from app.v3.financial_claims import audit_decision, execution_errors
from scripts.test_live_financial_record import source_hashes


def verify(row):
    artifact, record = row.get('artifact'), row['evidence']
    def answers(value):
        value = value.get('research_answers', [])
        if isinstance(value, dict):
            return {key: answer if isinstance(answer, list) else answer.get('step_ids') for key, answer in value.items()}
        return {answer['item_id']: answer.get('step_ids') for answer in value}
    def same_authored_fields(raw):
        return (isinstance(raw, dict) and isinstance(artifact, dict)
                and all(raw.get(key) == artifact.get(key) for key in
                        ('action', 'confidence', 'position_size_pct', 'reasoning_steps'))
                and answers(raw) == answers(artifact))
    parsed = [_parse_artifact(call.get('response') or '', 'final_decision', 'v3_board_of_directors')
              for call in row['calls']]
    preserved = any(same_authored_fields(raw) for raw in parsed) if artifact else None
    audit = audit_decision(artifact, record) if artifact else {'status': 'unresolved'}
    result = {'financial_evidence_version': 1, 'financial_evidence_record': record,
              'financial_decision': artifact, 'action': (artifact or {}).get('action', 'HOLD'),
              'estimate': {key: (artifact or {}).get(key) for key in
                           ('position_size_pct', 'stop_loss', 'take_profit', 'dynamic_trigger', 'entry_mode', 'trigger_purpose')}}
    baseline_errors = execution_errors(result)
    forged = deepcopy(result)
    forged['financial_audit'] = {'status': 'consistent', 'errors': []}
    claims = (forged.get('financial_decision') or {}).get('financial_claims') or []
    if claims:
        claims[0]['value'] = 'tampered'
    tampered_or_unresolved_blocked = bool(execution_errors(forged))
    changed_action = deepcopy(result)
    changed_action['action'] = 'BUY' if result['action'] != 'BUY' else 'SELL'
    changed_action_blocked = bool(execution_errors(changed_action))
    accepted = audit['status'] == 'consistent'
    if accepted:
        preserved = bool(parsed) and same_authored_fields(parsed[-1])
    expected_boundary = not baseline_errors if accepted else bool(baseline_errors)
    return {'index': row['index'], 'case': row['case'], 'authored_fields_preserved': preserved,
            'current_audit': audit['status'], 'execution_errors': baseline_errors,
            'tampered_or_unresolved_blocked_despite_forged_pass': tampered_or_unresolved_blocked,
            'changed_order_action_blocked': changed_action_blocked,
            'passed': (preserved is not False and expected_boundary and tampered_or_unresolved_blocked
                       and changed_action_blocked and audit['status'] == row['audit']['status'])}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('cohorts', type=Path, nargs='+')
    parser.add_argument('--out', required=True, type=Path)
    args = parser.parse_args()
    rows = [{'cohort': path.name, **verify(json.loads(file.read_text()))}
            for path in args.cohorts for file in sorted(path.glob('[0-9][0-9].json'))]
    report = {'source_hashes': source_hashes(), 'new_model_calls': 0, 'rows': rows,
              'passed': bool(rows) and all(row['passed'] for row in rows)}
    with args.out.open('x') as output:
        json.dump(report, output, indent=2)
    print(json.dumps({'passed': report['passed'], 'cases': len(rows)}))
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
