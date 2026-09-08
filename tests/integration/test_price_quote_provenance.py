"""Quote-time refresh and execution use an explicitly disposable Mongo database."""
from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock, MagicMock

import pandas as pd
import pytest

pytestmark = [pytest.mark.real_mongo, pytest.mark.asyncio]


async def test_matched_quote_refresh_and_paper_fill(real_mongo, monkeypatch):
    assert real_mongo.name.startswith('trading_bot_pytest_quote_')
    from app.db import mongo_store as store, mongo
    from app.collectors import yfinance_collector as yc
    from app.trading import paper_trader as pt
    monkeypatch.setattr(store, '_BACKENDS', {'*': 'mongo'})
    monkeypatch.setattr(mongo, 'get_mongo_db', lambda: real_mongo)
    # A daily DATE can exceed 96h while its observed close remains inside 96h.
    now = datetime.now(timezone.utc)
    observed = now - timedelta(hours=90)
    day = observed.date()
    stock = MagicMock()
    def frame(price):
        return pd.DataFrame({'Open': [510.], 'High': [520.], 'Low': [490.],
            'Close': [price], 'Volume': [18074400]}, index=pd.DatetimeIndex([day], tz='UTC'))
    stock.history.return_value = frame(499.7)
    stock.history_metadata = {'symbol': 'MSFT', 'regularMarketTime': observed.timestamp(),
        'regularMarketPrice': 499.7, 'exchangeTimezoneName': 'UTC'}
    monkeypatch.setattr(yc.yf, 'Ticker', lambda _: stock)
    monkeypatch.setattr(yc, '_is_blocked_ticker', lambda _: False)
    monkeypatch.setattr(yc, '_refresh_technicals', AsyncMock())
    store.upsert_doc('price_history', {'ticker': 'MSFT', 'date': day, 'source': 'yfinance'},
                     {'ticker': 'MSFT', 'date': day, 'source': 'yfinance', 'close': 510.})
    assert await yc.collect_price_history('MSFT') == 1
    assert real_mongo.price_history.count_documents({}) == 1
    price, age = pt._get_current_price('MSFT', now=now)
    assert price == pytest.approx(499.7)
    assert age == pytest.approx(90, abs=1e-5)
    # A slow response for the same day cannot overwrite newer price provenance.
    stock.history.return_value = frame(501.)
    stock.history_metadata.update(regularMarketPrice=501.,
        regularMarketTime=(observed - timedelta(minutes=1)).timestamp())
    await yc.collect_price_history('MSFT')
    assert pt._get_current_price('MSFT', now=now)[0] == pytest.approx(499.7)
    # Real execution and ledger writes; only unrelated notification is isolated.
    monkeypatch.setattr(pt, 'record_fund_alert', lambda *a, **k: None)
    result = await pt.buy('quote_fixture', 'MSFT', 0.015,
                         cycle_id='bench-quote-provenance', stop_loss_price=470.13,
                         take_profit_price=572.92)
    assert 'error' not in result, result
    fill = real_mongo.trade_fills.find_one({'cycle_id': 'bench-quote-provenance'})
    assert fill is not None
    assert fill['reference_price_age_hours'] >= 90
    assert fill['reference_price_source'] == 'stored_price'
    order = real_mongo.orders.find_one({'id': fill['order_id']})
    lot = real_mongo.position_lots.find_one({'fill_id': fill['fill_id']})
    position = real_mongo.positions.find_one({'bot_id': 'quote_fixture', 'ticker': 'MSFT'})
    bot = real_mongo.bots.find_one({'bot_id': 'quote_fixture'})
    assert order['qty'] == pytest.approx(fill['fill_qty'])
    assert lot['remaining_qty'] == pytest.approx(position['qty'])
    assert float(bot['starting_cash'].to_decimal() - bot['cash_balance'].to_decimal()) == pytest.approx(float(fill['fill_value'].to_decimal()))
    assert result['price_age_hours'] >= 90
    # Same-cycle replay cannot add another fill.
    duplicate = await pt.buy('quote_fixture', 'MSFT', 0.015, cycle_id='bench-quote-provenance')
    assert 'Duplicate' in duplicate.get('error', '')
    assert real_mongo.trade_fills.count_documents({}) == 1
