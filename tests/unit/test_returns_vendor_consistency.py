"""
Vendor consistency in the return loaders + the stale-conclusion flag
(2026-07-29).

`source` is part of the price_history primary key, so one ticker-date can carry
several vendor prints, and the vendors do NOT agree — measured over 9,225
dual-source ticker-dates across 38 tickers, mean absolute close difference
20.05%, with 2,959 pairs over 50bps. It is an adjustment-convention gap
(yfinance adjusted, polygon raw), so it is systematic.

Both failure directions are pinned below, because the bug did not have one sign:
  * pairing two prints of the same date dilutes variance (CRH 253-bar
    annualized vol read 25.18% against a true 32.44%)
  * alternating between conventions across dates manufactures jumps (DRIP read
    2,660.95% with 133 daily moves over 15%, against 232.39% and 1)
"""

import numpy as np
import pandas as pd
import pytest

from app.quant.returns import _keep_dominant_source
from app.quant.technical_baseline import mark_conclusion_stale
from app.v3.shared_desk import render_stale_conclusion


def _dual_source_frame():
    """One ticker, 4 dates, two vendors ~13% apart, plus a single-source ticker."""
    dates = pd.to_datetime(["2026-07-20", "2026-07-21", "2026-07-22", "2026-07-23"])
    rows = []
    for d, adj, raw in zip(dates, [100.0, 101.0, 102.0, 103.0],
                           [87.0, 87.9, 88.7, 89.6]):
        rows.append({"ticker": "DUAL", "date": d, "close": adj, "source": "yfinance"})
        rows.append({"ticker": "DUAL", "date": d, "close": raw, "source": "polygon"})
    # yfinance wins on row count only if it actually has more rows; give it one
    # extra date so the preference is unambiguous.
    rows.append({"ticker": "DUAL", "date": pd.Timestamp("2026-07-24"),
                 "close": 104.0, "source": "yfinance"})
    for d, c in zip(dates, [50.0, 50.5, 51.0, 51.5]):
        rows.append({"ticker": "SOLO", "date": d, "close": c, "source": "yfinance"})
    return pd.DataFrame(rows)


# ── vendor pinning ───────────────────────────────────────────────────

def test_dominant_source_collapses_to_one_row_per_date():
    df = _keep_dominant_source(_dual_source_frame())
    dual = df[df["ticker"] == "DUAL"]
    assert len(dual) == len(dual["date"].unique()), "duplicate ticker-dates survived"


def test_dominant_source_keeps_the_higher_row_count_vendor():
    df = _keep_dominant_source(_dual_source_frame())
    assert set(df[df["ticker"] == "DUAL"]["source"]) == {"yfinance"}


def test_single_source_ticker_is_untouched():
    """A safe fix is a no-op where there is nothing to fix (measured: AAPL
    24.73% before and after)."""
    original = _dual_source_frame()
    df = _keep_dominant_source(original)
    solo_before = original[original["ticker"] == "SOLO"].reset_index(drop=True)
    solo_after = df[df["ticker"] == "SOLO"].reset_index(drop=True)
    pd.testing.assert_frame_equal(
        solo_before[["ticker", "date", "close"]],
        solo_after[["ticker", "date", "close"]],
    )


def test_mixing_vendors_would_manufacture_jumps():
    """The DRIP failure mode, in miniature: convention-alternating rows produce
    huge fake returns; one pinned vendor produces none."""
    df = _dual_source_frame()
    mixed = df[df["ticker"] == "DUAL"].sort_values(["date", "source"])
    mixed_ret = np.diff(np.log(mixed["close"].to_numpy(dtype=float)))
    assert (np.abs(mixed_ret) > 0.10).sum() > 0, "fixture is not adversarial enough"

    pinned = _keep_dominant_source(df)
    pinned = pinned[pinned["ticker"] == "DUAL"].sort_values("date")
    pinned_ret = np.diff(np.log(pinned["close"].to_numpy(dtype=float)))
    assert (np.abs(pinned_ret) > 0.10).sum() == 0


def test_mixing_vendors_would_also_dilute_variance():
    """The CRH failure mode, which needs its own fixture.

    Which direction the bug goes depends on how far apart the vendors are:
    far apart (DRIP, 718%) manufactures jumps, CLOSE TOGETHER (CRH, ~1%) dilutes
    variance, because each same-date pair contributes a near-zero return that
    halves the effective sample without changing the span. Both are wrong; only
    the second one looks plausible, which is why it survived.
    """
    dates = pd.to_datetime([f"2026-07-{d:02d}" for d in range(1, 21)])
    adj = 100.0 * np.cumprod(1 + np.tile([0.02, -0.018], 10))
    rows = []
    for d, a in zip(dates, adj):
        rows.append({"ticker": "CLOSE", "date": d, "close": a, "source": "yfinance"})
        rows.append({"ticker": "CLOSE", "date": d, "close": a * 1.004,
                     "source": "polygon"})
    rows.append({"ticker": "CLOSE", "date": pd.Timestamp("2026-07-21"),
                 "close": float(adj[-1]), "source": "yfinance"})
    df = pd.DataFrame(rows)

    def ann_vol(series):
        r = np.diff(np.log(np.asarray(series, dtype=float)))
        return float(r.std(ddof=1) * np.sqrt(252) * 100)

    duplicated = df.sort_values(["date", "source"])["close"]
    pinned = _keep_dominant_source(df).sort_values("date")["close"]

    assert ann_vol(duplicated) < ann_vol(pinned), (
        "duplicate ticker-dates must deflate the volatility estimate"
    )
    # Understated by roughly a third here; measured 23% on CRH.
    assert ann_vol(duplicated) < 0.8 * ann_vol(pinned)


def test_stale_dominant_vendor_loses_to_fresh_minority():
    """The cycle-v3-1785504601 RBLX shape: depth alone must not pin a dead series.

    yfinance stopped writing RBLX on 2026-07-17 (a bad vendor bar re-failed
    validation every collection) while polygon carried bars through 07-30. The
    row-count rule kept choosing yfinance, so the desk analysed RBLX at the
    07-17 close — 24% off the real price — with the fresh series sitting in the
    same table. Freshness outranks depth.
    """
    rows = [
        {"ticker": "RBLX", "date": pd.Timestamp("2026-07-17") - pd.Timedelta(days=i),
         "close": 51.0, "source": "yfinance"}
        for i in range(20)
    ] + [
        {"ticker": "RBLX", "date": pd.Timestamp("2026-07-30") - pd.Timedelta(days=i),
         "close": 39.0, "source": "polygon"}
        for i in range(5)
    ]
    df = _keep_dominant_source(pd.DataFrame(rows))
    assert set(df["source"]) == {"polygon"}


def test_overnight_publishing_skew_does_not_flip_the_vendor():
    """One vendor updating a day later than the other is not staleness; the
    deep vendor must keep winning or the pin would flip-flop conventions on
    ordinary mornings."""
    rows = [
        {"ticker": "SKEW", "date": pd.Timestamp("2026-07-29") - pd.Timedelta(days=i),
         "close": 100.0, "source": "yfinance"}
        for i in range(20)
    ] + [
        {"ticker": "SKEW", "date": pd.Timestamp("2026-07-30") - pd.Timedelta(days=i),
         "close": 87.0, "source": "polygon"}
        for i in range(5)
    ]
    df = _keep_dominant_source(pd.DataFrame(rows))
    assert set(df["source"]) == {"yfinance"}


def test_no_source_column_is_a_passthrough():
    df = pd.DataFrame({"ticker": ["A"], "date": [pd.Timestamp("2026-07-20")],
                       "close": [1.0]})
    pd.testing.assert_frame_equal(_keep_dominant_source(df), df)


# ── stale conclusions ────────────────────────────────────────────────

def test_conclusion_flagged_when_its_inputs_were_corrected():
    art = {"thesis_direction": "BULLISH", "confidence": 85}
    mark_conclusion_stale(
        art, ["thesis_direction"],
        {"rsi": {"model": 99.0, "verified": 66.79}}, "risk metrics",
    )
    assert art["_conclusion_is_stale"] is True
    assert art["_conclusion_stale_fields"] == ["thesis_direction"]
    assert "99.0" in art["_conclusion_stale_reason"]
    assert "66.79" in art["_conclusion_stale_reason"]


def test_no_corrections_means_no_flag():
    art = {"thesis_direction": "BULLISH"}
    mark_conclusion_stale(art, ["thesis_direction"], {}, "risk metrics")
    assert "_conclusion_is_stale" not in art


def test_absent_conclusion_field_means_no_flag():
    """Nothing to discount if the agent never stated the call."""
    art = {}
    mark_conclusion_stale(
        art, ["thesis_direction"], {"rsi": {"model": 1, "verified": 2}}, "x"
    )
    assert "_conclusion_is_stale" not in art


@pytest.mark.parametrize("artifact", [None, {}, {"thesis_direction": "BULLISH"}])
def test_render_is_empty_for_sound_conclusions(artifact):
    assert render_stale_conclusion(artifact) == ""


def test_render_reaches_the_board():
    """Computed and never rendered is work nothing downstream can see."""
    art = {"verdict": "UNDERVALUED"}
    mark_conclusion_stale(
        art, ["verdict"], {"pe": {"model": 8.0, "verified": 31.4}},
        "valuation metrics",
    )
    out = render_stale_conclusion(art)
    assert "STALE CONCLUSION" in out
    assert "verdict" in out
    assert "31.4" in out
