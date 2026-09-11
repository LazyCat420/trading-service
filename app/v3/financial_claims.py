"""Validate declared financial claims against a captured fact record.

This is an evidence-consistency contract, not a proof of investment merit.
Numerical assertions belong in typed claims, where metric/date/unit/source can
all be checked. Prose remains qualitative; unsupported prose is never silently
counted as a checked numerical claim.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime
import json
import re
from decimal import Decimal

from app.v3.financial_evidence import calculated_facts, number, record_hash

REFERENCE = re.compile(r'\[([A-Za-z][A-Za-z0-9_:.\-]*)\]')
CLAIM_FIELDS = {'fact_id', 'metric', 'value', 'unit', 'as_of', 'source'}
_MONTHS = r'January|February|March|April|May|June|July|August|September|October|November|December'


def _dates(text):
    result = set(re.findall(r'\b\d{4}-\d{2}-\d{2}\b', text or ''))
    for date in re.findall(r'\b(?:'+_MONTHS+r')\s+\d{1,2},?\s+\d{4}\b', text or '', re.I):
        try:
            result.add(datetime.strptime(date.replace(',', ''), '%B %d %Y').date().isoformat())
        except ValueError:
            pass
    return result


def _supports_question_date(source, question):
    dates = _dates(question)
    month_names = [name.casefold() for name in _MONTHS.split('|')]
    months = {month_names.index(m.casefold())+1 for m in re.findall(r'\b(?:'+_MONTHS+r')\b', question, re.I)}
    filing_month = bool(months and 'filing' in question.casefold())
    if not dates and not filing_month:
        return True
    as_of = str(source.get('as_of') or '')[:10]
    try:
        observed = datetime.strptime(as_of, '%Y-%m-%d').date()
    except ValueError:
        return False
    if dates and (as_of > max(dates) or ('volume' in question.casefold() and as_of not in dates)):
        return False
    return not filing_month or observed.month in months


def _same_value(value, expected):
    if isinstance(expected, bool) or expected is None or isinstance(expected, str):
        return type(value) is type(expected) and value == expected
    if not isinstance(value, (int, float, Decimal)) or isinstance(value, bool):
        return False
    actual, target = number(value), number(expected)
    return actual is not None and target is not None and abs(actual-target) <= Decimal('0.005')


def _family(metric):
    return re.sub(r'^(?:prior_|sector_)', '', metric).removesuffix('_prior')


def _question_requirements(question):
    """Metric families implied by explicit financial questions, not golden answers."""
    text = question.casefold()
    groups = []
    if 'reward/risk' in text or 'reward-to-risk' in text:
        scenario = 'conditional' if re.search(r'future|proposed|would|hypothetical', text) else 'current'
        groups.append({f'calc_proposed_{scenario}_reward_risk', f'calc_plan_{scenario}_reward_risk'})
    if 'range' in text and ('position' in text or 'close' in text):
        groups.append({'calc_range_position_pct'})
    if 'peg' in text:
        groups.append({'calc_forward_peg'})
    if 'unrealized' in text and ('return' in text or 'percentage' in text):
        groups.append({'calc_holding_return_pct'})
    if 'operating margin' in text:
        groups.append({'operating_margin_pct'})
    if 'free cash flow' in text:
        groups.append({'free_cash_flow'})
    if ('exposure' in text and ('additional' in text or 'fit' in text)) or 'headroom' in text:
        groups.append({'calc_headroom_pct', 'calc_headroom_usd'})
    if 'fit' in text and ('purchase' in text or 'proposal' in text or 'proposed' in text):
        groups.append({'calc_proposal_fits'})
    if 'independent' in text and ('source' in text or 'report' in text):
        groups.append({'underlying_filing_count'})
    if 'volume' in text and ('five' in text or '5' in text):
        groups.append({'volume_five_session_trend'})
    if 'rsi' in text and ('support' in text or 'price' in text):
        groups.extend([{'support'}, {'rsi_14'}])
    if '200' in text and ('moving average' in text or 'sma' in text):
        groups.append({'sma_200'})
    return groups


def materialize_claim_references(decision, record):
    """Resolve authored fact IDs, never replace an authored metric or value.

    Explicit claim objects remain untouched and undergo the full coordinate
    audit. Unknown IDs remain invalid. The raw response is retained by tracing.
    """
    if not isinstance(decision, dict):
        return decision
    result = deepcopy(decision)
    claims = result.get('financial_claims')
    if not isinstance(claims, list):
        return result
    catalog = calculated_facts(record, result)
    expanded = []
    refs = []
    for item in claims:
        if isinstance(item, str) and item in catalog:
            fact = catalog[item]
            expanded.append({'fact_id':item, **{k:fact[k] for k in ('metric','value','unit','as_of','source')}})
            refs.append(item)
        else:
            expanded.append(item)
    result['financial_claims'] = expanded
    if refs:
        result['_financial_claim_reference_ids'] = refs
    return result


def audit_decision(decision, record):
    errors = []
    checked = []
    def error(kind, field, message):
        errors.append({'kind': kind, 'field': field, 'message': message})
    facts = record.get('facts')
    record_errors = []
    if not isinstance(facts, list) or not isinstance(record.get('ticker'), str):
        record_errors.append('The record requires a ticker and a fact array.')
        facts = []
    seen = set()
    for f in facts:
        if not isinstance(f, dict) or not isinstance(f.get('id'), str):
            record_errors.append('Malformed source fact.')
            continue
        if f['id'] in seen or not all(k in f for k in ('metric','value','unit','as_of','source','status')):
            record_errors.append('Duplicate or incomplete source fact: '+f['id'])
        if any(not isinstance(f.get(k), str) or not f[k] for k in ('metric','unit','source')):
            record_errors.append('Missing metric, unit or source: '+f['id'])
        if f.get('as_of') is not None and not isinstance(f['as_of'], str):
            record_errors.append('Invalid source date: '+f['id'])
        value=f.get('value')
        if not (value is None or isinstance(value,(str,bool)) or number(value) is not None):
            record_errors.append('Invalid source value: '+f['id'])
        if f.get('status') not in ('known','unknown') or (f.get('status')=='unknown') != (value is None):
            record_errors.append('Value and availability disagree: '+f['id'])
        seen.add(f['id'])
    if record_errors:
        return {'version':1,'status':'unresolved','record_sha256':record_hash(record),
                'checked_claims':0,'errors':[{'kind':'invalid_record','field':'record','message':m} for m in record_errors]}
    if decision.get('financial_reasoning_version') == 2:
        from app.v3.financial_reasoning import render_reasoning_artifact
        decision, rendering_errors = render_reasoning_artifact(decision, record)
        for message in rendering_errors:
            error('structured_reasoning', 'reasoning_steps', message)
    catalog = calculated_facts(record, decision)
    decision = materialize_claim_references(decision, record)
    claims = decision.get('financial_claims')
    if not isinstance(claims, list):
        error('missing_claims', 'financial_claims', 'Return financial_claims with exact metric, value, unit, date and source fields.')
        claims = []
    if len(claims) > 80:
        error('claim_limit', 'financial_claims', 'At most eighty claims are supported.')
    declared = {}
    for i, claim in enumerate(claims[:80]):
        path = f'financial_claims[{i}]'
        if not isinstance(claim, dict) or set(claim) != CLAIM_FIELDS:
            error('claim_shape', path, 'Use exactly fact_id, metric, value, unit, as_of and source.')
            continue
        key = claim.get('fact_id')
        if not isinstance(key, str) or key not in catalog:
            error('unknown_fact', path, 'fact_id must refer to a supplied record or code-computed plan calculation.')
            continue
        if key in declared:
            error('duplicate_claim', path, 'Each fact_id must appear only once.')
        declared[key] = claim
        source = catalog[key]
        for field in ('metric', 'unit', 'as_of', 'source'):
            if claim.get(field) != source.get(field):
                error('fact_'+field, path+'.'+field, f'{key}: expected {source.get(field)!r}.')
        if not _same_value(claim.get('value'), source.get('value')):
            error('fact_value', path+'.value', f'{key}: source/computed value is {source.get("value")!r}; '+source.get('reason',''))
        checked.append(key)

    comparisons = {}
    raw_comparisons = decision.get('financial_comparisons', [])
    if not isinstance(raw_comparisons, list):
        error('comparison_shape', 'financial_comparisons', 'financial_comparisons must be an array.')
        raw_comparisons = []
    if len(raw_comparisons) > 80:
        error('comparison_limit', 'financial_comparisons', 'Too many comparisons.')
    for i, comparison in enumerate(raw_comparisons[:80]):
        path = f'financial_comparisons[{i}]'
        if not isinstance(comparison, dict) or set(comparison) != {'id','left_id','right_id','relation'}:
            error('comparison_shape', path, 'Use id, left_id, right_id, relation (above/below/equal).')
            continue
        key = comparison.get('id')
        left_id, right_id = comparison.get('left_id'), comparison.get('right_id')
        left = catalog.get(left_id) if isinstance(left_id, str) else None
        right = catalog.get(right_id) if isinstance(right_id, str) else None
        if not isinstance(key, str) or key in declared or key in comparisons:
            error('comparison_id', path, 'Use a unique comparison id.')
            continue
        comparisons[key] = comparison
        if not left or not right or any(comparison.get(k) not in declared for k in ('left_id','right_id')):
            error('comparison_source', path, 'Comparison operands must be declared source facts.')
            continue
        price_metrics = {'close','support','resistance','sma_50','sma_200','proposed_entry','proposed_stop','proposed_target','plan_entry','plan_stop','plan_target'}
        like_prices = left['metric'] in price_metrics and right['metric'] in price_metrics
        if left['unit'] != right['unit'] or (_family(left['metric']) != _family(right['metric']) and not like_prices):
            error('comparison_metric', path, 'Different financial metrics are not like-for-like comparisons.')
            continue
        a,b=number(left['value']),number(right['value'])
        if a is None or b is None:
            error('comparison_unknown', path, 'Cannot compare unavailable or categorical operands.')
        elif comparison['relation'] != ('above' if a>b else 'below' if a<b else 'equal'):
            error('comparison_value', path, 'The stated relation disagrees with the source operands.')

    prose = [(key, decision.get(key)) for key in ('reasoning','rationale','mispricing_basis','override_reason','override_justification','timing_override_reason','dissent_resolution')]
    if isinstance(decision.get('bear_verdict_response'), dict):
        prose.append(('bear_verdict_response.decisive_claim', decision['bear_verdict_response'].get('decisive_claim')))
    answers = decision.get('research_answers')
    if not isinstance(answers, list):
        answers=[]
    answer_ids = []
    requested = {q['id']:q for q in record.get('questions', [])}
    for i, answer in enumerate(answers):
        path = f'research_answers[{i}]'
        if not isinstance(answer, dict):
            error('answer_shape', path, 'A research answer must be an object.')
            continue
        qid = answer.get('item_id')
        answer_ids.append(qid)
        q = requested.get(qid) if isinstance(qid, str) else None
        if q is None:
            error('unknown_question', path, 'Answer only the supplied exact question ids.')
            continue
        if answer.get('question') != q['question']:
            error('question_changed', path+'.question', 'Copy the original question exactly, including its historical date.')
        state = answer.get('status')
        if state not in ('answered','unresolved'):
            error('answer_status', path, 'Use answered or unresolved.')
        if not isinstance(answer.get('answer'), str) or len(answer['answer'].strip())<12:
            error('answer_missing', path, 'Explain the answer or the evidence gap.')
        refs=answer.get('fact_ids')
        if not isinstance(refs, list) or any(not isinstance(r,str) or r not in declared for r in refs):
            error('answer_sources', path, 'fact_ids must reference declared financial_claims.')
            refs=[]
        if state=='answered':
            if not refs or any(catalog[r]['status']!='known' for r in refs if r in catalog):
                error('answer_unknown', path, 'An answered question needs known supporting facts; missing observations remain unresolved.')
            if any(not _supports_question_date(catalog[r], q['question']) for r in refs if r in catalog):
                error('historical_evidence', path, 'Source dates do not support the requested historical date or filing month.')
        requirements = _question_requirements(q['question'])
        # A question about supplied scenario levels cannot be answered with a
        # newly chosen stop/target from the decision instead.
        requirements = [({k for k in group if k.startswith('calc_proposed_')} if
                         any(k.startswith('calc_proposed_') and k in catalog and
                             any(i in catalog for i in catalog[k].get('input_ids', []) if i != 'close')
                             for k in group) else group) for group in requirements]
        known_groups = [group for group in requirements
                        if any(k in catalog and catalog[k]['status']=='known' and
                               _supports_question_date(catalog[k], q['question']) for k in group)]
        if state == 'answered':
            for group in requirements:
                if not (group & set(refs)):
                    error('answer_relevance', path, 'Cite the metric/calculation actually requested: '+', '.join(sorted(group)))
        elif state == 'unresolved' and requirements and len(known_groups)==len(requirements):
            error('unnecessary_abstention', path, 'The requested metric/calculation is supplied; answer it rather than abstaining.')
        prose.append((path+'.answer',answer.get('answer')))
    for qid in requested:
        if answer_ids.count(qid) != 1:
            error('question_coverage', 'research_answers', f'Answer {qid} exactly once, including an explicit unresolved answer when needed.')

    resolution = decision.get('resolution_condition')
    if isinstance(resolution, dict):
        question_text = str(resolution.get('open_question') or '')
        for q in requested.values():
            dates = _dates(q['question'])
            resolution_dates = _dates(question_text)
            same_subject = any(word in q['question'].casefold() and word in question_text.casefold()
                               for word in ('volume','peg','moving average','sma','earnings'))
            if dates and resolution_dates and same_subject and dates != resolution_dates:
                error('resolution_date_changed','resolution_condition','An unresolved question changed its requested historical date.')
    cited = set()
    for path,text in prose:
        if not isinstance(text,str):
            continue
        refs=REFERENCE.findall(text)
        cited.update(refs)
        for ref in refs:
            if ref not in declared and ref not in comparisons:
                error('prose_reference',path,f'[{ref}] must cite a declared claim or comparison.')
        clean=REFERENCE.sub('',text)
        if re.search(r'\d', clean):
            error('unstructured_number',path,'Put quantitative assertions in financial_claims and cite [fact_id]; explain implications qualitatively in prose.')
        # Scope negation to the sentence containing the assertion. An unrelated
        # "not compelling" must not disable checks for the whole rationale.
        for sentence in re.split(r'(?<=[.;!?])\s+|\n', text):
            clean_sentence = REFERENCE.sub('', sentence)
            fits = catalog.get('calc_proposal_fits', {}).get('value')
            if fits is False and re.search(r'\b(?:proposed|proposal|purchase|addition)\b', clean_sentence, re.I):
                if (re.search(r'\b(?:purchase|proposal|addition)\s+(?:\[[^]]+\]\s*)?fits\b', sentence, re.I)
                        or re.search(r'\bdoes not (?:breach|exceed)\b[^.;]{0,30}(?:limit|cap)', clean_sentence, re.I)):
                    error('proposal_fit_relation',path,'The supplied proposal does not fit: calc_proposal_fits is false; the addition exceeds available headroom.')
            if re.search(r'\b(?:not|incorrect|wrong|rather than|never)\b',clean_sentence,re.I):
                continue
            relation = catalog.get('calc_price_vs_support',{}).get('value')
            m=re.search(r'\b(?:price|close|stock)\b[^.;\n]{0,45}\b(above|below)\s+(?:its\s+|the\s+)?support\b',clean_sentence,re.I)
            if m and relation and m[1].lower()!=relation:
                error('price_support_relation',path,f'Code comparison places close {relation} support.')
            for pattern, metric in ((r'operating margin','operating_margin_pct'),
                                     (r'gross margin','gross_margin_pct'),
                                     (r'free cash flow|fcf','free_cash_flow'),
                                     (r'roic','roic_pct')):
                assertion=re.search(r'\b(?:'+pattern+r')\b\s*(?:is|are|remains?|was|turned|currently|now|:)?\s*(positive|negative)\b',clean_sentence,re.I)
                value=number(catalog.get(metric,{}).get('value'))
                if assertion and value is not None and ((assertion[1].lower()=='positive' and value<=0) or (assertion[1].lower()=='negative' and value>=0)):
                    error('metric_sign',path,metric+' has the opposite sign in the supplied evidence.')
            range_position=number(catalog.get('calc_range_position_pct',{}).get('value'))
            if range_position is not None and re.search(r'range|support|resistance',sentence,re.I):
                wrong_range = (bool(re.search(r'midpoint|halfway',sentence,re.I)) and abs(range_position-50)>1)
                wrong_range |= (bool(re.search(r'one[ -]third|a third',sentence,re.I)) and abs(range_position-Decimal(100)/3)>1)
                wrong_range |= (bool(re.search(r'lower third',sentence,re.I)) and range_position>Decimal(100)/3)
                wrong_range |= (bool(re.search(r'upper third',sentence,re.I)) and range_position<Decimal(200)/3)
                if wrong_range:
                    error('range_position_relation',path,f'The stated range location disagrees with calc_range_position_pct={range_position}; the midpoint is fifty percent.')
            guidance=catalog.get('guidance_record',{}).get('value')
            if guidance=='met' and re.search(r'\b(?:beat\w*[^.;\n]{0,25}guidance|guidance\s+beat\w*)\b',clean_sentence,re.I):
                error('guidance_overstatement',path,'The source says guidance was met, not beaten.')
            for comparison in re.finditer(REFERENCE.pattern+r'\s+(?:exceeds?|is above|is below|is less than|is greater than)\b[^.;\n\[\]]{0,45}'+REFERENCE.pattern,sentence,re.I):
                left,right=(catalog.get(key) for key in comparison.groups())
                if left and right and (_family(left['metric']) != _family(right['metric'])):
                    error('prose_comparison_metric',path,f"{left['metric']} and {right['metric']} measure different things; this comparison is unsupported.")
    if not checked:
        error('no_evidence', 'financial_claims', 'Declare the facts that support the decision; zero checked claims is not a pass.')
    if checked and not cited and not answers:
        error('unconnected_evidence','reasoning','Connect the decision rationale to its claims using [fact_id].')
    # Recheck sizing against a known total-exposure budget. This is a finding,
    # not a rewritten size, and it applies to proposed conditional BUYs too.
    if str(decision.get('action','')).upper() in ('BUY','SELL') and not any(catalog[k]['status']=='known' for k in checked):
        error('no_supported_basis','financial_claims','An executable action cannot rely exclusively on unavailable observations.')
    if str(decision.get('action','')).upper()=='BUY':
        size=number(decision.get('position_size_pct'))
        for key in ('calc_headroom_pct','calc_unreserved_headroom_pct','max_order_size_pct'):
            limit=number(catalog.get(key,{}).get('value'))
            if size is not None and limit is not None and size>limit+Decimal('0.000001'):
                error('size_exceeds_capacity','position_size_pct',f'Proposed size exceeds {key}={limit}.')
    return {'version':1,'status':'unresolved' if errors else 'consistent',
            'record_sha256':record_hash(record),'checked_claims':len(checked),
            'checked_fact_ids':checked,'questions_required':len(requested),
            'questions_returned':len(answers),'errors':errors,
            'scope':'typed_source_facts_calculations_and_question_coverage',
            'limitations':'Consistency with supplied evidence; not source truth, complete semantic entailment, or investment merit.'}


def correction_prompt(user_prompt, artifact, audit, record):
    proposal = {k:artifact[k] for k in ('action','confidence','position_size_pct','stop_loss','take_profit','entry_mode','trigger_purpose','dynamic_trigger') if k in artifact}
    catalog_hint = ""
    question_hint = ""
    if record:
        from app.v3.financial_reasoning import reasoning_catalog
        from app.v3.financial_evidence import question_selection_prompt
        question_hint = question_selection_prompt(record)
        catalog_ids = sorted(reasoning_catalog(record).keys())
        catalog_hint = (
            '\nSelect ONLY from these valid catalog step IDs:\n' + json.dumps(catalog_ids) +
            "\nEvery question in research_answers MUST have a nonempty step_ids array. Never emit 'step_ids': []."
        )
    return (user_prompt+'\n\n## FINANCIAL EVIDENCE RECONSIDERATION\n'
            'Your previous decision was rejected. Write a fresh complete decision using the source records. '
            'The prior proposal is provided only to let you reconsider its action and price plan:\n'+json.dumps(proposal,default=str)+
            '\nCorrect ALL of these evidence/coverage errors:\n'+json.dumps(audit['errors'])+
            catalog_hint+question_hint+
            '\nReconsider the investment conclusion from the supplied facts. You may change action, confidence, '
            'size or plan when the corrected facts justify it. Do not merely relabel the error as verified. '
            'Preserve the original questions and distinguish unknown history from current observations. '
            'Return financial_reasoning_version=2 with reasoning_steps and research_answers containing item_id and step_ids, plus the complete decision and timing fields. '
            'Select steps from the supplied catalog; code renders the explanation and numerical records. '
            'Do not emit financial_claims or authored reasoning/answer prose. The harness will revalidate every selection. '
            'If evidence is insufficient, state the unresolved question; do not invent a value. '
            'Use only records in the supplied FINANCIAL EVIDENCE CONTRACT. '+
            '\nCode-calculated values for your previous plan (not executable quotes):\n'+
            json.dumps([f for k,f in calculated_facts(record,artifact).items() if k.startswith('calc_plan_')],default=str))


def desk_status(desk):
    """Recompute at the policy boundary; a model-supplied audit never authorizes."""
    meta=desk.cycle_metadata
    if meta.get('financial_evidence_version')!=1:
        return {'version':0,'status':'legacy'}
    record=meta.get('financial_evidence_record')
    if not isinstance(record,dict):
        return {'version':1,'status':'unresolved','errors':[{'kind':'missing_record','message':'No captured financial fact record.'}]}
    decision={**(desk.final_decision or {}),**(desk.trade_decision or {})}
    return audit_decision(decision,record)


def execution_errors(result):
    """Recheck persisted evidence and the flattened order fields at dispatch."""
    if result.get('financial_evidence_version')!=1:
        return []
    record=result.get('financial_evidence_record')
    decision=result.get('financial_decision')
    if not isinstance(record,dict) or not isinstance(decision,dict):
        return ['Versioned financial decision is missing its evidence or authored decision.']
    audit=audit_decision(decision,record)
    errors=[e['message'] for e in audit['errors']]
    if result.get('action')!=decision.get('action'):
        errors.append('Order action differs from the financially checked decision.')
    estimate=result.get('estimate') or {}
    for key in ('position_size_pct','stop_loss','take_profit','dynamic_trigger','entry_mode','trigger_purpose'):
        if estimate.get(key)!=decision.get(key):
            errors.append('Order '+key+' differs from the financially checked decision.')
    return errors
