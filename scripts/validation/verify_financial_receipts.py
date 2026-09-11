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
    artifact, record = row['artifact'], row['evidence']
    raw = _parse_artifact(row['calls'][-1]['response'], 'final_decision', 'v3_board_of_directors')
    preserved = all(raw.get(key) == artifact.get(key) for key in
                    ('action', 'confidence', 'position_size_pct', 'reasoning_steps'))
    def answers(value):
        value = value.get('research_answers', [])
        if isinstance(value, dict):
            return {key: answer if isinstance(answer, list) else answer.get('step_ids') for key, answer in value.items()}
        return {answer['item_id']: answer.get('step_ids') for answer in value}
    preserved &= answers(raw) == answers(artifact)
    result = {'financial_evidence_version': 1, 'financial_evidence_record': record,
              'financial_decision': artifact, 'action': artifact['action'],
              'estimate': {key: artifact.get(key) for key in
                           ('position_size_pct', 'stop_loss', 'take_profit', 'dynamic_trigger', 'entry_mode', 'trigger_purpose')}}
    baseline_errors = execution_errors(result)
    forged = deepcopy(result)
    forged['financial_decision']['financial_claims'][0]['value'] = 'tampered'
    forged['financial_audit'] = {'status': 'consistent', 'errors': []}
    altered_claim_blocked = bool(execution_errors(forged))
    changed_action = deepcopy(result)
    changed_action['action'] = 'BUY' if artifact['action'] != 'BUY' else 'SELL'
    changed_action_blocked = bool(execution_errors(changed_action))
    return {'index': row['index'], 'case': row['case'], 'authored_fields_preserved': preserved,
            'current_audit': audit_decision(artifact, record)['status'],
            'execution_errors': baseline_errors, 'tampered_claim_blocked': altered_claim_blocked,
            'changed_order_action_blocked': changed_action_blocked,
            'passed': preserved and not baseline_errors and altered_claim_blocked and changed_action_blocked}


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
