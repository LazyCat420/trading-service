"""
Agent-research wave (2026-07-21 audit): artifact validators, code-side
consensus sizing, skip_debate mitigation contract, precomputed quant block,
FRED curve/credit briefing lines.
"""

import contextlib

import numpy as np
import pytest

from app.v3.artifact_validators import (
    validate_artifact,
    validate_regime_artifact,
    validate_trade_decision_artifact,
)
from app.services.pipeline_service import resolve_buy_size_pct


# ── Regime artifact validator ────────────────────────────────────────

def test_regime_enum_literal_coerced():
    a = validate_regime_artifact({"regime": "HIGH_VOLATILITY|DEEP_DISCOUNT|CONTRADICTORY"})
    assert a["regime"] == "CONTRADICTORY"
    assert a["_validator_notes"]


def test_regime_partial_match_coerced_to_containing_label():
    assert validate_regime_artifact({"regime": "volatility"})["regime"] == "HIGH_VOLATILITY"


def test_regime_valid_untouched():
    a = validate_regime_artifact({"regime": "DEEP_DISCOUNT", "factors": {"volatility": 0.4}})
    assert a["regime"] == "DEEP_DISCOUNT"
    assert "_validator_notes" not in a


def test_regime_factors_clamped_and_nonnumeric_dropped():
    a = validate_regime_artifact({
        "regime": "CONTRADICTORY",
        "factors": {"volatility": 1.7, "trend_strength": -0.2, "liquidity": "high"},
    })
    assert a["factors"]["volatility"] == 1.0
    assert a["factors"]["trend_strength"] == 0.0
    assert "liquidity" not in a["factors"]


def test_regime_mods_normalized():
    assert validate_regime_artifact({"regime": "CONTRADICTORY"})["suggested_pipeline_modifications"] == []
    a = validate_regime_artifact({"regime": "CONTRADICTORY", "suggested_pipeline_modifications": "skip_debate"})
    assert a["suggested_pipeline_modifications"] == ["skip_debate"]


# ── Trade-decision trigger validator ─────────────────────────────────

def test_trigger_null_value_trailing_gets_default():
    a = validate_trade_decision_artifact(
        {"action": "HOLD", "dynamic_trigger": {"type": "trailing_drop", "value": None}}
    )
    assert a["dynamic_trigger"]["value"] == 0.10


def test_trigger_null_value_metric_type_gets_placeholder():
    a = validate_trade_decision_artifact(
        {"action": "HOLD", "dynamic_trigger": {"type": "sma_100_drop", "value": None}}
    )
    # order_triggers only needs non-null to evaluate; sma_* compares vs the live metric
    assert a["dynamic_trigger"]["value"] == 0.0


def test_trigger_unknown_type_without_value_dropped():
    a = validate_trade_decision_artifact(
        {"action": "HOLD", "dynamic_trigger": {"type": "mystery_signal", "value": None}}
    )
    assert a["dynamic_trigger"] is None


def test_trigger_numeric_string_coerced():
    a = validate_trade_decision_artifact(
        {"action": "HOLD", "dynamic_trigger": {"type": "sma_50_drop", "value": "145.5"}}
    )
    assert a["dynamic_trigger"]["value"] == 145.5


def test_trigger_type_none_cleared():
    a = validate_trade_decision_artifact({"action": "BUY", "dynamic_trigger": {"type": "null", "value": None}})
    assert a["dynamic_trigger"] is None


def test_dispatcher_covers_final_decision_and_unknown_types():
    a = validate_artifact("final_decision", {"dynamic_trigger": {"type": "trailing_drop", "value": None}})
    assert a["dynamic_trigger"]["value"] == 0.10
    untouched = {"anything": 1}
    assert validate_artifact("desk_note", untouched) is untouched


# ── Consensus/data-quality sizing (code-side, audit item 6) ──────────

def test_sizing_consensus_scales_explicit_size():
    # 4% board size × consensus 75/100 = 3%
    assert resolve_buy_size_pct(4.0, 80, 0.10, consensus_score=75) == pytest.approx(0.03)


def test_sizing_consensus_floored_at_half():
    # consensus 20 would scale ×0.2 — floored at ×0.5
    assert resolve_buy_size_pct(4.0, 80, 0.10, consensus_score=20) == pytest.approx(0.02)


def test_sizing_low_data_quality_halves():
    assert resolve_buy_size_pct(4.0, 80, 0.10, consensus_score=100, data_quality=50) == pytest.approx(0.02)
    assert resolve_buy_size_pct(4.0, 80, 0.10, consensus_score=100, data_quality=75) == pytest.approx(0.04)


def test_sizing_without_signals_unchanged():
    assert resolve_buy_size_pct(4.0, 80, 0.10) == pytest.approx(0.04)
    # watch-only and fallback behavior preserved
    assert resolve_buy_size_pct(0, 80, 0.10, consensus_score=90) is None
    assert resolve_buy_size_pct(None, 80, 0.10, consensus_score=90) == pytest.approx(0.08)


# ── skip_debate contract: stub risk flag arms the mitigation gate ────

def _skip_debate_desk(**decision_overrides):
    from app.v3.shared_desk import SharedDesk
    desk = SharedDesk(ticker="TEST", cycle_id="cycle-test")
    desk.regime_classification = {"summary": "panic", "regime": "HIGH_VOLATILITY"}
    desk.tournament_result = {
        "skipped": True,
        "vetoed": False,
        "risk_flags": ["debate_skipped_by_regime"],
    }
    desk.final_decision = {
        "action": "BUY",
        "confidence": 80,
        **decision_overrides,
    }
    return desk


def test_skipped_debate_without_mitigation_holds(monkeypatch):
    import app.quant.strategy_health as sh
    monkeypatch.setattr(sh, "get_pipeline_health", lambda: {"status": "NORMAL"})
    from app.v3.orchestrator import _apply_policy_gates
    desk = _skip_debate_desk()  # no stop/trigger/size
    assert _apply_policy_gates(desk) == "HOLD_POLICY_BLOCKED_UNMITIGATED_RISK"


def test_skipped_debate_with_full_mitigation_executes(monkeypatch):
    import app.quant.strategy_health as sh
    monkeypatch.setattr(sh, "get_pipeline_health", lambda: {"status": "NORMAL"})
    from app.v3.orchestrator import _apply_policy_gates
    desk = _skip_debate_desk(
        stop_loss=100.0,
        dynamic_trigger={"type": "trailing_drop", "value": 0.1},
        position_size_pct=2.0,
    )
    assert _apply_policy_gates(desk) == "EXECUTE_BUY"


# ── Precomputed quant math block ─────────────────────────────────────

def test_quant_math_block_composes_all_lines(monkeypatch):
    import app.quant.context_block as cb
    import app.quant.returns as qr
    import app.quant.garch as qg
    import app.quant.strategy_health as sh
    import app.tools.portfolio_tools as pt
    import pandas as pd

    rng = np.random.default_rng(5)
    monkeypatch.setattr(qr, "load_close_returns", lambda t, n=500: rng.normal(0, 0.02, 300))
    monkeypatch.setattr(qg, "garch_forecast", lambda r: {
        "predicted_vol_annualized_pct": 42.0, "realized_vol_annualized_pct": 30.0,
        "prediction_premium": 0.4, "vol_signal": "EXPANSION",
    })
    monkeypatch.setattr(pt, "_current_holdings", lambda bot_id="": ({"NVDA": 5000.0, "JPM": 5000.0}, 1000.0, 11000.0))
    dates = pd.date_range("2026-01-01", periods=200)
    rets = pd.DataFrame(
        rng.normal(0, 0.01, (200, 3)), index=dates, columns=["AAPL", "JPM", "NVDA"]
    )
    monkeypatch.setattr(qr, "load_returns_matrix", lambda universe, days: (rets, []))
    monkeypatch.setattr(sh, "get_pipeline_health", lambda: {
        "status": "REDUCE", "driver": "v3_quant_analyst", "reason": "trend down",
    })

    block = cb.build_quant_math_block("AAPL")
    assert "PRECOMPUTED QUANT MATH" in block
    assert "EXPANSION" in block
    assert "HRP covariance-aware sizing" in block and "AAPL" in block
    assert "REDUCE" in block


def test_quant_math_block_empty_on_total_failure(monkeypatch):
    import app.quant.context_block as cb
    import app.quant.returns as qr
    import app.quant.strategy_health as sh
    import app.tools.portfolio_tools as pt

    def _boom(*a, **k):
        raise RuntimeError("db down")

    monkeypatch.setattr(qr, "load_close_returns", _boom)
    monkeypatch.setattr(pt, "_current_holdings", _boom)
    monkeypatch.setattr(sh, "get_pipeline_health", _boom)
    assert cb.build_quant_math_block("AAPL") == ""


# ── FRED curve/credit lines ──────────────────────────────────────────

def test_fred_curve_credit_lines(monkeypatch):
    import app.services.retrieval_context as rc
    import app.db.connection as conn_mod

    class FakeCursor:
        def execute(self, *a, **k):
            return self
        def fetchall(self):
            return [("HY_SPREAD", 5.4), ("TREASURY_10Y", 4.10), ("TREASURY_2Y", 4.60)]

    @contextlib.contextmanager
    def fake_get_db():
        yield FakeCursor()

    monkeypatch.setattr(conn_mod, "get_db", fake_get_db)
    lines = rc.fred_curve_credit_lines()
    joined = "\n".join(lines)
    assert "INVERTED" in joined            # 4.10 - 4.60 < 0
    assert "-0.50pp" in joined
    assert "elevated stress" in joined     # HY 5.4 >= 5.0


def test_fred_lines_fail_open(monkeypatch):
    import app.services.retrieval_context as rc
    import app.db.connection as conn_mod

    def _boom():
        raise RuntimeError("no db")

    monkeypatch.setattr(conn_mod, "get_db", _boom)
    assert rc.fred_curve_credit_lines() == []
