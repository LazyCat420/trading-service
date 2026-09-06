"""A 30-row return over a two-vendor series is a 15-day return.

MEASURED 2026-09-06 (boot-resilience audit, live 90-day window): 45 of 509
S&P tickers carry rows from two vendors on the same dates — LULU, AVGO, C,
GOOG, NVDA each show 124 rows across 62 distinct trading days. `price_history`
is keyed (ticker, date, source); `compute_sector_performance` joined on ticker
and date only and then ran `pct_change(periods=30)` per ticker over the
interleaved rows, so for those names "30 days" reached back 15 trading days.
The sibling in the same file, `_prices_on`, already dedupes with
`keep_dominant_source`; the compute path never did. The sector momentum that
feeds the regime engine was wrong for a tenth of the universe.

Fixture: one ticker, 62 dates, the same close from two vendors. The honest
30-day return is close[61]/close[31]-1; the duplicated series yields
close[61]/close[46]-1.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from app.data import sector_aggregator as sa

DATES = 62
START = datetime(2026, 6, 1, tzinfo=timezone.utc)


def _close(i: int) -> float:
    return 100.0 + i


def _rows(sources):
    out = []
    for i in range(DATES):
        d = START + timedelta(days=i)
        for src in sources:
            # select order: ticker, date, close, volume, sector, market_cap, source
            out.append(("LULU", d, _close(i), 1_000_000, "Consumer", 1e10, src))
    return out


def _run(sources):
    written = []

    def fake_join(*a, **kw):
        return _rows(sources)

    def fake_upsert(coll, flt, doc):
        written.append(doc)

    with patch.object(sa.mongo_query, "join_rows", fake_join), \
         patch.object(sa.mongo_store, "upsert_doc", fake_upsert):
        asyncio.run(sa.compute_sector_performance())
    assert len(written) == 1, written
    return written[0]


HONEST_30D = (_close(DATES - 1) / _close(DATES - 31) - 1) * 100
FIFTEEN_DAY = (_close(DATES - 1) / _close(DATES - 16) - 1) * 100


def test_two_vendors_do_not_halve_the_window():
    doc = _run(["yfinance", "polygon"])
    assert abs(doc["avg_return_30d"] - HONEST_30D) < 1e-6, (
        f"avg_return_30d={doc['avg_return_30d']:.4f}: honest 30-day is "
        f"{HONEST_30D:.4f}, the interleaved 15-day figure is {FIFTEEN_DAY:.4f}"
    )


def test_one_vendor_is_unchanged():
    doc = _run(["yfinance"])
    assert abs(doc["avg_return_30d"] - HONEST_30D) < 1e-6


def test_the_fixture_can_tell_the_two_apart():
    """Control: if the two figures coincided the test would prove nothing."""
    assert abs(HONEST_30D - FIFTEEN_DAY) > 1.0
