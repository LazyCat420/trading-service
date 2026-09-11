"""Report model acceptance separately from safety enforcement and abstention."""
from copy import deepcopy
from app.v3.financial_repair import validated_answers


def assess_attempt(artifact, record, artifact_type='final_decision', extra_errors=()):
    from app.v3.financial_claims import audit_decision
    from app.v3.financial_reasoning import render_reasoning_artifact
    from app.v3.artifacts import validate_artifact
    from app.v3.decision_contract import entry_errors
    artifact = artifact if isinstance(artifact, dict) else {}
    audit = audit_decision(artifact, record)
    rendered, errors = render_reasoning_artifact(artifact, record)
    errors += validate_artifact(artifact_type, deepcopy(rendered))
    errors += entry_errors(rendered)
    errors += list(extra_errors)
    return {'accepted': audit['status'] == 'consistent' and not errors,
            'evidence_consistent': audit['status'] == 'consistent',
            'action': artifact.get('action'),
            'valid_answer_ids': sorted(validated_answers(artifact, record)),
            'error_kinds': sorted({e['kind'] for e in audit['errors']}),
            'contract_errors': errors,
            'prevented_answer_regressions': artifact.get('_financial_repair_preservation', {}).get('prevented_answer_regressions', [])}


def quality_metrics(attempts, final, record, final_audit):
    first = attempts[0] if attempts else {}
    last = attempts[-1] if len(attempts) > 1 else None
    final_valid = set(validated_answers(final, record))
    before = set(first.get('valid_answer_ids', []))
    return {'first_response_accepted': first.get('accepted', False),
            'repair_attempted': len(attempts) > 1,
            'repair_succeeded': bool(last and last.get('accepted') and final_audit.get('status') == 'consistent'),
            'final_accepted': final_audit.get('status') == 'consistent',
            'repair_valid_answer_regressions': sorted(before - set(last.get('valid_answer_ids', []))) if last else [],
            'final_valid_answer_regressions': sorted(before - final_valid),
            'prevented_answer_regressions': last.get('prevented_answer_regressions', []) if last else [],
            'initial_action': first.get('action'), 'final_action': final.get('action'),
            'execution_authority': 'eligible_for_further_gates' if final_audit.get('status') == 'consistent' else 'blocked',
            'limitation': 'Evidence consistency is not investment merit; blocked proposals are model failures and safety successes.'}
