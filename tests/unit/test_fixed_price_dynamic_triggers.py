from unittest.mock import patch
import pytest
from app.trading.order_triggers import dynamic_condition_is_met, dynamic_trigger_is_evaluable, create_trigger
from app.v3.decision_contract import entry_errors

@pytest.mark.parametrize('setup,price,expected', [('price_below', 96, False), ('price_below', 95, True), ('price_below', 94, True), ('price_above', 94, False), ('price_above', 95, True), ('price_above', 96, True)])
def test_fixed_price_threshold_ignores_moving_average(setup, price, expected):
    assert dynamic_trigger_is_evaluable(setup)
    for sma in (None, 90, 110):
        assert dynamic_condition_is_met(setup, 95, current_price=price, metric_val=sma) == (expected, '')


def test_sma_and_rsi_do_not_mean_fixed_price():
    assert dynamic_condition_is_met('sma_50_drop', 95, current_price=96, metric_val=100) == (True, '')
    assert dynamic_condition_is_met('price_below', 95, current_price=96, metric_val=100) == (False, '')
    assert dynamic_condition_is_met('rsi_14_oversold', 35, current_price=96, metric_val=34) == (True, '')


@pytest.mark.parametrize('value', [None, 0, -1, float('nan'), float('inf'), True, '95'])
def test_invalid_price_thresholds_never_fire(value):
    met, reason = dynamic_condition_is_met('price_below', value, current_price=90, metric_val=None)
    assert met is False and reason


def test_conditional_entry_contract_accepts_fixed_price_and_rejects_invalid_rsi():
    decision = {'action': 'BUY', 'entry_mode': 'enter_on_condition', 'trigger_purpose': 'entry', 'dynamic_trigger': {'type': 'price_below', 'value': 95}}
    assert entry_errors(decision) == []
    decision['dynamic_trigger'] = {'type': 'rsi_14_oversold', 'value': 350}
    assert 'RSI trigger value must be at most 100' in entry_errors(decision)

@pytest.mark.asyncio
@pytest.mark.parametrize('price,active', [(96, True), (95, False)])
async def test_fixed_price_creation_preserves_condition_and_already_met_guard(monkeypatch, price, active):
    from app.trading import order_triggers
    rows = []
    monkeypatch.setattr(order_triggers.mongo_store, 'insert_docs', lambda c, d, **kw: rows.extend(d) if c == 'price_triggers' else None)
    monkeypatch.setattr(order_triggers.mongo_store, 'update_docs', lambda *a, **kw: None)
    monkeypatch.setattr(order_triggers, '_get_current_price', lambda ticker: (price, 'fixture'))
    monkeypatch.setattr(order_triggers, '_current_metric', lambda *a: pytest.fail('fixed price must not query a moving average'))
    result = await create_trigger(bot_id='test', ticker='TEST', trigger_type='dynamic', trigger_price=0, action='BUY', dynamic_trigger_type='price_below', dynamic_trigger_value=95, created_by='pipeline')
    assert 'error' not in result
    assert rows[0]['active'] is active
    assert rows[0]['dynamic_trigger_type'] == 'price_below'
    assert rows[0]['dynamic_trigger_value'] == 95
