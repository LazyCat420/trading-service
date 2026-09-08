from copy import deepcopy
from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.v3.arithmetic_audit import audit_artifact, check_text, arithmetic_handoff, board_plan_math
from app.v3.shared_desk import SharedDesk


@pytest.mark.parametrize('text,expected', [
    ('cash down from $94.6B to $75.5B (-25% YoY)', '-20.19027484143763213530655391'),
    ('cash DOWN 25% YoY ($94.6B to $75.5B) and debt UP 43% ($44.9B to $64.1B)', '-20.19027484143763213530655391'),
    ('the +14% run off the 50-day ($444.10 to $499.70)', '12.51970276964647601891465886'),
    ('from 100 to 120 (30% increase)', '20.0'),
])
def test_observed_percentage_errors(text, expected):
    errors = [c for c in check_text(text) if c['status'] != 'consistent']
    assert len(errors) == 1
    assert Decimal(errors[0]['expected']) == Decimal(expected)


@pytest.mark.parametrize('text', [
    'PPI m/m forecast 0.4 vs 0.0 prior (a 4x acceleration)',
    'PPI m/m consensus 0.4 vs 0.0 prior is a genuine 4x acceleration forecast',
    'from 0.0 to 0.4 (4x increase)',
    'from 0 to 5 (100% growth)',
])
def test_zero_denominator_is_unknown_not_a_number(text):
    assert check_text(text)[0]['status'] == 'undefined'
    assert check_text(text)[0]['expected'] is None


@pytest.mark.parametrize('text', [
    'debt up from $44.9B to $64.1B (+43%)',
    'cash from $94.6B to $75.5B (-20.19% YoY)',
    'forecast 0.4 vs prev 0.1 — a 4x acceleration',
    'forecast 0.4 vs 0.1 prior (4x)',
    'from 100 to 120 (+20%)',
    'from 100 to 80 (down 20%)',
    'a 20% decline from 100 to 80',
    'from 100 to 100 (0% growth)',
    'from 100 to 200 (2x)',
])
def test_consistent_arithmetic_including_rounding(text):
    checks = check_text(text)
    assert checks and all(c['status'] == 'consistent' for c in checks)


@pytest.mark.parametrize('text', [
    'from $94.6B to $75.5M (-25%)',  # mismatched scale
    'from -10 to 5 (150% growth)',  # convention-dependent
    'from 5% to 7% (2 percentage points)',
    'from 94.6 to 75.5 and debt increased 43%',
    'cash ($94.6B to $75.5B), debt up 43%',
    'from 100 to 120 (not 30%)',
    'from 100 to 120 (30% is wrong)',
    'cash down 25% without stated operands',
    'from 2026-09-01 to 2026-09-08, 25% of observations',
    'RSI <=35 does not imply a fixed price of 95',
])
def test_ambiguous_unrelated_or_denied_claims_are_not_asserted_errors(text):
    assert not check_text(text)


def test_append_checks_original_prose_and_delivers_corrections_to_later_roles():
    desk = SharedDesk(ticker='TEST', cycle_id='test-math')
    artifact = {'summary': 'cash down from $94.6B to $75.5B (-25% YoY)',
                'confidence': 72, 'action': 'BUY'}
    original = deepcopy(artifact)
    desk.append_artifact('desk_note', artifact)
    assert {key: artifact[key] for key in original} == original
    assert artifact['_arithmetic_audit']['errors'] == 1
    block = arithmetic_handoff(desk)
    assert '-20.1903%' in block
    assert 'do not verify source facts' in block
    restored = SharedDesk.from_dict(desk.to_dict())
    assert arithmetic_handoff(restored) == block


def test_no_check_is_not_claimed_to_be_verified():
    artifact = {'summary': 'Cash weakened, but the thesis still holds.'}
    report = audit_artifact(artifact)
    assert report['checked'] == 0 and report['errors'] == 0
    assert report['scope'] == 'explicit_operand_arithmetic_only'
    assert report == audit_artifact(artifact)  # excludes its own metadata


def test_debate_corrections_are_scoped_and_duplicates_do_not_snowball():
    desk = SharedDesk()
    for name in ['bear_rebuttal', 'bull_defense', 'debate_judge']:
        desk.append_artifact(name, {'summary': 'from 100 to 120 (30%)'})
    assert arithmetic_handoff(desk) == ''
    assert arithmetic_handoff(desk, include_debate=True).count('computed 20.0000%') == 1


def test_current_board_plan_math_replaces_stale_ratio_without_changing_plan():
    board = {'action': 'BUY', 'entry_mode': 'enter_now', 'stop_loss': 470.13, 'take_profit': 572.92}
    desk = SimpleNamespace(final_decision=board, cycle_metadata={'technical_baseline_context': '  - close: 499.7   [price: age unknown]'})
    original = deepcopy(board)
    block = board_plan_math(desk)
    assert 'reward/risk 2.4762:1' in block
    assert 'not a live execution quote' in block
    assert board == original
    board['entry_mode'] = 'enter_on_condition'
    assert board_plan_math(desk) == ''


@pytest.mark.parametrize('stop,target,baseline', [
    (500, 572, '- close: 499.7'), (470, 480, '- close: 499.7'),
    (float('nan'), 572, '- close: 499.7'), (470, 572, 'no close'),
])
def test_invalid_or_unknown_plan_does_not_invent_ratio(stop, target, baseline):
    desk = SimpleNamespace(final_decision={'action': 'BUY', 'entry_mode': 'enter_now', 'stop_loss': stop, 'take_profit': target}, cycle_metadata={'technical_baseline_context': baseline})
    assert board_plan_math(desk) == ''


def test_learning_receipt_excludes_proven_arithmetic_error(monkeypatch):
    from app.services.learning import receipts
    captured = []
    monkeypatch.setattr(receipts.mongo_store, 'upsert_doc', lambda table, query, row, **kwargs: captured.append(row))
    result = {'learning_identity': {'conversation_id': 'offline', 'output_hash': 'hash', 'agent': 'junior', 'project': 'vllm-trading-bot'}}
    receipts.queue_artifact_receipt(result, cycle_id='test', ticker='TEST', role='junior', artifact_type='desk_note', artifact={'summary': 'from 0 to 0.4 (4x)'}, valid=True)
    assert captured[0]['valid'] is False
    assert captured[0]['reason'] == 'arithmetic_inconsistent'
    assert captured[0]['arithmetic_errors'] == 1

def test_neighbouring_balance_sheet_claims_keep_their_own_percentages():
    checks = check_text('cash down from $94.6B to $75.5B (-25% YoY), debt up from $44.9B to $64.1B (+43%).')
    assert len(checks) == 2
    assert [c['status'] for c in checks] == ['mismatch', 'consistent']


def test_quote_verified_arithmetic_error_is_not_promotable_or_served():
    from datetime import datetime, timedelta, timezone
    from app.services.learning import consolidation, freshness
    now = datetime.now(timezone.utc)
    quote = 'The balance sheet shows cash down from $94.6B to $75.5B (-25% YoY).'
    observation = {'id': 'obs', 'ticker': 'TEST', 'created_at': now-timedelta(days=1), 'source_type': 'pipeline', 'cycle_id': 'cycle-v3-real', 'observation_text': quote}
    proposal = {'new_or_updated_memories': [{'source_evidence': [{'observation_id': 'obs', 'quote': quote}]}]}
    with pytest.raises(ValueError, match='inconsistent arithmetic'):
        consolidation.validate_result(proposal, 'TEST', [observation], [])
    memory = {'status': 'active', 'contract_version': 2, 'validation_state': 'source_verified',
        'summary': 'Historical balance-sheet observation', 'source_evidence': [{'quote': quote, 'cycle_id': 'cycle-v3-real'}],
        'valid_from': now-timedelta(days=1), 'valid_until': now+timedelta(days=10)}
    assert not freshness.eligible_memory(memory)
    memory['source_evidence'][0]['quote'] = quote.replace('-25%', '-20.19%')
    assert freshness.eligible_memory(memory)


@pytest.mark.parametrize('text', [
    'Confidence 62% -> 60% -> 72%',
    'Confidence 55%→69%→72%→73%',
    'Net income +82% (margin 21%→36%)',
    'EPS $0.27 → $2.65, revenue +46.6%',
    'Revenue from 100 to 120, income increased 30%',
    'Revenue from 100 to 120; margins improved 30%',
    'from -$10 to $5 (150% growth)',
])
def test_chained_levels_and_neighbouring_metrics_are_not_growth_claims(text):
    assert not check_text(text)


def test_high_structural_quality_cannot_hide_an_arithmetic_error(monkeypatch):
    from app.v3 import quality_scorer
    for name in ['_score_content_density', '_score_data_completeness', '_score_consistency', '_score_source_grounding']:
        monkeypatch.setattr(quality_scorer, name, lambda *args: 90)
    artifact = {'summary': 'cash down from $94.6B to $75.5B (-25% YoY)', 'confidence': 72}
    result = quality_scorer.score_artifact('desk_note', artifact)
    assert result['quality_score'] == 90
    assert result['structural_flag'] == 'good'
    assert result['flag'] == 'needs_review'
    assert result['evidence_status'] == 'arithmetic_inconsistent'
    assert artifact['confidence'] == 72
    result = quality_scorer.score_artifact('desk_note', {'summary': 'Thesis remains sound.'})
    assert result['evidence_status'] == 'not_established'
