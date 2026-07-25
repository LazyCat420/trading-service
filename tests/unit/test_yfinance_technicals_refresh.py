"""`collect_all` must refresh technicals whenever it refreshes prices.

Nothing scheduled `compute_technicals`. It ran only when an agent happened to
call `get_technical_indicators`, so the derived table drifted arbitrarily far
behind `price_history` — measured 2026-07-25, only **5 of 503 tickers** were
fresher than 3 days while prices were current for all of them. The quant
analyst was handed those rows as its "VERIFIED TECHNICAL BASELINE".

Technicals are a pure function of price_history, so the refresh belongs at the
point prices land.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest


@pytest.mark.asyncio
async def test_collect_all_refreshes_technicals():
    import app.collectors.yfinance_collector as yf

    with patch.object(yf, "collect_price_history", AsyncMock(return_value=250)), \
         patch.object(yf, "collect_fundamentals", AsyncMock(return_value=True)), \
         patch.object(yf, "collect_financials", AsyncMock(return_value=4)), \
         patch.object(yf, "collect_balance_sheet", AsyncMock(return_value=4)), \
         patch("app.processors.technical_processor.compute_technicals",
               return_value=487) as tech:
        out = await yf.collect_all("MSFT")

    tech.assert_called_once_with("MSFT")
    assert out["technical_rows"] == 487
    assert out["price_rows"] == 250


@pytest.mark.asyncio
async def test_technicals_failure_does_not_lose_the_price_rows():
    """Fail-open: stale technicals are bad, but a failure here must not cost
    us the prices we just collected."""
    import app.collectors.yfinance_collector as yf

    with patch.object(yf, "collect_price_history", AsyncMock(return_value=250)), \
         patch.object(yf, "collect_fundamentals", AsyncMock(return_value=True)), \
         patch.object(yf, "collect_financials", AsyncMock(return_value=4)), \
         patch.object(yf, "collect_balance_sheet", AsyncMock(return_value=4)), \
         patch("app.processors.technical_processor.compute_technicals",
               side_effect=RuntimeError("db down")):
        out = await yf.collect_all("MSFT")

    assert out["price_rows"] == 250
    assert out["technical_rows"] == 0


@pytest.mark.asyncio
async def test_blocked_ticker_skips_everything():
    import app.collectors.yfinance_collector as yf

    with patch.object(yf, "_is_blocked_ticker", return_value=True), \
         patch("app.processors.technical_processor.compute_technicals") as tech:
        out = await yf.collect_all("BADTICKER")

    tech.assert_not_called()
    assert out["price_rows"] == 0
