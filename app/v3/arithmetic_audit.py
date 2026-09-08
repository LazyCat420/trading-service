"""Check explicit operand relationships, not financial truth or investment merit.

Only local, unambiguous transitions and current-vs-prior ratios are parsed.
No check is a coverage gap, not a passing grade. Original prose stays intact;
findings are attached by the harness and delivered separately to later agents.
"""
from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation

_NUMBER = r'[+-]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?'
_UNIT = r'(?:billion|million|trillion|bn|[bmk%])'


def _operand(name: str) -> str:
    return rf'(?<![\w.$,+-])\$?(?P<{name}>{_NUMBER})(?P<{name}_unit>\s*{_UNIT})?(?![\w.])'


_TRANSITION = re.compile(_operand('old') + r'\s*(?:to|→|->)\s*' + _operand('new'), re.I)
_PRIOR = re.compile(
    _operand('new') + r'\s+vs\.?\s+(?:(?:prev(?:ious)?|prior)\s+)' + _operand('old'), re.I)
_PRIOR_SUFFIX = re.compile(
    _operand('new') + r'\s+vs\.?\s+' + _operand('old') + r'\s+(?:prev(?:ious)?|prior)\b', re.I)
_AFTER = re.compile(r'^[^\d%+\-;\n]{0,45}(?P<claim>' + _NUMBER + r')\s*(?P<kind>%|x\b|fold\b)', re.I)
_BEFORE = re.compile(r'(?P<claim>' + _NUMBER + r')\s*(?P<kind>%|x\b|fold\b)(?:[^\d%;\n),]|(?:50|200)[ -]day){0,65}$', re.I)
_UNITS = {'b': 'billion', 'bn': 'billion', 'm': 'million', 'k': 'thousand'}
_ARTIFACTS = ('regime_classification', 'desk_note', 'fundamental_report', 'quant_report',
              'valuation_report', 'bull_argument', 'bear_rebuttal', 'bull_defense',
              'debate_judge', 'final_decision', 'trade_decision', 'delta_report')


def _decimal(value: str) -> Decimal:
    return Decimal(value.replace(',', ''))


def _unit(value: str | None) -> str:
    value = (value or '').strip().lower()
    return _UNITS.get(value, value)


def _rounding(value: str) -> Decimal:
    places = len(value.split('.')[1]) if '.' in value else 0
    return max(Decimal('0.005'), Decimal(1).scaleb(-places) / 2)


def _texts(value, path=''):
    if isinstance(value, str):
        yield path, value
    elif isinstance(value, dict):
        for key, child in value.items():
            if not key.startswith('_') and key not in {'data_gaps', 'tags', 'source_refs', 'raw_evidence'}:
                yield from _texts(child, f'{path}.{key}' if path else key)
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from _texts(child, f'{path}[{index}]')


# Require grammatical arithmetic links, not arbitrary prose between a pair and
# a percentage. Otherwise "EPS 0.27 → 2.65, revenue +46.6%" cross-wires metrics.
_BRIDGE_WORDS = frozenset(('a an the from is was genuine approximately about roughly '
    'up down increase increased decrease decreased decline declined drop dropped '
    'growth gain loss change of by yoy qoq run off 50 200 day').split())


def _arithmetic_bridge(text: str) -> bool:
    words = re.sub(r'[\s(,:—–-]+', ' ', text.lower()).strip().split()
    return all(word in _BRIDGE_WORDS for word in words)


def check_text(text: str) -> list[dict]:
    checks = []
    seen = set()
    for pattern in (_TRANSITION, _PRIOR, _PRIOR_SUFFIX):
        for match in pattern.finditer(text):
            if _unit(match['old_unit']) != _unit(match['new_unit']):
                continue  # mixed or implicit units are not a provable relationship
            before, after = text[max(0, match.start()-110):match.start()], text[match.end():match.end()+90]
            claim = _BEFORE.search(before) if pattern is _TRANSITION else None
            preceding = claim is not None
            if claim is None:
                claim = _AFTER.search(after)
                if claim and (')' in after[:claim.start('claim')] or re.search(r'\b(?:and|while|but|whereas|then)\b', after[:claim.start('claim')], re.I)):
                    claim = None
            if claim is None:
                continue
            bridge = before[claim.end('kind'):] if preceding else after[:claim.start('claim')]
            if not _arithmetic_bridge(bridge):
                continue
            raw = claim['claim']
            kind = 'percent_change' if claim['kind'] == '%' else 'value_multiple'
            if kind == 'percent_change' and _unit(match['old_unit']) == '%':
                continue  # rate levels / percentage points / chained confidence scores are ambiguous
            if pattern is not _TRANSITION and kind != 'value_multiple':
                continue  # vs does not specify a percentage-change denominator
            excerpt = (before[claim.start():] + match[0] if preceding else match[0] + after[:claim.end()])
            # A denied or explicitly corrected quotation is not a fresh assertion.
            adjacent = before[max(0, claim.start('claim')-16):] if preceding else after[:claim.start('claim')]
            suffix = after[claim.end():claim.end()+30] if not preceding else ''
            if re.search(r'\b(?:not|incorrect|wrong|undefined)\b', adjacent, re.I) or re.match(r'\s*(?:is|was)?\s*(?:wrong|incorrect|undefined)\b', suffix, re.I):
                continue
            old, new, claimed = _decimal(match['old']), _decimal(match['new']), _decimal(raw)
            if old < 0:
                continue  # growth on negative bases has competing conventions
            direction_text = (before[max(0, claim.start('claim')-24):] if preceding
                              else before[-24:] + after[:claim.start('claim')])
            directions = re.findall(r'\b(?:down|decline[ds]?|decreas\w*|drop\w*|fell|up|increas\w*|growth|rose)\b', direction_text, re.I)
            if kind == 'percent_change' and claimed > 0 and directions and re.match(r'down|declin|decreas|drop|fell', directions[-1], re.I):
                claimed = -claimed
            key = (match.start(), kind, str(claimed))
            if key in seen:
                continue
            seen.add(key)
            expected = None if old == 0 else (new / old if kind == 'value_multiple' else (new-old) / old * 100)
            status = 'undefined' if expected is None else ('consistent' if abs(expected-claimed) <= _rounding(raw) else 'mismatch')
            checks.append({'kind': kind, 'old': str(old), 'new': str(new), 'claimed': str(claimed),
                           'expected': str(expected) if expected is not None else None,
                           'status': status, 'excerpt': excerpt[:240]})
    return checks


def has_arithmetic_errors(*texts) -> bool:
    """A quote match cannot make an internally inconsistent calculation true."""
    return any(check['status'] != 'consistent' for text in texts if isinstance(text, str)
               for check in check_text(text))


def audit_artifact(artifact: dict) -> dict:
    """Attach reproducible checks; never alter a model's action, confidence or prose."""
    checks = []
    chars = 0
    truncated = False
    for path, text in _texts(artifact):
        if chars + len(text) > 100_000:
            truncated = True
            break
        chars += len(text)
        for finding in check_text(text):
            checks.append({'field': path, **finding})
    report = {'version': 1, 'scope': 'explicit_operand_arithmetic_only',
              'checked': len(checks), 'errors': sum(c['status'] != 'consistent' for c in checks),
              'scan_truncated': truncated, 'checks': checks[:100],
              'checks_omitted': max(0, len(checks)-100)}
    artifact['_arithmetic_audit'] = report
    return report


def arithmetic_handoff(desk, *, include_debate: bool = False) -> str:
    """Bounded corrections from completed artifacts. Safe for pre-decision injection."""
    lines = []
    seen = set()
    omitted = 0
    names = _ARTIFACTS if include_debate else _ARTIFACTS[:5]
    for name in names:
        artifact = getattr(desk, name, None)
        if not isinstance(artifact, dict):
            continue
        for check in (artifact.get('_arithmetic_audit') or {}).get('checks', []):
            if check['status'] == 'consistent':
                continue
            key = (check['kind'], check['old'], check['new'], check['claimed'])
            if key in seen:
                continue
            seen.add(key)
            result = ('undefined because the starting value is zero' if check['expected'] is None
                      else f"{Decimal(check['expected']):.4f}" + ('%' if check['kind'] == 'percent_change' else 'x'))
            line = (f"- {name}.{check['field']}: {check['old']} → {check['new']}; "
                    f"claimed {check['claimed']} ({check['kind']}), computed {result}.")
            if sum(len(x)+1 for x in lines) + len(line) > 2000:
                omitted += 1
            else:
                lines.append(line)
    if not lines:
        return ''
    return ('## ARITHMETIC CORRECTIONS FROM PRIOR ARTIFACTS\n'
            'These check the agents’ stated operands only; they do not verify source facts. '
            'Do not repeat the erroneous relationship or label it verified. Reassess any conclusion that relies on it.\n'
            + '\n'.join(lines) + (f'\n{omitted} additional distinct errors omitted; consult the artifact audit.' if omitted else ''))


def board_plan_math(desk) -> str:
    """Math at the supplied reference close, never an invented executable entry quote."""
    board = getattr(desk, 'final_decision', None) or {}
    if board.get('action') != 'BUY' or board.get('entry_mode') != 'enter_now':
        return ''
    baseline = desk.cycle_metadata.get('technical_baseline_context', '')
    match = re.search(r'^\s*- close:\s*(' + _NUMBER + r')\b', baseline, re.M)
    if not match:
        return ''
    try:
        entry = _decimal(match[1])
        stop, target = Decimal(str(board.get('stop_loss'))), Decimal(str(board.get('take_profit')))
        if not all(x.is_finite() for x in (entry, stop, target)) or not Decimal(0) < stop < entry < target:
            return ''
    except (InvalidOperation, TypeError, ValueError):
        return ''
    risk, reward = entry-stop, target-entry
    return ('## CURRENT BOARD PLAN ARITHMETIC\n'
            f'At the supplied technical-baseline reference close {entry} (not a live execution quote), '
            f'Board stop {stop} and target {target} imply risk {risk}, reward {reward}, '
            f'reward/risk {reward/risk:.4f}:1. '
            'Older ratios computed with a different stop or entry do not describe this plan. '
            'This checks arithmetic only, not the probability of either outcome. Recompute if the plan or entry changes.')
