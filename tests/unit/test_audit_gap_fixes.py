"""
Tests for the 2026-07-15 post-deploy-audit gap fixes.

1. Tournament debate nuance (h2h attack points + juror reasoning) now renders
   into the desk context the Board reads — previously only the one-line
   rationale survived in tournament mode (the default).
2. Portfolio drawdown circuit breaker: new BUYs refused when mark-to-market
   value falls MAX_PORTFOLIO_DRAWDOWN_PCT below the recorded snapshot peak;
   fails open with no snapshots; disabled at 0.
"""
from unittest.mock import MagicMock, patch

from app.trading.paper_trader import _check_drawdown_breaker
from app.v3.shared_desk import SharedDesk


# ── Tournament nuance rendering ──────────────────────────────────────

def _tournament_artifact():
    return {
        "summary": "Tournament complete: BUY case won 2-1.",
        "action": "BUY",
        "confidence": 70,
        "winning_side": "bull",
        "vetoed": False,
        "h2h": {
            "thesis_a": {
                "persona": "momentum_quant",
                "claim": "Breakout confirmed",
                "attack_points": ["Opponent ignores fading volume on the breakout"],
            },
            "thesis_b": {
                "persona": "mean_reversion",
                "claim": "Overextended",
                "attack_points": ["RSI divergence contradicts the breakout claim"],
            },
        },
        "jury_verdict": {
            "vetoed": False,
            "jury_results": {
                "Risk_Manager": {"score": 6, "veto": False,
                                 "reasoning": "Stop distance is wide but acceptable"},
                "Skeptic": {"score": 4, "veto": True,
                            "reasoning": "Volume does not confirm the move"},
            },
        },
    }


def test_tournament_nuance_reaches_board_context():
    desk = SharedDesk(ticker="TEST", cycle_id="c1")
    desk.append_artifact("tournament_result", _tournament_artifact())

    ctx = desk.get_compressed_context(include_debate=True)

    assert "attack points" in ctx
    assert "fading volume" in ctx
    assert "RSI divergence" in ctx
    assert "Juror reasoning" in ctx
    assert "Skeptic [VETO]" in ctx
    assert "Stop distance" in ctx


def test_tournament_without_nuance_still_renders():
    desk = SharedDesk(ticker="TEST", cycle_id="c1")
    desk.append_artifact("tournament_result", {
        "summary": "s", "action": "HOLD", "confidence": 0,
        "winning_side": "split", "vetoed": False,
    })
    ctx = desk.get_compressed_context(include_debate=True)
    assert "Tournament Debate Verdict" in ctx
    assert "attack points" not in ctx


# ── Drawdown circuit breaker ─────────────────────────────────────────

def _mock_peak(peak_value):
    db = MagicMock()
    db.execute.return_value.fetchone.return_value = (peak_value,)
    ctx = MagicMock()
    ctx.__enter__ = MagicMock(return_value=db)
    ctx.__exit__ = MagicMock(return_value=False)
    return ctx


def test_breaker_blocks_buy_beyond_drawdown_limit():
    with patch("app.trading.paper_trader.get_db", return_value=_mock_peak(100_000.0)):
        result = _check_drawdown_breaker("bot1", portfolio_value=70_000.0)
    assert result is not None
    assert "drawdown breaker" in result["error"].lower()
    assert result["drawdown_pct"] == -30.0
    assert "SELLs allowed" in result["error"]


def test_breaker_allows_buy_within_limit():
    with patch("app.trading.paper_trader.get_db", return_value=_mock_peak(100_000.0)):
        assert _check_drawdown_breaker("bot1", portfolio_value=90_000.0) is None


def test_breaker_fails_open_without_snapshots():
    with patch("app.trading.paper_trader.get_db", return_value=_mock_peak(None)):
        assert _check_drawdown_breaker("bot1", portfolio_value=50_000.0) is None


def test_breaker_fails_open_on_db_error():
    with patch("app.trading.paper_trader.get_db", side_effect=RuntimeError("db down")):
        assert _check_drawdown_breaker("bot1", portfolio_value=1.0) is None


def test_breaker_disabled_at_zero(monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "MAX_PORTFOLIO_DRAWDOWN_PCT", 0, raising=False)
    assert _check_drawdown_breaker("bot1", portfolio_value=1.0) is None
