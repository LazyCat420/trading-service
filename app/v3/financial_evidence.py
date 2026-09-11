"""Dated source facts and deterministic calculations for Board decisions.

Inputs are captured baseline/portfolio snapshots, never analyst prose or memory.
A proposal is a scenario, an absent operand stays unknown, and matching this
record verifies consistency with supplied evidence, not the vendor's truth.
"""
from __future__ import annotations

from copy import deepcopy
from decimal import Decimal, InvalidOperation
import hashlib
import json

VERSION = 1
TECH_FIELDS = {'close': ('close', 'USD'), 'rsi': ('rsi_14', 'RSI_points'),
               'atr': ('atr_14', 'USD'), 'support': ('support', 'USD'),
               'resistance': ('resistance', 'USD'), 'sma_50': ('sma_50', 'USD'),
               'sma_200': ('sma_200', 'USD')}
FUND_FIELDS = {'oper_margin': ('operating_margin_pct', 'percent'),
               'gross_margin': ('gross_margin_pct', 'percent'),
               'profit_margin': ('net_margin_pct', 'percent'),
               'roic': ('roic_pct', 'percent'), 'roe': ('roe_pct', 'percent'),
               'roa': ('roa_pct', 'percent'), 'debt_to_equity': ('debt_to_equity', 'ratio'),
               'forward_pe': ('forward_pe', 'ratio'), 'pe_ratio': ('trailing_pe', 'ratio'),
               'peg_ratio': ('vendor_peg', 'ratio'),
               'revenue_growth': ('revenue_growth_pct', 'percent'),
               'eps_growth_qoq': ('eps_growth_qoq_pct', 'percent'),
               'dividend_yield': ('dividend_yield_pct', 'percent')}


def number(value):
    if isinstance(value, bool) or value is None:
        return None
    try:
        result = Decimal(str(value))
        return result if result.is_finite() else None
    except (ValueError, TypeError, InvalidOperation):
        return None


def fact(metric, value, unit, *, as_of=None, source, entity, period='current', reason=None):
    result = {'id': metric, 'metric': metric, 'value': value, 'unit': unit,
              'as_of': str(as_of) if as_of is not None else None,
              'source': source, 'entity': entity, 'period': period,
              'status': 'unknown' if value is None else 'known'}
    if reason:
        result['reason'] = reason
    return result


def record_hash(record):
    return hashlib.sha256(json.dumps(record, sort_keys=True, separators=(',', ':'),
                                     default=str).encode()).hexdigest()


def build_record(desk):
    """Build only from snapshots captured when their original brief was built."""
    meta = desk.cycle_metadata
    ticker = desk.ticker
    facts = []
    for key, mapping in (('financial_technical_snapshot', TECH_FIELDS),
                         ('financial_fundamental_snapshot', FUND_FIELDS)):
        snapshot = meta.get(key) or {}
        for field, (metric, unit) in mapping.items():
            if unit == 'USD':
                unit = snapshot.get('currency') or meta.get('quote_currency') or 'quote_currency'
            provenance = (snapshot.get('field_as_of') or {}).get(field) or snapshot
            value = number(snapshot.get(field))
            # Match the existing fundamental briefing's documented vendor
            # percent display convention. Do not apply it to ratio fields.
            if value is not None and unit == 'percent' and abs(value) <= 1:
                value *= 100
            source = ':'.join((key, ticker, str(provenance.get('as_of')),
                               str(provenance.get('source') or 'unspecified_vendor')))
            facts.append(fact(metric, float(value) if value is not None else None, unit,
                              as_of=provenance.get('as_of'), source=source, entity=ticker,
                              reason='Not present in the captured source snapshot.' if value is None else None))
    technical = meta.get('financial_technical_snapshot') or {}
    volume_source = (technical.get('field_as_of') or {}).get('volume_trend') or {}
    dates = volume_source.get('session_dates') or []
    volumes = [number(v) for v in volume_source.get('session_volumes', [])]
    trend = None
    if (len(dates) >= 5 and len(volumes) == len(dates) and len(set(dates)) == len(dates)
            and all(v is not None and v > 0 for v in volumes[:5])):
        # Source rows are newest first. A lower five-day average than the prior
        # fifteen does NOT establish declining volume across those five days.
        pairs = list(zip(volumes[:4], volumes[1:5]))
        trend = ('DECLINING' if all(a < b for a, b in pairs) else
                 'INCREASING' if all(a > b for a, b in pairs) else
                 'FLAT' if all(a == b for a, b in pairs) else 'MIXED')
    facts.append(fact('volume_five_session_trend', trend, 'category',
                      as_of=volume_source.get('as_of'),
                      source=str(volume_source.get('source') or 'price_history:unspecified_vendor')+':'+ticker+':'+str(volume_source.get('as_of')),
                      entity=ticker, period='latest_five_sessions',
                      reason=None if trend is not None else 'No complete dated five-session volume window.'))
    valuation = meta.get('financial_valuation_snapshot') or {}
    for key, metric in (('fcf_ttm', 'free_cash_flow'), ('revenue_ttm', 'revenue_ttm'),
                        ('ebit_ttm', 'operating_income_ttm')):
        value = number(valuation.get(key))
        facts.append(fact(metric, float(value) if value is not None else None,
                          valuation.get('currency') or 'reporting_currency',
                          as_of=valuation.get('ttm_as_of'),
                          source='financial_history:'+ticker+':'+str(valuation.get('ttm_as_of')),
                          entity=ticker, period='trailing_four_quarters',
                          reason='Complete quarterly source observations not available.' if value is None else None))
    position = meta.get('position') or {}
    if position.get('held') is True:
        value = number(position.get('avg_entry'))
        facts.append(fact('average_cost', float(value) if value is not None else None,
                          (meta.get('financial_technical_snapshot') or {}).get('currency') or meta.get('quote_currency') or 'quote_currency',
                          as_of=meta.get('timestamp'), source='position_snapshot:'+ticker,
                          entity=ticker, period='holding_cost'))
    book = meta.get('financial_book_snapshot') or {}
    for metric, unit in (('equity', 'USD'), ('cash', 'USD'), ('exposure_pct', 'percent')):
        value = number(book.get(metric)) if book.get('valuation_complete') is True else None
        facts.append(fact(metric, float(value) if value is not None else None, unit,
                          as_of=book.get('as_of'), source='portfolio_snapshot:'+ticker,
                          entity=ticker, reason='Portfolio valuation incomplete or unavailable.' if value is None else None))
    for metric in ('single_name_limit_pct', 'max_order_size_pct'):
        value = number(book.get(metric))
        facts.append(fact(metric, float(value) if value is not None else None, 'percent',
                          as_of=book.get('as_of'), source='parameter_store:'+metric, entity=ticker))
    # Pending orders are deliberately not assumed to be zero. The executor
    # rechecks actual order capacity. Until a reservation snapshot is provided,
    # final purchase headroom is unknown even with a complete held book.
    reservations = book.get('reservations') or {}
    pending = number(reservations.get('pending_exposure_pct'))
    complete = (reservations.get('complete') is True and reservations.get('ticker') == ticker
                and reservations.get('as_of') == book.get('as_of') and reservations.get('source')
                and pending is not None and pending >= 0)
    facts.append(fact('pending_exposure_pct', float(pending) if complete else None, 'percent',
                      as_of=reservations.get('as_of'), source=reservations.get('source') or 'missing:'+ticker,
                      entity=ticker, reason=None if complete else 'No complete matching pending-order reservation snapshot.'))
    reserved_cash = number(reservations.get('cash_reserved')) if complete else None
    facts.append(fact('pending_cash', float(reserved_cash) if reserved_cash is not None and reserved_cash >= 0 else None,
                      'USD', as_of=reservations.get('as_of'), source=reservations.get('source') or 'missing:'+ticker,
                      entity=ticker))
    # EPS QoQ and a vendor PEG are not next-year EPS growth. Do not reverse
    # engineer a growth rate from them to make forward PEG computable.
    facts.append(fact('eps_growth_next_year_pct', None, 'percent', source='missing:'+ticker,
                      entity=ticker, period='next_fiscal_year',
                      reason='No typed next-year EPS-growth observation is supplied.'))
    questions = []
    for q in meta.get('research_questions') or []:
        questions.append({'id': q.get('id'), 'question': q.get('question') or
                          (q.get('payload') or {}).get('question') or q.get('reason') or '',
                          'asked_at': str(q.get('created_at') or '')})
    return {'version': VERSION, 'ticker': ticker, 'as_of': meta.get('timestamp'),
            'facts': facts, 'questions': questions}


def calculated_facts(record, decision=None):
    """Fixed formulas with dimensional checks. No eval and no inferred operands."""
    facts = {f['id']: deepcopy(f) for f in record.get('facts', [])}
    if decision:
        price_unit = facts.get('close', {}).get('unit', 'quote_currency')
        for key, metric in (('stop_loss', 'plan_stop'), ('take_profit', 'plan_target')):
            value = number(decision.get(key))
            facts[metric] = fact(metric, float(value) if value is not None else None, price_unit,
                                 as_of=record.get('as_of'), source='decision_proposal',
                                 entity=record['ticker'], period='proposed')
        trigger = decision.get('dynamic_trigger')
        if isinstance(trigger, dict) and trigger.get('type') in ('price_below', 'price_above'):
            value = number(trigger.get('value'))
            facts['plan_entry'] = fact('plan_entry', float(value) if value is not None else None,
                                      price_unit, as_of=record.get('as_of'), source='decision_proposal',
                                      entity=record['ticker'], period='hypothetical')

    def calc(name, operands, units, unit, formula, fn, *, period='current', valid=None):
        inputs = [facts.get(k) for k in operands]
        values = [number(f.get('value')) if f and f.get('status') == 'known' else None for f in inputs]
        reason = None
        if any(v is None for v in values):
            reason = 'Missing operand: ' + ', '.join(k for k, v in zip(operands, values) if v is None)
        elif any(f['unit'] != u for f, u in zip(inputs, units)):
            reason = 'Operand units do not match formula.'
        elif valid and not valid(*values):
            reason = 'Formula domain invalid (including zero denominator or invalid price ordering).'
        value = None
        if reason is None:
            try:
                value = fn(*values)
                if isinstance(value, Decimal):
                    value = float(value)
            except (ArithmeticError, ValueError):
                reason = 'Undefined arithmetic.'
        result = fact(name, value, unit, as_of=record.get('as_of'), source='calculation:'+name,
                      entity=record['ticker'], period=period, reason=reason)
        result.update(input_ids=list(operands), formula=formula,
                      source_ids=sorted({s for f in inputs if f for s in f.get('source_ids', [f['source']])}))
        facts[name] = result

    quote_unit = facts.get('close', {}).get('unit', 'quote_currency')
    calc('calc_range_position_pct', ('close', 'support', 'resistance'), (quote_unit,)*3,
         'percent', '(close-support)/(resistance-support)*100', lambda p,s,r:(p-s)/(r-s)*100,
         valid=lambda p,s,r:r>s)
    calc('calc_price_vs_support', ('close', 'support'), (quote_unit,)*2, 'category',
         'compare(close,support)', lambda p,s:'above' if p>s else 'below' if p<s else 'at')
    calc('calc_holding_return_pct', ('close', 'average_cost'), (quote_unit,)*2, 'percent',
         '(close-average_cost)/average_cost*100', lambda p,c:(p-c)/c*100, valid=lambda p,c:c>0 and p>=0)
    calc('calc_forward_peg', ('forward_pe', 'eps_growth_next_year_pct'), ('ratio','percent'),
         'ratio', 'forward_pe/eps_growth_next_year_pct', lambda pe,g:pe/g, valid=lambda pe,g:pe>0 and g>0,
         period='next_fiscal_year')
    calc('calc_unreserved_headroom_pct', ('single_name_limit_pct','exposure_pct'),
         ('percent',)*2, 'percentage_points', 'max(0,limit-exposure)',
         lambda cap,held:max(Decimal(0),cap-held), valid=lambda cap,held:cap>=0 and held>=0)
    calc('calc_headroom_pct', ('single_name_limit_pct','exposure_pct','pending_exposure_pct'),
         ('percent',)*3, 'percentage_points', 'max(0,limit-exposure-pending)',
         lambda cap,held,pending:max(Decimal(0),cap-held-pending), valid=lambda cap,held,pending:cap>=0 and held>=0 and pending>=0)
    calc('calc_headroom_usd', ('calc_headroom_pct','equity'), ('percentage_points','USD'),
         'USD', 'headroom_pct/100*equity', lambda h,e:h/100*e, valid=lambda h,e:e>0)
    calc('calc_proposal_fits', ('proposed_purchase_pct','calc_headroom_pct'),
         ('percent','percentage_points'), 'boolean', 'proposed_purchase_pct<=headroom_pct',
         lambda p,h:p<=h, valid=lambda p,h:p>=0)
    for prefix, stop, target, entry in (('proposed', 'proposed_stop', 'proposed_target', 'proposed_entry'),
                                       ('plan', 'plan_stop', 'plan_target', 'plan_entry')):
        if prefix == 'plan' and not decision:
            continue
        for scenario, price in (('current','close'), ('conditional',entry)):
            calc(f'calc_{prefix}_{scenario}_reward_risk', (price,stop,target), (quote_unit,)*3,
                 'ratio', '(target-entry)/(entry-stop)', lambda p,s,t:(t-p)/(p-s),
                 valid=lambda p,s,t:Decimal(0)<s<p<t,
                 period='hypothetical' if scenario=='conditional' else 'current_reference_price')
    # Omit calculations with no operand at all. Retain partially known
    # calculations as explicit unknowns instead of quietly using a substitute.
    return {k:v for k,v in facts.items() if not k.startswith('calc_') or
            any(operand in facts for operand in v['input_ids'])}


FINANCIAL_OUTPUT_RULES = """
## REQUIRED STRUCTURED FINANCIAL DECISION
Return ONE flat JSON object. Required model-authored fields are financial_reasoning_version (always 2), action (BUY/HOLD/SELL), confidence, position_size_pct, entry_mode, trigger_purpose, dynamic_trigger, resolution_condition, reasoning_steps, research_answers.
reasoning_steps is a NONEMPTY concise list of IDs from SELECTABLE FINANCIAL REASONING STEPS. Select the relationships that explain YOUR decision. Read their statements: they already interpret the calculations accurately. Action, confidence and sizing remain your judgment, subject to the supplied risk limits.
research_answers is an ARRAY containing EVERY question in the FINANCIAL EVIDENCE CONTRACT record.questions exactly once. Debate topics, peer questions and data gaps are NOT research question IDs. When record.questions is empty, return research_answers: []. Otherwise use this form: {"item_id":"supplied question ID","step_ids":["selected catalog step ID"]}. Every question MUST contain at least one step ID in step_ids; NEVER leave step_ids empty []. Select all steps needed to cover every part of that question. Select the unavailable-observation step when history is absent. Do not substitute a true but unrelated observation.
Do NOT emit reasoning, financial_claims, question text, answer prose, fact_ids or answer status. Code renders those fields from your selected steps and the immutable source records. Do not write a second financial explanation in optional fields. In particular, never relabel a false proposal-fit result as sufficient capacity, compare different margin/return metrics, or treat hypothetical prices as executable quotes.
Preserve the ordinary decision timing contract. HOLD uses watch_only; SELL uses enter_now; a conditional BUY uses enter_on_condition with an entry trigger. If no trigger or research wake is needed, use trigger_purpose=none with null dynamic_trigger and resolution_condition. For trade_decision also supply attribution and signal_weights required by its decision contract.
"""


def correction_system_prompt(artifact_type):
    return ('You are reviewing an investment decision against immutable financial evidence. '
            'Correct the evidence selection and reconsider the action or size when required by the supplied facts. '
            'Return only complete '+artifact_type+' JSON. Select existing code-verified reasoning steps; '
            'do not write replacement financial prose. Respect the supplied risk and decision contracts. '
            + FINANCIAL_OUTPUT_RULES + '\nFor a structured original, a targeted financial_repair_version: 1 patch is also allowed; omit unchanged fields and validated answers. The harness merges and revalidates the complete decision.')


def question_selection_prompt(record):
    questions = record.get('questions') or []
    return ('\n## AUTHORITATIVE FINANCIAL RESEARCH QUESTION IDS\n'
            'The complete allowed item_id list for research_answers is: '
            + json.dumps([q['id'] for q in questions]) + '. '
            + ('Return research_answers: []. There are no research questions to answer. '
               if not questions else 'Answer each of these IDs exactly once using nonempty step_ids. ')
            + 'Do not promote debate-frame topics, peer objections or data gaps into item_id values. '
            'Those may inform reasoning_steps, but are not additional research questions.\n'
            + question_components_prompt(record))


def evidence_prompt(record):
    expanded = {**record, 'facts': list(calculated_facts(record).values())}
    return ('## FINANCIAL EVIDENCE CONTRACT v1\n'
            'Immutable source observations and code calculations follow. A missing value is UNKNOWN, never zero. '
            'Metric, unit, source, period and original date belong to each record. '
            'The selectable reasoning-step catalog interprets these records; choose its step IDs for the structured decision.\n'
            + json.dumps(expanded, separators=(',', ':'), default=str)
            + question_selection_prompt(record) + decision_budget_prompt(record))


def decision_budget(record):
    """Verified upper bound; incomplete reservations never become a zero balance."""
    facts = calculated_facts(record)
    limits = {}
    invalid_limits = []
    for key in ('calc_headroom_pct', 'calc_unreserved_headroom_pct', 'max_order_size_pct'):
        f = facts.get(key, {})
        value = number(f.get('value')) if f.get('status') == 'known' else None
        expected_unit = 'percent' if key == 'max_order_size_pct' else 'percentage_points'
        if f.get('status') == 'known' and (value is None or value < 0 or f.get('unit') != expected_unit):
            invalid_limits.append(key)
        elif value is not None:
            limits[key] = float(value)
    cash, equity = (number(facts.get(k, {}).get('value')) for k in ('cash', 'equity'))
    if (cash is not None and equity is not None and cash >= 0 and equity > 0
            and facts.get('cash', {}).get('unit') == facts.get('equity', {}).get('unit') == 'USD'):
        reserved = number(facts.get('pending_cash', {}).get('value'))
        limits['cash_capacity_pct'] = float(max(Decimal(0), cash - (reserved if reserved is not None and reserved >= 0 else 0)) / equity * 100)
    return {'unit': 'percent_of_portfolio_equity', 'limits': limits, 'invalid_limits': invalid_limits,
            'max_additional_purchase_pct': min(limits.values()) if limits else None,
            'reservation_status': 'known' if facts.get('calc_headroom_pct', {}).get('status') == 'known' else 'unknown',
            'authority': 'proposal_upper_bound_only; recheck current portfolio and reservations at execution',
            'record_sha256': record_hash(record)}


def decision_budget_prompt(record):
    return ('\n## CURRENT DECISION BUDGET\n' + json.dumps(decision_budget(record)) +
            '\nA BUY must have positive size no greater than every known limit. '
            'Reconsider a smaller BUY or HOLD when the proposed purchase exceeds capacity. '
            'A rejected scenario does not forbid a different, smaller purchase. '
            'Unknown reservations are not zero and do not authorize execution. '
            'Historical memory cannot change these limits. Size and action remain your decision.\n')


def question_components(record):
    from app.v3.financial_claims import _question_requirements
    result = []
    facts = calculated_facts(record)
    source_ids = {f['id'] for f in record.get('facts', [])}
    for q in record.get('questions', []):
        components = []
        for group in _question_requirements(q['question']):
            # Source-scenario questions remain about that scenario even after HOLD.
            supplied = {k for k in group if k.startswith('calc_proposed_')}
            if supplied and any(k in facts and
                                any(i in source_ids
                                    for i in facts[k].get('input_ids', []) if i != 'close')
                                for k in supplied):
                group = supplied
            label = next((k for k in sorted(group) if k in facts), sorted(group)[0])
            components.append({'id': label, 'fact_ids_any_of': sorted(group)})
        result.append({'item_id': q['id'], 'components': components})
    return result


def question_components_prompt(record):
    return ('\n## REQUIRED QUESTION COMPONENTS\n' + json.dumps(question_components(record)) +
            '\nCover EVERY component for each original question ID, including after HOLD. '
            'fact_ids_any_of contains actual selectable source IDs; choose those IDs or equivalent catalog steps. '
            'Select an explicit unknown observation when unavailable; empty selection is invalid.\n')
