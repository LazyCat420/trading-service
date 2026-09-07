"""Routes must preserve substantive-analysis age and honor material wakes."""
from datetime import datetime, timezone
from unittest.mock import patch

from app.v3.shared_desk import SharedDesk


def test_material_wake_and_unknown_news_never_glance_skip():
    from app.v3.triage import select_tier
    knobs = dict(deep_hours=72, deep_news_volume=8, glance_hours=12)
    assert select_tier(2, 0, trigger_type='price_below', **knobs) == 'v3_delta'
    assert select_tier(2, None, **knobs) == 'v3_deep'
    assert select_tier(2, 0, **knobs) == 'v3_glance'
    assert select_tier(80, 0, **knobs) == 'v3_deep'
    assert select_tier(2, 0, force_full=True, **knobs) == 'v3_deep'


def test_delta_only_finalizes_a_valid_non_executable_decision():
    from app.v3.triage import delta_needs_panel
    from app.v3.shared_desk import PhaseOutcome
    for action in ['BUY', 'SELL']:
        assert delta_needs_panel({'action': action, 'confidence': 80,
                                  'verdict': 'ADJUST', 'escalate': False}, PhaseOutcome.SUCCESS)
    hold = {'action': 'HOLD', 'confidence': 72, 'verdict': 'REAFFIRM', 'escalate': False}
    assert not delta_needs_panel(hold, PhaseOutcome.SUCCESS)
    assert delta_needs_panel(hold, PhaseOutcome.AGENT_ERROR)
    assert delta_needs_panel({'summary': 'No usable decision'}, PhaseOutcome.SUCCESS)
    assert delta_needs_panel({**hold, 'escalate': True}, PhaseOutcome.SUCCESS)
    assert delta_needs_panel({**hold, 'confidence': 'nonsense'}, PhaseOutcome.SUCCESS)


def test_prior_reader_skips_glance_and_keeps_original_thesis_age():
    from app.v3.desk_persistence import load_latest_desk_for_ticker
    real = SharedDesk(ticker='TEST', cycle_id='cycle-v3-100')
    real.created_at = '2026-09-01T12:00:00+00:00'
    real.final_decision = {'action': 'HOLD', 'confidence': 72, 'decision_provenance': 'board_reasoned'}
    skipped = SharedDesk(ticker='TEST', cycle_id='cycle-v3-200')
    skipped.created_at = '2026-09-07T01:00:00+00:00'
    skipped.cycle_metadata['triage_tier'] = 'v3_glance'
    skipped.final_decision = {'action': 'HOLD', 'confidence': 0, 'decision_provenance': 'triage_skip'}
    # Patch only the storage boundary. The production reader selects the desk.
    with patch('app.v3.desk_persistence.mongo_query.find_row', return_value=(skipped.to_dict(),)), \
         patch('app.v3.desk_persistence.mongo_query.find_rows', return_value=[(skipped.to_dict(),), (real.to_dict(),)]):
        prior = load_latest_desk_for_ticker('TEST')
    assert prior.cycle_id == real.cycle_id
    assert prior.created_at == real.created_at
    assert prior.final_decision['confidence'] == 72
