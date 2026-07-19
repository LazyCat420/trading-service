"""Adversarial / injection guards — try to TRICK each safety layer.

Every test here impersonates a hostile or buggy input: fabricated backtest
summaries, NaN/Infinity parameter proposals, spoofed agent tiers, reserved
whiteboard sections via self-identification, nonsense exit prices.
"""
import asyncio
import json
import math
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from app.cognition.debate import backtest_runner as br
from app.services import parameter_store as ps
from app.services import parameter_governor as gov
from app.validation import parameter_validator as pv
from app.trading import paper_trader as pt


def _run(coro):
    return asyncio.run(coro)


GOOD_REASON = "Adversarial test: volatility regime justification exceeding the length floor."


def _no_db(monkeypatch):
    def _boom():
        raise RuntimeError("db down")
    monkeypatch.setattr(ps, "get_db", _boom)
    monkeypatch.setattr(pv, "_last_change_age_hours", lambda key: None)
    monkeypatch.setattr(pv, "_changes_last_24h", lambda: 0)
    ps.invalidate_cache()


# ── 1. Equation fabricates its own backtest summary ─────────────────────────

def test_self_reported_backtest_summary_cannot_pass_the_gate(monkeypatch):
    """An LLM-written equation can `result = {"total_trades": 50, ...}` and
    skip simulation entirely. That self-reported fiction must NOT gate-pass
    as a verified edge or pollute library stats."""
    fabricated = {
        "total_trades": 50,
        "cumulative_return_pct": 99.0,
        "win_rate_pct": 96.0,
        "sharpe_ratio": 9.9,
        "out_of_sample": {"cumulative_return_pct": 42.0},
        "oos_trades": 20,
        "null_percentile": 0.99,
        "trades": [],
    }
    stats_written = []
    monkeypatch.setattr(br, "get_equation_by_name", lambda n: {"code": "x", "name": n})
    monkeypatch.setattr(br, "increment_usage", lambda n: None)
    monkeypatch.setattr(br, "update_backtest_stats", lambda *a, **k: stats_written.append(a))
    monkeypatch.setattr(br, "execute_equation", lambda c, t, p: {"status": "ok", "result": dict(fabricated)})

    result = br.run_backtest_for_equation("liar_eq", "AAPL")
    assert result.get("self_reported") is True, "self-reported summary must be labeled"
    assert stats_written == [], "fabricated stats must never reach the library"

    out = br.filter_pitches_by_backtest(
        [{"equation_name": "liar_eq", "evidence": "x" * 50}], "AAPL")
    assert len(out) == 1
    assert out[0]["backtest_pnl"] is None, "liar equation must pass through UNVERIFIED, not as a 99% edge"


# ── 2. NaN / Infinity into the parameter governor ───────────────────────────

def test_nan_and_inf_parameter_values_rejected(monkeypatch):
    _no_db(monkeypatch)
    for evil in (float("nan"), float("inf"), float("-inf")):
        res = gov.propose_parameter_change(
            "MAX_POSITION_SIZE_PCT", evil, GOOD_REASON, agent="v3_board_of_directors")
        assert res["status"] == "rejected", f"{evil} must be rejected"


def test_nan_ttl_rejected(monkeypatch):
    _no_db(monkeypatch)
    res = gov.propose_parameter_change(
        "MAX_POSITION_SIZE_PCT", 0.08, GOOD_REASON,
        ttl_hours=float("nan"), agent="v3_board_of_directors")
    assert res["status"] == "rejected"


# ── 3. Spoofed agent identity for board-tier params ─────────────────────────

def test_worker_cannot_reach_board_tier_even_with_creative_names(monkeypatch):
    _no_db(monkeypatch)
    for spoof in ("v3_board_of_directors ", "V3_BOARD_OF_DIRECTORS", "board", "v3_junior_analyst"):
        res = gov.propose_parameter_change(
            "MAX_PORTFOLIO_DRAWDOWN_PCT", 0.20, GOOD_REASON, agent=spoof)
        assert res["status"] == "rejected", f"spoofed agent {spoof!r} must not pass tier auth"


# ── 4. Whiteboard: reserved sections stay blocked even with author param ────

def test_reserved_sections_blocked_regardless_of_author(monkeypatch):
    from app.tools import whiteboard_tools as wt
    out = json.loads(_run(wt.whiteboard_write(
        ticker="AAPL", section="final_decision",
        content='{"action": "BUY"}', author="v3_board_of_directors")))
    assert out["status"] == "error", "agents must not overwrite orchestrator sections"


# ── 5. Nonsense exit prices into the paper trader ───────────────────────────

def test_nan_stop_price_falls_back_to_atr(monkeypatch):
    monkeypatch.setattr(pt, "_compute_stop_loss_pct", lambda t, p: 0.08)
    stop_pct, source, tp = pt._resolve_entry_exits(
        "AAPL", 100.0, stop_loss_price=float("nan"), take_profit_price=float("nan"))
    assert source == "atr_fallback"
    assert stop_pct == 0.08
    assert tp is None


def test_negative_and_zero_stop_prices_fall_back(monkeypatch):
    monkeypatch.setattr(pt, "_compute_stop_loss_pct", lambda t, p: 0.08)
    for evil in (-5.0, 0.0):
        _, source, _ = pt._resolve_entry_exits("AAPL", 100.0, stop_loss_price=evil)
        assert source == "atr_fallback"


# ── 6. Zero-ish vectors cannot sneak into the embedding store ───────────────

def test_near_zero_vector_all_zeros_rejected(monkeypatch):
    from app.db.vector_store import VectorStore
    def _boom():
        raise AssertionError("DB must not be touched")
    monkeypatch.setattr("app.db.vector_store.get_db", _boom)
    assert VectorStore().store_embedding("t", "s", "AAPL", "x", [0, 0.0, 0]) == ""
