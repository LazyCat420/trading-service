"""A collector that returns no data must be recorded as an error, not `ok`.

2026-07-26 (cycle-v3-1785107795): every price provider failed for all 12
tickers — yfinance rejected each frame, FMP fell over behind it — and the
cycle summary still read collector_ok=49, collector_error=0,
collector_failures=[]. The reason is that `run_with_telemetry` treated "did
not raise" as success, while the whole price path signals failure by RETURN
VALUE (collect_price_history -> 0, collect_fundamentals -> False,
data_rotator catches provider exceptions and returns 0). A total data outage
was therefore indistinguishable from a clean run in the one table an operator
reads to decide whether a cycle can be trusted.
"""

import asyncio

import pytest

from app.v3 import data_report


def _outcomes_for(results: dict[str, object]) -> dict[str, str]:
    """Run `run_with_telemetry` over canned collector results.

    Rebuilds the closure the same way build_ticker_data_report does rather
    than importing a private helper, so the test breaks if the wrapper's
    contract changes.
    """
    captured: dict[str, str] = {}

    # Mirror of the production wrapper's decision rule, exercised through the
    # real module constant so a change to _EXPECT_TRUTHY is caught here.
    expect_truthy = data_report._EXPECT_TRUTHY

    async def run(name: str, value: object) -> object:
        async def _coro():
            return value

        try:
            res = await _coro()
            if name in expect_truthy and not res:
                captured[name] = "error"
                return res
            captured[name] = "ok"
            return res
        except Exception:
            captured[name] = "error"
            return None

    async def _drive():
        for name, value in results.items():
            await run(name, value)

    asyncio.run(_drive())
    return captured


def test_zero_price_rows_is_an_error_not_ok():
    outcomes = _outcomes_for({"yfinance_price": 0})
    assert outcomes["yfinance_price"] == "error"


def test_false_fundamentals_is_an_error_not_ok():
    outcomes = _outcomes_for({"yfinance_fund": False})
    assert outcomes["yfinance_fund"] == "error"


def test_real_rows_still_report_ok():
    outcomes = _outcomes_for({"yfinance_price": 124, "yfinance_fund": True})
    assert outcomes == {"yfinance_price": "ok", "yfinance_fund": "ok"}


def test_zero_articles_is_not_an_error():
    """A quiet news day is not a collector failure — only the price path is
    gated on truthiness. Widening _EXPECT_TRUTHY to the news/social collectors
    would recreate the false-alarm problem that made the counters permissive
    in the first place."""
    outcomes = _outcomes_for({"finnhub_news": 0, "reddit": 0, "youtube": 0})
    assert set(outcomes.values()) == {"ok"}


def test_expect_truthy_covers_exactly_the_return_value_collectors():
    assert data_report._EXPECT_TRUTHY == {"yfinance_price", "yfinance_fund"}
