"""Every way a price fetch can fail must say which way it was.

Over 07-27..07-31 the desk logged 18 `yfinance_price returned no data` events,
15 of them inside the 09:xx ET hour. `execution_errors` — which captures every
WARNING and ERROR — held no record of any of them: no rate limit, no timeout,
nothing. The fetcher logged its exception path at INFO and its `df is None`
path not at all, so the cause was unknowable from outside the process.

These tests assert the tags exist and are distinct. They are deliberately about
the LOG TEXT, because the log text is the artifact being fixed: a reader has to
be able to count the three failure modes apart in SQL.

    SELECT error_message FROM execution_errors WHERE error_message LIKE '%FETCH_%'
"""
from __future__ import annotations

import asyncio
import logging

import pandas as pd
import pytest

from app.collectors import yfinance_collector as yc


def _run(coro):
    return asyncio.run(coro)


class _FakeTicker:
    """Stands in for yf.Ticker. `history` either returns a frame or raises."""

    def __init__(self, result=None, exc: Exception | None = None):
        self._result, self._exc = result, exc

    def history(self, *_a, **_kw):
        if self._exc is not None:
            raise self._exc
        return self._result


@pytest.fixture
def patch_ticker(monkeypatch):
    def _install(result=None, exc=None):
        monkeypatch.setattr(yc.yf, "Ticker", lambda _t: _FakeTicker(result, exc))
    return _install


def test_empty_frame_is_tagged_and_warned(patch_ticker, caplog):
    patch_ticker(result=pd.DataFrame())
    with caplog.at_level(logging.WARNING):
        assert _run(yc.fetch_ohlcv_dataframe("AAPL")) is None
    assert "FETCH_EMPTY" in caplog.text
    assert "AAPL" in caplog.text


def test_none_frame_is_tagged(patch_ticker, caplog):
    patch_ticker(result=None)
    with caplog.at_level(logging.WARNING):
        assert _run(yc.fetch_ohlcv_dataframe("AAPL")) is None
    assert "FETCH_EMPTY" in caplog.text


def test_exception_records_its_TYPE_not_just_a_message(patch_ticker, caplog):
    """A 429 and a timeout must not be indistinguishable in the log.

    This is the specific regression: the old line was
    `logger.info(f"...: {e}")`, so the class of failure was thrown away.
    """
    patch_ticker(exc=TimeoutError("read timed out"))
    with caplog.at_level(logging.WARNING):
        assert _run(yc.fetch_ohlcv_dataframe("AAPL")) is None
    assert "FETCH_EXCEPTION" in caplog.text
    assert "TimeoutError" in caplog.text, "the exception TYPE must be logged"
    assert "read timed out" in caplog.text


def test_all_bars_incomplete_is_its_own_tag(patch_ticker, caplog):
    """A frame that arrives but is entirely unusable is a third, distinct case."""
    df = pd.DataFrame(
        {"Open": [None], "High": [None], "Low": [None], "Close": [None], "Volume": [10]},
        index=pd.to_datetime(["2026-07-31"]),
    )
    patch_ticker(result=df)
    with caplog.at_level(logging.WARNING):
        assert _run(yc.fetch_ohlcv_dataframe("AAPL")) is None
    assert "FETCH_ALL_INCOMPLETE" in caplog.text
    assert "FETCH_EMPTY" not in caplog.text, "the three tags must be distinguishable"


def test_the_three_fetch_tags_are_mutually_exclusive(patch_ticker, caplog):
    seen = {}
    for label, kwargs in (
        ("empty", {"result": pd.DataFrame()}),
        ("exception", {"exc": ValueError("boom")}),
    ):
        caplog.clear()
        patch_ticker(**kwargs)
        with caplog.at_level(logging.WARNING):
            _run(yc.fetch_ohlcv_dataframe("AAPL"))
        seen[label] = {t for t in ("FETCH_EMPTY", "FETCH_EXCEPTION",
                                   "FETCH_ALL_INCOMPLETE") if t in caplog.text}
    assert seen["empty"] == {"FETCH_EMPTY"}
    assert seen["exception"] == {"FETCH_EXCEPTION"}


def test_collect_logs_when_it_gets_no_frame(monkeypatch, caplog):
    """The branch that used to log nothing at all."""
    async def _no_frame(*_a, **_kw):
        return None

    async def _noop(*_a, **_kw):
        return None

    monkeypatch.setattr(yc, "fetch_ohlcv_dataframe", _no_frame)
    monkeypatch.setattr(yc, "_refresh_technicals", _noop)
    monkeypatch.setattr(yc, "_is_blocked_ticker", lambda _t: False)

    with caplog.at_level(logging.WARNING):
        assert _run(yc.collect_price_history("AAPL")) == 0
    assert "COLLECT_NO_FRAME" in caplog.text
    assert "AAPL" in caplog.text


def test_a_healthy_fetch_logs_no_failure_tag(patch_ticker, caplog):
    """The guard against crying wolf: a good frame must stay quiet."""
    idx = pd.to_datetime(["2026-07-30", "2026-07-31"])
    df = pd.DataFrame(
        {"Open": [1.0, 1.1], "High": [1.2, 1.3], "Low": [0.9, 1.0],
         "Close": [1.1, 1.2], "Volume": [100, 110]}, index=idx,
    )
    patch_ticker(result=df)
    with caplog.at_level(logging.WARNING):
        out = _run(yc.fetch_ohlcv_dataframe("AAPL"))
    assert out is not None and len(out) == 2
    assert "FETCH_" not in caplog.text
