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

def _mock_peak(peak_value, error=None):
    """Patch the breaker's peak read.

    The breaker used to run `SELECT MAX(total_value) FROM portfolio_snapshots`
    through `get_db`; it now issues
    `mongo_query.agg_row('portfolio_snapshots', {'bot_id': ...}, [('max','total_value')])`,
    which returns a TUPLE in the requested aggregate order. Patching `get_db`
    (a symbol paper_trader no longer imports) intercepted nothing — these four
    tests were scoring the REAL portfolio_snapshots collection, so the peak
    they measured against was whatever production held, not 100k.
    """
    q = MagicMock()
    if error is not None:
        q.agg_row.side_effect = error
    else:
        q.agg_row.return_value = (peak_value,)
    return patch("app.trading.paper_trader.mongo_query", q), q


def _assert_peak_read(q):
    """The breaker must read the MAX total_value of THIS bot's snapshots."""
    collection, query, aggs = q.agg_row.call_args[0][:3]
    assert collection == "portfolio_snapshots"
    assert query == {"bot_id": "bot1"}
    assert aggs == [("max", "total_value")]


def test_breaker_blocks_buy_beyond_drawdown_limit():
    ctx, q = _mock_peak(100_000.0)
    with ctx:
        result = _check_drawdown_breaker("bot1", portfolio_value=70_000.0)
    assert result is not None
    assert "drawdown breaker" in result["error"].lower()
    assert result["drawdown_pct"] == -30.0
    assert result["peak_value"] == 100_000.0
    assert result["reason_code"] == "DRAWDOWN_BREAKER"
    assert "SELLs allowed" in result["error"]
    _assert_peak_read(q)


def test_breaker_allows_buy_within_limit():
    ctx, q = _mock_peak(100_000.0)
    with ctx:
        assert _check_drawdown_breaker("bot1", portfolio_value=90_000.0) is None
    _assert_peak_read(q)


def test_breaker_fails_open_without_snapshots():
    # agg_row over an empty match returns SQL's answer for an empty group:
    # (None,) for a MAX. No peak → no block.
    ctx, q = _mock_peak(None)
    with ctx:
        assert _check_drawdown_breaker("bot1", portfolio_value=50_000.0) is None


def test_breaker_fails_open_on_db_error():
    ctx, q = _mock_peak(None, error=RuntimeError("mongo down"))
    with ctx:
        assert _check_drawdown_breaker("bot1", portfolio_value=1.0) is None


def test_breaker_disabled_at_zero(monkeypatch):
    # The limit now resolves through the governed parameter store.
    monkeypatch.setattr("app.trading.paper_trader.get_param", lambda k: 0)
    assert _check_drawdown_breaker("bot1", portfolio_value=1.0) is None
