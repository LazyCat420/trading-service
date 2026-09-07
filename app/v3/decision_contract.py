"""Decision provenance and execution intent shared by producer and consumers.

Version 1 is stamped on newly created desks. Legacy saved decisions remain
readable, but an untyped new BUY cannot silently become an immediate order.
Validation verifies references and explicit changes, not the economic truth of
a model's cited evidence. The model retains authority to make a justified override.
"""
from __future__ import annotations

import hashlib
import json
import math
from typing import Any

VERSION = 1
ENTRY_MODES = frozenset({'enter_now', 'enter_on_condition', 'watch_only'})
TRIGGER_PURPOSES = frozenset({'none', 'entry', 'monitor', 'research'})
DECISION_FIELDS = ('action', 'position_size_pct', 'stop_loss', 'take_profit',
                   'exit_style', 'dynamic_trigger', 'entry_mode', 'trigger_purpose')
TIMING_FIELDS = ('entry_mode', 'trigger_purpose', 'dynamic_trigger')
REFERENCE_FIELDS = DECISION_FIELDS + ('confidence', 'resolution_condition')


def board_reference(board: dict) -> str:
    payload = {key: board.get(key) for key in REFERENCE_FIELDS}
    encoded = json.dumps(payload, sort_keys=True, separators=(',', ':'), default=str)
    return 'board:' + hashlib.sha256(encoded.encode()).hexdigest()[:24]


def effective_decision(decision: dict, board: dict | None = None) -> dict:
    """Absent fields inherit; explicit null/zero is an intentional value."""
    return {**(board or {}), **(decision or {})}


def _text(value: Any, minimum: int = 20) -> bool:
    return isinstance(value, str) and len(value.strip()) >= minimum


def entry_errors(decision: dict) -> list[str]:
    errors = []
    mode, purpose = decision.get('entry_mode'), decision.get('trigger_purpose')
    action = str(decision.get('action') or '').upper()
    trigger = decision.get('dynamic_trigger')
    if mode not in ENTRY_MODES:
        errors.append('entry_mode must be enter_now, enter_on_condition, or watch_only')
    if purpose not in TRIGGER_PURPOSES:
        errors.append('trigger_purpose must be none, entry, monitor, or research')
    if mode == 'enter_on_condition' and (action != 'BUY' or purpose != 'entry'):
        errors.append('enter_on_condition requires BUY and trigger_purpose=entry')
    if action == 'SELL' and mode != 'enter_now':
        errors.append('SELL requires entry_mode=enter_now')
    if action == 'HOLD' and mode != 'watch_only':
        errors.append('HOLD requires entry_mode=watch_only')
    if action == 'BUY' and mode == 'enter_now' and purpose == 'entry':
        errors.append('an unmet entry trigger requires enter_on_condition, not enter_now')
    if purpose == 'entry' and mode != 'enter_on_condition':
        errors.append('trigger_purpose=entry requires enter_on_condition')
    if purpose == 'none' and trigger:
        errors.append('a dynamic_trigger needs an explicit purpose')
    if purpose in ('entry', 'monitor', 'research'):
        if not isinstance(trigger, dict):
            errors.append('trigger purpose requires a structured dynamic_trigger')
        else:
            from app.trading.order_triggers import dynamic_trigger_is_evaluable
            typ, value = trigger.get('type'), trigger.get('value')
            if not isinstance(typ, str) or not dynamic_trigger_is_evaluable(typ):
                errors.append('dynamic_trigger.type must be evaluable by the monitor')
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
                errors.append('dynamic_trigger.value must be a positive finite number')
            elif typ == 'trailing_drop' and value >= 1:
                errors.append('trailing_drop.value must be a fraction between 0 and 1')
    return errors


def contract_errors(decision: dict, *, board: dict | None = None,
                    evidence_sources: set[str] | None = None) -> list[str]:
    resolved = effective_decision(decision, board)
    errors = entry_errors(resolved)
    if board is None:
        return errors
    if decision.get('source_board_ref') != board_reference(board):
        errors.append('source_board_ref must match the current Board artifact')
    if decision.get('source_board_action') != board.get('action'):
        errors.append('source_board_action must match the current Board action')
    relation = decision.get('decision_relation')
    if relation not in ('preserve', 'override'):
        errors.append('decision_relation must be preserve or override')
    changes = [key for key in DECISION_FIELDS if resolved.get(key) != board.get(key)]
    if changes and relation != 'override':
        errors.append('changed fields require decision_relation=override: ' + ', '.join(changes))
    if relation == 'override':
        if not _text(decision.get('override_reason')):
            errors.append('override_reason must explain the change')
        evidence = decision.get('override_evidence')
        valid = isinstance(evidence, list) and bool(evidence)
        for item in evidence if isinstance(evidence, list) else []:
            valid = valid and isinstance(item, dict) and _text(item.get('claim'))
            valid = valid and isinstance(item.get('source') if isinstance(item, dict) else None, str)
            if isinstance(item, dict) and evidence_sources is not None:
                valid = valid and item.get('source') in evidence_sources
        if not valid:
            errors.append('override_evidence needs claims citing a delivered evidence source')
        if any(key in changes for key in TIMING_FIELDS) and not _text(decision.get('timing_override_reason')):
            errors.append('timing_override_reason is required when timing or trigger purpose changes')
    return errors


def evidence_sources(desk) -> set[str]:
    """Only sources actually on this desk, never a fabricated catalog label."""
    names = ('desk_note', 'fundamental_report', 'quant_report', 'valuation_report',
             'bull_argument', 'bear_rebuttal', 'bull_defense', 'debate_judge',
             'regime_classification')
    out = {name for name in names if getattr(desk, name, None)}
    out.update(key for key, value in desk.cycle_metadata.items()
               if value and (key == 'data_report' or key.endswith('_context')))
    return out


def prompt_block(desk, artifact_type: str) -> str:
    """The same typed source used by validation, delivered outside prose cuts."""
    if desk.cycle_metadata.get('decision_contract_version') != VERSION:
        return ''
    if artifact_type not in ('final_decision', 'trade_decision'):
        return ''
    lines = [
        '## DECISION CONTRACT v1 (required)',
        'Declare entry_mode: enter_now | enter_on_condition | watch_only.',
        'Declare trigger_purpose: none | entry | monitor | research.',
        'BUY enter_now executes now. BUY enter_on_condition only arms a re-analysis wake; '
        'it does not buy now. When a fired condition has been verified and entry is now '
        'appropriate, explicitly choose enter_now. A post-entry monitor is not an entry condition.',
        'HOLD uses watch_only. SELL exits now and uses enter_now. A dynamic trigger with '
        'purpose entry/monitor/research needs its structured type and positive value.',
        'State resolution_condition when there is an unresolved question worth watching: '
        '{"open_question":"...", "resolving_fact":"the observable fact that settles it"}. '
        'Use null if no unresolved question remains; do not invent one for the schema.',
    ]
    if artifact_type == 'trade_decision' and desk.final_decision:
        board = desk.final_decision
        lines += [
            'CURRENT BOARD SOURCE (this cycle; source of truth for attribution):',
            json.dumps({'source_board_ref': board_reference(board),
                        **{key: board.get(key) for key in REFERENCE_FIELDS}}, sort_keys=True, default=str),
            'Return source_board_ref exactly and source_board_action exactly. '
            'decision_relation=preserve inherits omitted fields from this source; explicit null clears a field. '
            'For any action, size, level, exit-style, or timing change use decision_relation=override, '
            'override_reason, and override_evidence=[{"source":"delivered source name","claim":"specific evidence"}]. '
            'Timing/purpose/trigger changes also require timing_override_reason. '
            'Confidence may change without changing the decision relation. An evidence-based override is permitted.',
            'Valid evidence source names: ' + ', '.join(sorted(evidence_sources(desk))),
        ]
    previous = desk.cycle_metadata.get('decision_contract_repair_errors', {}).get(artifact_type)
    if previous:
        lines.append('Your previous attempt failed this contract. Correct: ' + '; '.join(previous))
    return '\n'.join(lines)


def status(desk) -> dict:
    if desk.cycle_metadata.get('decision_contract_version') != VERSION:
        return {'version': 0, 'status': 'legacy'}
    board = desk.final_decision or {}
    decision = desk.trade_decision or board
    errors = contract_errors(decision, board=board if desk.trade_decision else None,
                             evidence_sources=evidence_sources(desk))
    return {'version': VERSION, 'status': 'invalid' if errors else 'valid', 'errors': errors,
            'source_board_ref': decision.get('source_board_ref') or board_reference(board),
            'producer': decision.get('decision_producer') or decision.get('persona_used'),
            'decision_relation': decision.get('decision_relation') or 'origin',
            'changed_fields': [key for key in DECISION_FIELDS if decision.get(key, board.get(key)) != board.get(key)]}
