"""Real backtest gate — transaction costs, OOS split, coin-flip null test,
holding-period Sharpe, and honest pass-through below the evidence threshold."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from app.cognition.debate import backtest_runner as br


def _signals(prices, start="2026-01-01"):
    """Alternating BUY/SELL signals over given prices, ~5 days apart."""
    import datetime
    sigs = []
    d = datetime.date.fromisoformat(start)
    for i, p in enumerate(prices):
        sigs.append({
            "action": "BUY" if i % 2 == 0 else "SELL",
            "price": p,
            "date": d.isoformat(),
        })
        d += datetime.timedelta(days=5)
    return sigs


def test_costs_reduce_returns():
    # Buy 100, sell 101: gross +1%, net of 7.5bps/side ≈ +0.85%
    trades = br._simulate_from_signals(_signals([100.0, 101.0]))
    assert len(trades) == 1
    assert trades[0]["return_pct"] < 1.0
    assert trades[0]["return_pct"] > 0.7


def test_flat_roundtrip_loses_the_friction():
    trades = br._simulate_from_signals(_signals([100.0, 100.0]))
    assert trades[0]["return_pct"] < 0  # costs make a flat trade a loser


def test_sharpe_annualized_by_holding_period():
    # 5-day holds must NOT be annualized as daily returns: same per-trade
    # returns → holding-aware Sharpe is ~sqrt(5) smaller than the old formula.
    import numpy as np
    trades = br._simulate_from_signals(
        _signals([100, 102, 100, 103, 100, 101, 100, 104, 100, 102]))
    sharpe = br._sharpe_from_trades(trades)
    returns = [t["return_pct"] for t in trades]
    old_style = (np.mean(returns) / np.std(returns, ddof=1)) * np.sqrt(252)
    assert 0 < sharpe < old_style
    assert abs(sharpe - old_style / np.sqrt(5)) / old_style < 0.1


def test_null_percentile_flags_real_vs_noise():
    # Consistent winners beat almost all coin-flip resamples…
    winners = [{"return_pct": 2.0, "entry_date": "2026-01-01", "exit_date": "2026-01-06"}] * 8
    assert br._null_percentile(winners) > 0.9
    # …a perfectly symmetric win/loss record does not.
    mixed = []
    for i in range(8):
        mixed.append({"return_pct": 2.0 if i % 2 == 0 else -2.0,
                      "entry_date": "2026-01-01", "exit_date": "2026-01-06"})
    assert br._null_percentile(mixed) < 0.85
    # Below the evidence threshold there is no verdict at all.
    assert br._null_percentile(winners[:3]) is None


def _pitch(name="eq_x"):
    return {"equation_name": name, "evidence": "x" * 50}


def test_filter_passes_through_thin_backtests(monkeypatch):
    monkeypatch.setattr(br, "run_backtest_for_equation", lambda n, t, p: {
        "total_trades": 2, "cumulative_return_pct": 50.0, "trades": [],
    })
    out = br.filter_pitches_by_backtest([_pitch()], "AAPL")
    assert len(out) == 1
    assert out[0]["backtest_pnl"] is None  # not "proved" by 2 trades


def test_filter_eliminates_negative_oos(monkeypatch):
    monkeypatch.setattr(br, "run_backtest_for_equation", lambda n, t, p: {
        "total_trades": 20, "cumulative_return_pct": 12.0,  # great in-sample
        "out_of_sample": {"cumulative_return_pct": -3.0},   # dies held-out
        "oos_trades": 6, "null_percentile": 0.9, "sharpe_ratio": 1.0, "trades": [],
    })
    assert br.filter_pitches_by_backtest([_pitch()], "AAPL") == []


def test_filter_eliminates_coin_flip_edge(monkeypatch):
    monkeypatch.setattr(br, "run_backtest_for_equation", lambda n, t, p: {
        "total_trades": 20, "cumulative_return_pct": 4.0,
        "out_of_sample": {"cumulative_return_pct": 2.0},
        "oos_trades": 6, "null_percentile": 0.4, "sharpe_ratio": 1.0, "trades": [],
    })
    assert br.filter_pitches_by_backtest([_pitch()], "AAPL") == []


def test_filter_keeps_real_edge(monkeypatch):
    monkeypatch.setattr(br, "run_backtest_for_equation", lambda n, t, p: {
        "total_trades": 20, "cumulative_return_pct": 9.0,
        "out_of_sample": {"cumulative_return_pct": 4.0},
        "oos_trades": 6, "null_percentile": 0.85, "sharpe_ratio": 1.2, "trades": [],
    })
    out = br.filter_pitches_by_backtest([_pitch()], "AAPL")
    assert len(out) == 1
    assert out[0]["backtest_pnl"] == 9.0


def test_oos_split_fields_present(monkeypatch):
    prices = [100, 102, 100, 103, 100, 101, 100, 104, 100, 102, 100, 105]
    monkeypatch.setattr(br, "get_equation_by_name", lambda n: {"code": "x", "name": n})
    monkeypatch.setattr(br, "increment_usage", lambda n: None)
    monkeypatch.setattr(br, "update_backtest_stats", lambda *a, **k: None)
    monkeypatch.setattr(br, "execute_equation", lambda c, t, p: {
        "status": "ok", "result": {"signals": _signals(prices)},
    })
    res = br.run_backtest_for_equation("eq_x", "AAPL")
    assert "out_of_sample" in res and "in_sample" in res
    assert res["costs_applied_pct_per_side"] == br.COST_PCT_PER_SIDE
    assert isinstance(res.get("null_percentile"), float)
    assert res["oos_trades"] >= 1
