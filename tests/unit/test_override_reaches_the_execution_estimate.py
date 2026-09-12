"""An override must change exactly what it declares, all the way to execution.

Layer 5 may replace the Board's decision -- that is the point of it -- but no
override has ever executed. Measured 2026-09-11 over the last 400 production
desks: every synthesizer decision that carried a relation carried `preserve`
(20 of 20, the other 5 predate the field), and across the staging cycles r5-r7
the relation was `preserve` with `changed_fields == []` every time. The
validation of an override is covered by unit fixtures; the MERGE that turns one
into the numbers `buy()` is called with is not covered anywhere, and it is the
only path on which this layer can move money.

`effective_decision` is a plain dict merge, so a field the override omits
inherits the Board's value and an explicit null clears it. Both halves are
asserted here against real Board verdicts, together with the refusals that stop
an unjustified change reaching the same place.
"""
from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

import pytest

from app.v3.decision_contract import (
    DECISION_FIELDS, TIMING_FIELDS, board_reference, contract_errors, effective_decision,
    entry_errors as _entry_errors, status)
from app.v3.shared_desk import SharedDesk

FIXTURE = Path(__file__).resolve().parents[1] / 'benchmarks/fixtures/production_board_synth_pairs_v1.json'
PAIRS = json.loads(FIXTURE.read_text())['pairs']
#: Contract-era Boards only. Four of the recorded BUYs (NVDA, MANU, DKS, CRDO)
#: predate entry_mode/trigger_purpose entirely, so the contract rejects them on
#: their own terms before an override is even considered -- see
#: TestLegacyBoardsAreRecognisedAsLegacy. Mixing them in would make every
#: override case fail for a reason that has nothing to do with the override.
BUY_BOARDS = [p for p in PAIRS if p['board']['action'] == 'BUY' and not _entry_errors(p['board'])]
LEGACY_BOARDS = [p for p in PAIRS if p['board']['action'] == 'BUY' and _entry_errors(p['board'])]
SOURCES = {'quant_report', 'data_report'}
REASON = 'The verified guidance cited by the Board was superseded by the later filing.'
TIMING_REASON = 'No immediate entry while the superseded guidance is the entry premise.'
EVIDENCE = [{'source': 'data_report',
             'claim': 'The filed guidance is materially below the number the Board priced.'}]


def desk_with(board, override):
    desk = SharedDesk(ticker='TEST', cycle_id='bench-override')
    desk.cycle_metadata = {'decision_contract_version': 1}
    desk.final_decision = deepcopy(board)
    desk.trade_decision = {**deepcopy(override),
                           'source_board_ref': board_reference(board),
                           'source_board_action': board['action'],
                           'decision_producer': 'v3_decision_synthesizer'}
    return desk


def estimate_of(desk):
    with patch('app.collectors.fund_scanner.get_institutional_signal', return_value={}):
        from app.v3.orchestrator import _build_v1_compatible_result
        return _build_v1_compatible_result(desk)


class TestNoOverrideHasEverBeenObserved:
    """The premise for everything below, measured rather than assumed."""

    def test_the_fixture_is_real_production_pairs(self):
        assert len(PAIRS) >= 20 and BUY_BOARDS
        assert all(p['board'].get('action') in ('BUY', 'SELL', 'HOLD') for p in PAIRS)

    def test_every_recorded_relation_is_preserve(self):
        recorded = [p['synthesizer_relation'] for p in PAIRS if p['synthesizer_relation']]
        assert recorded and set(recorded) == {'preserve'}

    def test_and_the_synthesizer_changed_no_decision_field(self):
        for pair in PAIRS:
            if not pair['synthesizer_relation']:
                continue
            changed = [k for k in DECISION_FIELDS
                       if pair['synthesizer'].get(k) != pair['board'].get(k)]
            assert changed == [], f"{pair['ticker']}: {changed}"


class TestAnOverrideChangesExactlyWhatItDeclares:

    @pytest.mark.parametrize('pair', BUY_BOARDS, ids=lambda p: p['ticker'])
    def test_the_declared_fields_reach_the_execution_estimate(self, pair):
        board = pair['board']
        override = {'decision_relation': 'override', 'override_reason': REASON,
                    'override_evidence': EVIDENCE,
                    'position_size_pct': round((board['position_size_pct'] or 1) / 2, 4),
                    'stop_loss': round((board['stop_loss'] or 100) * 0.95, 4)}
        desk = desk_with(board, override)
        assert contract_errors(desk.trade_decision, board=board, evidence_sources=SOURCES) == []
        assert set(status(desk)['changed_fields']) == {'position_size_pct', 'stop_loss'}
        estimate = estimate_of(desk)['estimate']
        assert estimate['position_size_pct'] == override['position_size_pct']
        assert estimate['stop_loss'] == override['stop_loss']

    @pytest.mark.parametrize('pair', BUY_BOARDS, ids=lambda p: p['ticker'])
    def test_everything_it_omits_is_inherited_not_blanked(self, pair):
        board = pair['board']
        override = {'decision_relation': 'override', 'override_reason': REASON,
                    'override_evidence': EVIDENCE,
                    'position_size_pct': round((board['position_size_pct'] or 1) / 2, 4)}
        desk = desk_with(board, override)
        estimate = estimate_of(desk)['estimate']
        for field in ('take_profit', 'exit_style', 'entry_mode', 'trigger_purpose'):
            assert estimate[field] == board[field], field

    def test_an_explicit_null_clears_the_inherited_field(self):
        board = BUY_BOARDS[0]['board']
        assert board['take_profit'] is not None, 'fixture must start from a real level'
        override = {'decision_relation': 'override', 'override_reason': REASON,
                    'override_evidence': EVIDENCE, 'take_profit': None}
        desk = desk_with(board, override)
        assert effective_decision(desk.trade_decision, board)['take_profit'] is None
        assert 'take_profit' in status(desk)['changed_fields']

    def test_an_action_override_carries_its_timing_to_execution(self):
        board = BUY_BOARDS[0]['board']
        override = {'decision_relation': 'override', 'override_reason': REASON,
                    'timing_override_reason': TIMING_REASON, 'override_evidence': EVIDENCE,
                    'action': 'HOLD', 'entry_mode': 'watch_only', 'trigger_purpose': 'none',
                    'dynamic_trigger': None, 'position_size_pct': 0}
        desk = desk_with(board, override)
        assert contract_errors(desk.trade_decision, board=board, evidence_sources=SOURCES) == []
        result = estimate_of(desk)
        assert result['action'] == 'HOLD'
        assert result['estimate']['entry_mode'] == 'watch_only'
        assert result['estimate']['position_size_pct'] == 0


class TestAnUnjustifiedChangeNeverGetsThere:

    def _base(self):
        board = BUY_BOARDS[0]['board']
        return board, {'decision_relation': 'override', 'override_reason': REASON,
                       'override_evidence': EVIDENCE,
                       'position_size_pct': round((board['position_size_pct'] or 1) / 2, 4),
                       'source_board_ref': board_reference(board),
                       'source_board_action': board['action']}

    def test_a_change_declared_as_preserve_is_refused(self):
        board, candidate = self._base()
        candidate['decision_relation'] = 'preserve'
        assert any('decision_relation=override' in e
                   for e in contract_errors(candidate, board=board, evidence_sources=SOURCES))

    def test_an_override_without_a_reason_is_refused(self):
        board, candidate = self._base()
        candidate.pop('override_reason')
        assert any('override_reason' in e
                   for e in contract_errors(candidate, board=board, evidence_sources=SOURCES))

    def test_evidence_citing_a_source_this_desk_never_had_is_refused(self):
        board, candidate = self._base()
        candidate['override_evidence'] = [{'source': 'a_report_nobody_wrote',
                                           'claim': EVIDENCE[0]['claim']}]
        assert any('override_evidence' in e
                   for e in contract_errors(candidate, board=board, evidence_sources=SOURCES))

    @pytest.mark.parametrize('field', TIMING_FIELDS)
    def test_a_timing_change_without_its_own_reason_is_refused(self, field):
        board, candidate = self._base()
        candidate.update({'entry_mode': 'enter_on_condition', 'trigger_purpose': 'entry',
                          'action': 'BUY',
                          'dynamic_trigger': {'type': 'price_below', 'value': 1.0}})
        candidate.pop('timing_override_reason', None)
        errors = contract_errors(candidate, board=board, evidence_sources=SOURCES)
        assert any('timing_override_reason' in e for e in errors), (field, errors)

    def test_an_override_attributed_to_a_different_board_is_refused(self):
        board, candidate = self._base()
        candidate['source_board_ref'] = 'board:0000000000000000000000'
        assert any('source_board_ref' in e
                   for e in contract_errors(candidate, board=board, evidence_sources=SOURCES))


class TestLegacyBoardsAreRecognisedAsLegacy:
    """A pre-contract Board is refused on its own fields, before any override."""

    def test_the_fixture_still_contains_pre_contract_boards(self):
        assert LEGACY_BOARDS, 'fixture no longer covers the legacy shape'
        assert all(p['board'].get('entry_mode') is None for p in LEGACY_BOARDS)

    @pytest.mark.parametrize('pair', LEGACY_BOARDS, ids=lambda p: p['ticker'])
    def test_a_legacy_board_cannot_pass_the_contract_untouched(self, pair):
        errors = _entry_errors(pair['board'])
        assert any('entry_mode' in e for e in errors)
