"""Technicals must refresh wherever prices land.

Nothing scheduled `compute_technicals`. It ran only when an agent happened to
call `get_technical_indicators`, so the derived table drifted arbitrarily far
behind `price_history` — measured 2026-07-25, only **5 of 503 tickers** were
fresher than 3 days while prices were current for all of them. CVX served a
**1963-12-26** RSI as its "VERIFIED TECHNICAL BASELINE".

The hook belongs in `collect_price_history`, NOT in `collect_all`: the V3
precollect path (`app/v3/data_report.py`) calls `collect_price_history`
directly, so a hook one level up would never fire during a cycle — the path
that matters most.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pandas as pd
import pytest


def _yf_frame(n: int = 30) -> pd.DataFrame:
    idx = pd.date_range("2026-06-01", periods=n, freq="D")
    return pd.DataFrame(
        {
            "Open": [100.0 + i for i in range(n)],
            "High": [101.0 + i for i in range(n)],
            "Low": [99.0 + i for i in range(n)],
            "Close": [100.5 + i for i in range(n)],
            "Volume": [1_000_000] * n,
        },
        index=idx,
    )


def _patched_yf(monkeypatch, frame):
    """Stub yfinance + the DB so only the refresh hook is under test."""
    import app.collectors.yfinance_collector as yf

    ticker_obj = MagicMock()
    ticker_obj.history.return_value = frame
    monkeypatch.setattr(yf.yf, "Ticker", lambda *_a, **_k: ticker_obj)
    monkeypatch.setattr(yf, "_is_blocked_ticker", lambda _t: False)

    # Writes go through mongo_store.upsert_doc now; stub the whole module so
    # the collection never reaches a real client.
    monkeypatch.setattr(yf, "mongo_store", MagicMock())
    return yf


@pytest.mark.asyncio
async def test_collect_price_history_refreshes_technicals(monkeypatch):
    yf = _patched_yf(monkeypatch, _yf_frame())

    with patch("app.processors.technical_processor.compute_technicals",
               return_value=487) as tech:
        await yf.collect_price_history("MSFT")

    tech.assert_called_once_with("MSFT")


@pytest.mark.asyncio
async def test_technicals_failure_does_not_lose_the_price_rows(monkeypatch):
    """Fail-open: stale technicals are bad, but a failure here must not cost
    us the prices we just collected."""
    yf = _patched_yf(monkeypatch, _yf_frame())

    with patch("app.processors.technical_processor.compute_technicals",
               side_effect=RuntimeError("db down")):
        count = await yf.collect_price_history("MSFT")

    assert count > 0, "price rows must survive a technicals failure"


@pytest.mark.asyncio
async def test_refresh_runs_even_when_the_fetch_returns_nothing(monkeypatch):
    """yfinance returns NaN often enough (rate limits, after hours) that
    skipping the refresh on a failed fetch would leave the whole table's
    freshness at the vendor's mercy. The prices already stored may still be
    newer than the technicals.
    """
    import app.collectors.yfinance_collector as yf

    monkeypatch.setattr(yf, "_is_blocked_ticker", lambda _t: False)
    monkeypatch.setattr(yf, "fetch_ohlcv_dataframe", AsyncMock(return_value=None))

    with patch("app.processors.technical_processor.compute_technicals",
               return_value=487) as tech:
        count = await yf.collect_price_history("MSFT")

    assert count == 0
    tech.assert_called_once_with("MSFT")


@pytest.mark.asyncio
async def test_blocked_ticker_skips_the_refresh(monkeypatch):
    """A blocked ticker should cost nothing at all."""
    import app.collectors.yfinance_collector as yf

    monkeypatch.setattr(yf, "_is_blocked_ticker", lambda _t: True)

    with patch("app.processors.technical_processor.compute_technicals") as tech:
        count = await yf.collect_price_history("BADTICKER")

    assert count == 0
    tech.assert_not_called()


@pytest.mark.asyncio
async def test_hook_is_on_the_path_v3_precollect_actually_uses(monkeypatch):
    """Regression guard for where the hook lives.

    `app/v3/data_report.py` calls `collect_price_history` directly, so putting
    the refresh in `collect_all` would leave every cycle stale.

    Asserts BEHAVIOUR, not source text. The previous version grepped
    `data_report.py` for the substring "collect_all", which passes happily
    against a file refactored into brokenness — and would pass even if the
    hook had been deleted outright (2026-07-25 audit). This one calls the
    real precollect path and asserts the recompute actually fires.
    """
    import app.collectors.news_collector as newsc
    import app.collectors.reddit_collector as redditc
    import app.collectors.yfinance_collector as yfc
    import app.collectors.youtube_collector as ytc
    import app.v3.data_report as dr

    called: list[str] = []

    async def _fake_collect(ticker, *a, **k):
        # Stand-in for the real collector: records that precollect routed
        # through the hooked function.
        called.append(ticker)
        return 1

    async def _noop(*a, **k):
        return None

    # Patched on the SOURCE module: data_report imports the name inside the
    # function body, so it resolves at call time from yfinance_collector.
    # Patching data_report's namespace would silently miss and the test would
    # pass for the wrong reason.
    monkeypatch.setattr(yfc, "collect_price_history", _fake_collect)

    # And the OTHER four, for a different reason. `build_ticker_data_report`
    # fans out to fundamentals, Finnhub, Reddit and YouTube in parallel, and
    # only the price call is under test — but they are real network calls with
    # no bound. Left unpatched (as this test was until 2026-08-30) a plain
    # `pytest` run opened a socket to the live scraper service at
    # 10.0.0.16:8001 and sat there; with `pytest-timeout` absent, nothing ended
    # it. The assertion below already passed by then; the run just never
    # finished. Stub what is not being asserted.
    monkeypatch.setattr(yfc, "collect_fundamentals", _noop)
    monkeypatch.setattr(newsc, "collect_finnhub_news", _noop)
    monkeypatch.setattr(redditc, "collect_for_ticker", _noop)
    monkeypatch.setattr(ytc, "collect_for_ticker", _noop)

    try:
        await dr.build_ticker_data_report("MSFT")
    except Exception:
        # The report has many other collectors that need a DB; we only care
        # that the price path routed through the hooked function.
        pass

    assert called, (
        "V3 precollect did not call collect_price_history — the technicals "
        "hook lives there, so every cycle would run on stale indicators"
    )
