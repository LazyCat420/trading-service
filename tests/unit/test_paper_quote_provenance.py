from datetime import date, datetime, timezone, timedelta
from unittest.mock import AsyncMock, MagicMock

import pandas as pd
import pytest

from app.trading.price_observation import SOURCE, quote_observation
from app.trading import paper_trader as pt

NOW = datetime(2026, 9, 8, 11, 40, tzinfo=timezone.utc)
OBSERVED = datetime(2026, 9, 4, 20, 0, 1, tzinfo=timezone.utc)
DAY = date(2026, 9, 4)
META = {'symbol': 'MSFT', 'regularMarketTime': OBSERVED.timestamp(),
        'regularMarketPrice': 499.7, 'exchangeTimezoneName': 'America/New_York'}


def test_holiday_weekend_uses_observed_quote_not_midnight(monkeypatch):
    observation = quote_observation('MSFT', DAY, 499.70001220703125, META, now=NOW)
    assert observation['price_as_of'] == OBSERVED
    monkeypatch.setattr(pt.mongo_query, 'find_row', lambda *a, **k:
        (499.70001220703125, DAY, observation['price_as_of'], SOURCE, 499.7))
    price, age = pt._get_current_price('MSFT', now=NOW)
    assert price == pytest.approx(499.7)
    assert age == pytest.approx(87.666388889)
    assert age < pt.MAX_PRICE_AGE_HOURS
    assert age > pt.STALE_FILL_WARN_HOURS  # remains an explicitly cached fill


@pytest.mark.parametrize('change', [
    {'symbol': 'OTHER'}, {'regularMarketPrice': 501},
    {'regularMarketTime': (NOW + timedelta(seconds=1)).timestamp()},
    {'regularMarketTime': (OBSERVED - timedelta(days=1)).timestamp()},
    {'regularMarketTime': float('nan')}, {'regularMarketPrice': float('inf')},
    {'exchangeTimezoneName': 'invalid/timezone'}, {'exchangeTimezoneName': None},
])
def test_provider_mismatch_never_refreshes_bar(change):
    assert quote_observation('MSFT', DAY, 499.7, {**META, **change}, now=NOW) is None


@pytest.mark.parametrize('observed,source,quote', [
    (None, SOURCE, 499.7), (NOW + timedelta(days=1), SOURCE, 499.7),
    (OBSERVED, 'collection_time', 499.7), (OBSERVED, SOURCE, 510),
    ('not a date', SOURCE, 499.7), (OBSERVED, SOURCE, float('nan')),
])
def test_missing_or_invalid_provenance_retains_refusal(monkeypatch, observed, source, quote):
    monkeypatch.setattr(pt.mongo_query, 'find_row', lambda *a, **k:
        (499.7, DAY, observed, source, quote))
    _, age = pt._get_current_price('MSFT', now=NOW)
    assert age > pt.MAX_PRICE_AGE_HOURS


def test_genuinely_old_observation_still_refused(monkeypatch):
    monkeypatch.setattr(pt.mongo_query, 'find_row', lambda *a, **k:
        (499.7, DAY, OBSERVED, SOURCE, 499.7))
    _, age = pt._get_current_price('MSFT', now=NOW + timedelta(days=1))
    assert age > pt.MAX_PRICE_AGE_HOURS


def test_crypto_fallback_keeps_its_actual_timestamp(monkeypatch):
    def lookup(collection, *args, **kwargs):
        return None if collection == 'price_history' else (100, NOW - timedelta(hours=2))
    monkeypatch.setattr(pt.mongo_query, 'find_row', lookup)
    assert pt._get_current_price('COIN', now=NOW) == (100, 2)


@pytest.mark.asyncio
async def test_collector_preserves_quote_time_and_refreshes_matching_daily_row(monkeypatch):
    from app.collectors import yfinance_collector as yc
    stock = MagicMock()
    stock.history.return_value = pd.DataFrame({'Open': [510.], 'High': [511.],
        'Low': [499.36], 'Close': [499.70001220703125], 'Volume': [18074400]},
        index=pd.DatetimeIndex(['2026-09-04'], tz='America/New_York'))
    stock.history_metadata = META
    monkeypatch.setattr(yc.yf, 'Ticker', lambda _: stock)
    monkeypatch.setattr(yc, '_is_blocked_ticker', lambda _: False)
    monkeypatch.setattr(yc, '_refresh_technicals', AsyncMock())
    store = MagicMock()
    monkeypatch.setattr(yc, 'mongo_store', store)
    assert await yc.collect_price_history('MSFT') == 1
    collection, query, update = store.update_docs.call_args.args
    assert collection == 'price_history'
    assert query['$or'][1] == {'price_as_of': {'$lte': OBSERVED}}
    assert update['$set']['price_as_of'] == OBSERVED
    assert update['$set']['close'] == pytest.approx(499.7)
    assert update['$set']['date'] == DAY
    assert update['$set']['price_as_of_source'] == SOURCE
