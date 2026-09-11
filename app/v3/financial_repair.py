"""Bounded patches preserve checked answers, never supply an unselected fact."""
from copy import deepcopy

PLAN_FIELDS = ('action', 'position_size_pct', 'entry_mode', 'trigger_purpose',
               'dynamic_trigger', 'stop_loss', 'take_profit')
RENDERED = ('reasoning', 'financial_claims', '_financial_audit',
            '_financial_explanation_provenance', '_financial_claim_reference_ids')


def answers_by_id(artifact):
    answers = artifact.get('research_answers', []) if isinstance(artifact, dict) else []
    if isinstance(answers, dict):
        answers = [({'item_id': key, 'step_ids': value} if isinstance(value, list)
                    else {'item_id': key, **value} if isinstance(value, dict) else {})
                   for key, value in answers.items()]
    return answers if isinstance(answers, list) else []


def validated_answers(artifact, record):
    from app.v3.financial_claims import audit_decision
    from app.v3.financial_reasoning import reasoning_catalog
    if not isinstance(artifact, dict) or artifact.get('financial_reasoning_version') != 2 or not record:
        return {}
    catalog = reasoning_catalog(record, artifact)
    if not catalog:
        return {}
    answers = answers_by_id(artifact)
    valid = {}
    for q in record.get('questions', []):
        matches = [a for a in answers if isinstance(a, dict) and a.get('item_id') == q['id']]
        if len(matches) != 1:
            continue
        probe = {k: deepcopy(artifact[k]) for k in PLAN_FIELDS if k in artifact}
        probe.update(financial_reasoning_version=2, action='HOLD', position_size_pct=0,
                     reasoning_steps=[next(iter(catalog))], research_answers=matches)
        scoped = {**record, 'questions': [q]}
        if audit_decision(probe, scoped)['status'] == 'consistent':
            valid[q['id']] = {'item_id': q['id'], 'step_ids': deepcopy(matches[0]['step_ids'])}
    return valid


def repair_context(artifact, record):
    valid = validated_answers(artifact, record)
    return {'validated_answers': list(valid.values()),
            'questions_to_correct': [q['id'] for q in (record or {}).get('questions', []) if q['id'] not in valid],
            'plan_dependency_rule': 'Reselect plan_* calculations after changing action, size, timing or price plan. Supplied scenarios remain required after HOLD.'}


def merge_repair(original, candidate, record):
    """Return candidate and merge errors. Caller must revalidate the entire result."""
    if not isinstance(candidate, dict):
        return candidate, ['Repair is not an object.']
    partial = candidate.get('financial_repair_version') == 1
    if not partial and candidate.get('financial_reasoning_version') != 2:
        return candidate, []  # legacy repairs still undergo the existing full audit
    if partial and original.get('financial_reasoning_version') != 2:
        return candidate, ['A partial repair requires a structured original decision.']
    valid = validated_answers(original, record)
    supplied = answers_by_id(candidate)
    errors = []
    plan_changed = any(k in candidate and candidate[k] != original.get(k) for k in PLAN_FIELDS)
    result = deepcopy(original) if partial else deepcopy(candidate)
    # Only old code-rendered fields are discarded; conflicting newly authored
    # fields remain and fail the normal renderer/audit.
    if partial:
        for key in RENDERED:
            result.pop(key, None)
        result.update(deepcopy(candidate))
    result.pop('financial_repair_version', None)
    merged = deepcopy(supplied)
    preserved = []
    prevented = []
    from app.v3.financial_reasoning import reasoning_catalog
    catalog = reasoning_catalog(record, original)
    for qid, answer in valid.items():
        matches = [a for a in supplied if isinstance(a, dict) and a.get('item_id') == qid]
        dependent = any(f.startswith(('plan_', 'calc_plan_'))
                        for step in answer['step_ids'] for f in catalog.get(step, {}).get('fact_ids', []))
        if dependent and plan_changed:
            if not matches:
                errors.append(f'research_answers.{qid}: changed plan requires explicit reselection.')
            continue
        if not matches:
            merged.append(answer)
            preserved.append(qid)
            if not partial:
                prevented.append(qid)
        elif len(matches) == 1:
            proposed = matches[0]
            ids = proposed.get('step_ids')
            if not isinstance(ids, list) or any(not isinstance(i, str) or i not in catalog for i in ids):
                errors.append(f'research_answers.{qid}: repair introduced invalid selections for a validated answer.')
                continue
            old_facts = {f for step in answer['step_ids'] for f in catalog[step]['fact_ids']}
            new_facts = {f for step in ids for f in catalog[step]['fact_ids']}
            if old_facts == new_facts:
                continue  # Catalog aliases denote exactly the same source evidence.
            if set(proposed) <= {'item_id', 'step_ids'}:
                # Validated unchanged questions are outside the repair scope.
                # Retain the ORIGINAL selection even if a full replacement adds
                # other true facts while dropping a required component. This is
                # recorded, not credited as newly model-authored evidence. Never
                # discard newly authored prose/claims to hide a conflict.
                merged[merged.index(proposed)] = answer
                preserved.append(qid)
                if qid not in validated_answers({**original, 'research_answers': [proposed]}, record):
                    prevented.append(qid)
            else:
                errors.append(f'research_answers.{qid}: repair changed a validated unrelated answer.')
    result['research_answers'] = merged
    result['_financial_repair_preservation'] = {'retained_answer_ids': preserved,
        'prevented_answer_regressions': prevented, 'source': 'original_model_selected_validated_answers'}
    return result, errors
