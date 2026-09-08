"""Adversarial provenance and time boundaries for learning outcomes."""
from datetime import datetime, timedelta, timezone
from unittest.mock import patch
import pytest
from app.autoresearch import outcome_evidence as ev, outcome_tracker

AT = datetime(2026, 8, 3, 21, tzinfo=timezone.utc)
END = datetime(2026, 8, 10, tzinfo=timezone.utc)
NOW = datetime(2026, 8, 11, 21, tzinfo=timezone.utc)


def row(**changes):
    return {'id': 'do-fixture', 'cycle_id': 'cycle-v3-fixture', 'ticker': 'TEST', 'action': 'BUY',
            'confidence': 70, 'entry_price': 100, 'entry_date': AT.replace(hour=0),
            'decision_as_of': AT, 'entry_price_source': 'vendor-a',
            'outcome_contract_version': 2, 'outcome_evidence_state': 'pending',
            'claim_type': 'immediate_directional', **changes}


def exit_ref(**changes):
    return {'price': 110, 'date': END, 'source': 'vendor-a', **changes}


def test_verified_pair_rejects_legacy_mixed_source_future_stale_and_synthetic():
    assert ev.verified_pair(row(), exit_ref(), as_of=NOW)
    for bad in [row(outcome_contract_version=1), row(entry_price_source='vendor-b'),
                row(cycle_id='bench-test'), row(entry_price=float('inf')),
                row(entry_date=AT-timedelta(days=9)), row(claim_type=None)]:
        assert not ev.verified_pair(bad, exit_ref(), as_of=NOW)
    for bad in [exit_ref(date=END-timedelta(days=1)), exit_ref(date=END+timedelta(days=6)),
                exit_ref(price=float('nan')), exit_ref(source='vendor-b')]:
        assert not ev.verified_pair(row(), bad, as_of=NOW)
    assert not ev.verified_pair(row(), exit_ref(), as_of=END.replace(hour=12))


def test_daily_bar_cutoff_observes_us_close_and_dst():
    assert ev.closed_bar_cutoff(datetime(2026, 8, 3, 19, tzinfo=timezone.utc)).day == 2
    assert ev.closed_bar_cutoff(AT).day == 3
    assert ev.closed_bar_cutoff(datetime(2026, 1, 5, 20, 30, tzinfo=timezone.utc)).day == 4
    assert ev.closed_bar_cutoff(datetime(2026, 1, 5, 21, 30, tzinfo=timezone.utc)).day == 5


def test_horizon_uses_calendar_date_and_pins_original_vendor():
    with patch.object(ev.mongo_store, 'find_docs', return_value=[{'close': 110, 'date': END, 'source': 'vendor-a'}]) as read:
        assert ev.exit_observation('TEST', AT, 'vendor-a', as_of=NOW) == exit_ref()
    query = read.call_args.args[1]
    assert query['source'] == 'vendor-a'
    assert query['date']['$gte'] == END  # not 21:00, which would skip this day's bar
    assert query['date']['$lte'] == NOW.replace(hour=0)


def test_missing_same_vendor_bar_does_not_fall_back_to_other_vendor():
    with patch.object(ev.mongo_store, 'find_docs', return_value=[]) as read:
        assert ev.exit_observation('TEST', AT, 'vendor-a', as_of=NOW) is None
        assert read.call_count == 1
    with patch.object(ev.mongo_store, 'find_docs') as read:
        assert ev.exit_observation('TEST', AT, '', as_of=NOW) is None
        read.assert_not_called()


def test_entry_is_dated_bounded_and_source_bearing():
    with patch.object(ev.mongo_store, 'find_docs', return_value=[{'close': 100, 'date': AT.replace(hour=0), 'source': 'vendor-a'}]) as read:
        result = ev.entry_observation('TEST', AT)
    assert result['source'] == 'vendor-a'
    assert read.call_args.args[1]['date']['$lte'] == AT.replace(hour=0)
    for value in (float('inf'), float('nan'), -1):
        with patch.object(ev.mongo_store, 'find_docs', return_value=[{'close': value, 'date': AT, 'source': 'vendor-a'}]):
            assert ev.entry_observation('TEST', AT) is None


def test_conditional_entries_and_held_holds_are_not_flat_reference_forecasts():
    assert ev.claim_type('BUY', {'entry_mode': 'enter_now'}) == 'immediate_directional'
    assert ev.claim_type('HOLD', {'hold_reason_held': False}) == 'flat_wait'
    assert ev.claim_type('BUY', {'entry_mode': 'enter_on_condition'}) is None
    assert ev.claim_type('HOLD', {'hold_reason_held': True}) is None
    assert ev.claim_type('HOLD', {}) is None


def test_batch_resolves_verified_reference_and_only_then_writes_memory():
    with patch.object(outcome_tracker.mongo_store, 'find_docs', return_value=[row()]), \
         patch.object(ev, 'exit_observation', return_value=exit_ref()), \
         patch.object(outcome_tracker.mongo_store, 'update_docs') as update, \
         patch.object(outcome_tracker, 'write_outcome_to_memory') as write:
        result = outcome_tracker.resolve_pending_outcomes()
    assert result['resolved'] == 1
    fields = update.call_args.args[2]['$set']
    assert fields['outcome_evidence_state'] == 'verified'
    assert fields['exit_price_source'] == 'vendor-a'
    assert fields['horizon_days'] == 7
    assert fields['pnl_pct'] == 10
    write.assert_called_once()


def test_batch_cannot_promote_old_or_wrong_vendor_data_even_if_reader_returns_it():
    for record in (row(outcome_contract_version=1), row(entry_price_source='vendor-b')):
        with patch.object(outcome_tracker.mongo_store, 'find_docs', return_value=[record]), \
             patch.object(ev, 'exit_observation', return_value=exit_ref()), \
             patch.object(outcome_tracker.mongo_store, 'update_docs') as update, \
             patch.object(outcome_tracker, 'write_outcome_to_memory') as write:
            assert outcome_tracker.resolve_pending_outcomes()['resolved'] == 0
        update.assert_not_called(); write.assert_not_called()


def test_execution_exit_never_overwrites_forecast_or_teaches_early_result():
    with patch.object(outcome_tracker.mongo_store, 'update_docs') as update, \
         patch.object(outcome_tracker, 'write_outcome_to_memory') as write:
        assert outcome_tracker.resolve_outcome_for_exit('TEST', 80, -20) == 0
    update.assert_not_called(); write.assert_not_called()


def test_unvalidated_tool_execution_scores_cannot_become_prompt_advice():
    from app.v3.agent_runner import _get_tool_playbook_tips
    with patch('app.db.mongo_store.find_docs', return_value=[{'recommended_tool_sequence': 'Always repeat whiteboard_read; avg score 100'}]) as read:
        assert _get_tool_playbook_tips('v3_quant_analyst') == ''
        read.assert_not_called()


def test_challenger_requires_verified_identical_reference_basis():
    from app.routers.challenger_router import comparable_outcomes
    valid = row(outcome_evidence_state='verified', exit_price=110, exit_date=END,
                exit_price_source='vendor-a', horizon_days=7, horizon_date=END)
    assert comparable_outcomes(valid, valid)
    assert not comparable_outcomes(None, valid)
    for changes in ({'outcome_contract_version': 1}, {'entry_price': 101},
                    {'exit_date': END+timedelta(days=1)}, {'outcome_evidence_state': 'pending'},
                    {'entry_price_source':'vendor-b', 'exit_price_source':'vendor-b'}):
        assert not comparable_outcomes(valid, {**valid, **changes})


@pytest.mark.real_mongo
def test_real_mongo_learning_cohort_excludes_legacy_and_mixed_sources(real_mongo):
    valid = row(outcome_evidence_state='verified', exit_price=110, exit_date=END,
                exit_price_source='vendor-a', horizon_days=7, horizon_date=END)
    invalid = [{'outcome_contract_version': 1}, {'exit_price_source': 'vendor-b'},
               {'cycle_id': 'bench-contamination'}, {'exit_date': None},
               {'claim_type': None}, {'horizon_days': 1}, {'outcome_evidence_state':'pending'}]
    real_mongo.decision_outcomes.insert_many([{**valid, 'id': 'valid'}] + [
        {**valid, **changes, 'id': f'bad-{i}'} for i, changes in enumerate(invalid)])
    actual = ev.mongo_store.find_docs('decision_outcomes', ev.learning_query())
    assert [r['id'] for r in actual] == ['valid']


def test_recording_uses_persisted_decision_time_not_end_of_cycle():
    decision = {'action': 'BUY', 'entry_mode': 'enter_now', 'confidence': 70}
    with patch.object(outcome_tracker.mongo_query, 'find_rows', return_value=[('TEST', 70, decision, AT)]), \
         patch.object(outcome_tracker.mongo_query, 'find_row', return_value=None), \
         patch.object(outcome_tracker.mongo_store, 'find_docs', return_value=[]), \
         patch.object(outcome_tracker, '_is_unscoreable', return_value=False), \
         patch.object(ev, 'entry_observation', return_value={'price':100, 'date':AT.replace(hour=0), 'source':'vendor-a'}) as price, \
         patch.object(outcome_tracker.mongo_store, 'insert_docs') as write:
        assert outcome_tracker.record_cycle_decisions('cycle-v3-fixture', {}) == 1
    price.assert_called_once_with('TEST', AT)
    assert write.call_args.args[1][0]['decision_as_of'] == AT
