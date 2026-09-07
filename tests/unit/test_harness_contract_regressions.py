"""Behavioral regressions from the September 6 end-to-end harness audit.

These assert preserved intent/evidence and real policy outcomes, not successful
function calls. Live order and database access are never used.
"""
from copy import deepcopy
from datetime import datetime, timezone
from unittest.mock import patch

import pytest

from app.v3.shared_desk import SharedDesk
from app.v3.hold_reason import classify_hold
from app.services.watch_allocator import evidence_from_trip
from app.services.watch_triage import TriageInputs, triage

NOW = datetime(2026, 9, 7, tzinfo=timezone.utc)


def test_agent_keep_is_not_rewritten_as_exit_by_baseline_disagreement():
    desk = SharedDesk(ticker='SNOW', cycle_id='isolated-keep')
    desk.cycle_metadata = {'held': True, 'decision_score': {'band': 'AVOID'}}
    desk.final_decision = {
        'action': 'HOLD', 'confidence': 72, 'decision_provenance': 'board_reasoned',
        'reasoning': 'Thesis intact; keep the existing position at its current size.',
    }
    result = classify_hold(desk, 'HOLD')
    assert result['hold_reason'] == 'KEEP'
    assert result['basis'] == 'agent_decision'
    assert 'baseline:avoid_band' in result['signals']
    assert desk.final_decision['action'] == 'HOLD'


def test_unknown_news_time_cannot_be_certified_fresh():
    evidence = evidence_from_trip({}, {'type': 'news'}, 'Headline without matched source', None, {}, NOW)
    assert evidence.observed_at is None
    verdict = triage(TriageInputs(ticker='TEST', watch={}, evidence=evidence, now=NOW,
                                 budget_left=6, budget_total=6))
    assert not verdict.eligible
    assert verdict.reject_reason == 'unknown_evidence_age'


def test_matched_old_news_is_rejected_on_world_timestamp():
    old = datetime(2026, 8, 1, tzinfo=timezone.utc)
    evidence = evidence_from_trip({}, {'type': 'news'}, 'News: “Old headline”', None,
                                  {'news': [('Old headline', old)]}, NOW)
    verdict = triage(TriageInputs(ticker='TEST', watch={}, evidence=evidence, now=NOW,
                                 budget_left=6, budget_total=6))
    assert evidence.observed_at == old
    assert not verdict.eligible
    assert verdict.reject_reason == 'stale_evidence'


def test_complete_defense_answers_survive_saturated_research():
    desk = SharedDesk(ticker='OWL', cycle_id='isolated-defense')
    desk.desk_note = {'summary': 'Research ' * 2500}
    desk.fundamental_report = {'summary': 'More evidence ' * 2000}
    desk.bull_argument = {'summary': 'The business is growing.', 'claims': ['Recurring revenue']}
    desk.bear_rebuttal = {'summary': 'Three independent risks.',
                         'independent_risks': ['Valuation', 'Institutional selling', 'Dilution']}
    answers = [
        {'risk': 'Valuation', 'answer': 'Valuation is expensive; the concession reduces proposed size to one percent.'},
        {'risk': 'Institutional selling', 'answer': 'The filing identifies index rebalancing, which does not establish discretionary selling.'},
        {'risk': 'Dilution', 'answer': 'Dilution remains unresolved until the next share count disclosure; keep it as a risk.'},
    ]
    desk.bull_defense = {'summary': 'Thesis narrowed after concessions.',
                         'independent_risks_answered': answers,
                         'thesis_survives': True, 'final_confidence': 65}
    context = desk.get_compressed_context(include_debate=True)
    assert len(context) <= 10000
    for pair in answers:
        assert pair['answer'] in context
    assert 'Bull Defense' in context
    assert 'never answered' not in context


def test_explicit_board_timing_survives_synthesis_and_reaches_adapter():
    from app.v3.decision_contract import board_reference, contract_errors
    from app.v3.orchestrator import _build_v1_compatible_result
    desk = SharedDesk(ticker='DKS', cycle_id='isolated-contract')
    desk.cycle_metadata = {'decision_contract_version': 1}
    board = {'action': 'BUY', 'confidence': 72, 'reasoning': 'Wait for the pullback.',
             'position_size_pct': 1.0, 'stop_loss': 118, 'take_profit': 155,
             'entry_mode': 'enter_on_condition', 'trigger_purpose': 'entry',
             'dynamic_trigger': {'type': 'sma_50_drop', 'value': 125.2}}
    desk.final_decision = board
    desk.trade_decision = {**deepcopy(board), 'source_board_ref': board_reference(board),
                           'source_board_action': 'BUY', 'decision_relation': 'preserve'}
    assert contract_errors(desk.trade_decision, board=board, evidence_sources={'quant_report'}) == []
    with patch('app.collectors.fund_scanner.get_institutional_signal', return_value={}):
        result = _build_v1_compatible_result(desk)
    assert result['estimate']['entry_mode'] == 'enter_on_condition'
    assert result['estimate']['trigger_purpose'] == 'entry'
    assert result['decision_contract']['source_board_ref'] == board_reference(board)


def test_timing_override_requires_reason_even_when_trigger_bytes_unchanged():
    from app.v3.decision_contract import board_reference, contract_errors
    board = {'action': 'BUY', 'confidence': 71, 'position_size_pct': 1.1,
             'entry_mode': 'enter_now', 'trigger_purpose': 'monitor',
             'dynamic_trigger': {'type': 'sma_50_drop', 'value': 10.57}}
    candidate = {**deepcopy(board), 'entry_mode': 'enter_on_condition',
                 'trigger_purpose': 'entry', 'source_board_ref': board_reference(board),
                 'source_board_action': 'BUY', 'decision_relation': 'preserve'}
    errors = contract_errors(candidate, board=board, evidence_sources={'quant_report'})
    assert errors
    candidate.update(decision_relation='override', override_reason='The latest verified volatility warrants waiting for a pullback.',
                     override_evidence=[{'source': 'quant_report', 'claim': 'Verified volatility has doubled since the initial estimate.'}])
    assert any('timing_override_reason' in e for e in contract_errors(candidate, board=board, evidence_sources={'quant_report'}))
    candidate['timing_override_reason'] = 'Wait for a lower entry because the verified volatility doubled.'
    assert contract_errors(candidate, board=board, evidence_sources={'quant_report'}) == []


def test_justified_action_override_and_hold_remain_valid():
    from app.v3.decision_contract import board_reference, contract_errors
    board = {'action': 'BUY', 'confidence': 72, 'position_size_pct': 1,
             'entry_mode': 'enter_now', 'trigger_purpose': 'none'}
    candidate = {**deepcopy(board), 'action': 'HOLD', 'entry_mode': 'watch_only',
                 'position_size_pct': 0, 'source_board_ref': board_reference(board),
                 'source_board_action': 'BUY', 'decision_relation': 'override',
                 'override_reason': 'The cited earnings release contradicts the premise of the Board decision.',
                 'timing_override_reason': 'No entry while the earnings premise remains refuted.',
                 'override_evidence': [{'source': 'data_report', 'claim': 'Reported guidance is lower than the number used by the Board.'}]}
    assert contract_errors(candidate, board=board, evidence_sources={'data_report'}) == []
    candidate['source_board_ref'] = 'an unrelated Board'
    assert contract_errors(candidate, board=board, evidence_sources={'data_report'})
